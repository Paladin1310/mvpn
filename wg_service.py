"""Xray VLESS + Reality management API"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import List
from urllib.parse import urlencode

import mysql.connector
import requests
from fastapi import FastAPI, HTTPException, Query, Response, Path as FPath
from pydantic import BaseModel

# Try to use psutil for accurate CPU percent
try:
    import psutil  # type: ignore
    _HAS_PSUTIL = True
except Exception:  # noqa: BLE001
    psutil = None  # type: ignore
    _HAS_PSUTIL = False

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------
API_TOKEN: str = os.getenv("API_TOKEN", "ReplaceMe")
MYSQL_HOST: str = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_DB: str = os.getenv("MYSQL_DB", "xray_panel")
MYSQL_USER: str = os.getenv("MYSQL_USER", "xray_user")
MYSQL_PASS: str = os.getenv("MYSQL_PASSWORD", "xray_pass")

# Separate DB for temporary profiles
TEMP_MYSQL_HOST: str = os.getenv("TEMP_MYSQL_HOST", MYSQL_HOST)
TEMP_MYSQL_DB: str = os.getenv("TEMP_MYSQL_DB", "xray_temp")
TEMP_MYSQL_USER: str = os.getenv("TEMP_MYSQL_USER", MYSQL_USER)
TEMP_MYSQL_PASS: str = os.getenv("TEMP_MYSQL_PASSWORD", MYSQL_PASS)

SERVER_DOMAIN: str = os.getenv("SERVER_DOMAIN", "example.com")
SERVER_PORT: int = int(os.getenv("SERVER_PORT", "443"))
SERVER_PUBLIC_KEY: str = os.getenv("SERVER_PUBLIC_KEY", "<pbk>")
# Extra fields for status reporting compatibility with WireGuard version
WG_INTERFACE: str = os.getenv("WG_INTERFACE", "xray")
SERVER_ENDPOINT_IP: str = os.getenv("SERVER_ENDPOINT_IP", SERVER_DOMAIN)
SERVER_ENDPOINT_PORT: int = int(os.getenv("SERVER_ENDPOINT_PORT", str(SERVER_PORT)))
VPN_NETWORK_STR: str = os.getenv("VPN_NETWORK", "")
DNS_SERVERS: str = os.getenv("DNS_SERVERS", "")
XRAY_CONFIG: Path = Path(os.getenv("XRAY_CONFIG", "/usr/local/etc/xray/config.json"))

SNI = "vk.com"
FINGERPRINT = "chrome"
API_PORT: int = int(os.getenv("API_PORT", "8080"))

# ---------------------------------------------------------------------------
# Database initialisation
# ---------------------------------------------------------------------------
# Main DB
_db_connect_kwargs = dict(host=MYSQL_HOST, user=MYSQL_USER, password=MYSQL_PASS, database=MYSQL_DB, autocommit=True)
db = mysql.connector.connect(**_db_connect_kwargs)

with db.cursor() as cur:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS vless_profiles (
            id INT PRIMARY KEY AUTO_INCREMENT,
            uuid CHAR(36) NOT NULL UNIQUE,
            short_id VARCHAR(16) NOT NULL UNIQUE,
            created_at DATETIME NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """
    )

# Temporary DB (separate database)

def _ensure_temp_database_and_connect() -> mysql.connector.connection.MySQLConnection:
    try:
        return mysql.connector.connect(
            host=TEMP_MYSQL_HOST,
            user=TEMP_MYSQL_USER,
            password=TEMP_MYSQL_PASS,
            database=TEMP_MYSQL_DB,
            autocommit=True,
        )
    except mysql.connector.Error as exc:
        # Attempt to create the database if it doesn't exist
        try:
            admin_conn = mysql.connector.connect(
                host=TEMP_MYSQL_HOST,
                user=TEMP_MYSQL_USER,
                password=TEMP_MYSQL_PASS,
                autocommit=True,
            )
            with admin_conn.cursor() as cur:
                cur.execute(f"CREATE DATABASE IF NOT EXISTS `{TEMP_MYSQL_DB}` CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci")
            admin_conn.close()
            # Reconnect to the created DB
            return mysql.connector.connect(
                host=TEMP_MYSQL_HOST,
                user=TEMP_MYSQL_USER,
                password=TEMP_MYSQL_PASS,
                database=TEMP_MYSQL_DB,
                autocommit=True,
            )
        except mysql.connector.Error as inner_exc:
            print(f"Failed to create/connect temporary DB: {inner_exc}", file=sys.stderr)
            raise


temp_db = _ensure_temp_database_and_connect()

with temp_db.cursor() as cur:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS temp_vless_profiles (
            id INT PRIMARY KEY AUTO_INCREMENT,
            uuid CHAR(36) NOT NULL UNIQUE,
            short_id VARCHAR(16) NOT NULL UNIQUE,
            created_at DATETIME NOT NULL,
            expires_at DATETIME NOT NULL,
            INDEX (expires_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """
    )

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Xray VLESS API", version="1.0.0")

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _require_token(token: str | None):
    if token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing token")


def _load_config() -> dict:
    try:
        return json.loads(XRAY_CONFIG.read_text())
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="Xray config not found")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid Xray config: {exc}")


def _save_config(cfg: dict):
    XRAY_CONFIG.write_text(json.dumps(cfg, indent=2))


def _restart_xray():
    try:
        subprocess.run(["systemctl", "restart", "xray"], check=True)
    except subprocess.CalledProcessError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to restart xray: {exc.stderr}")


def _generate_uuid() -> str:
    return str(uuid.uuid4())


def _generate_short_id() -> str:
    return os.urandom(4).hex()


def _add_client(uuid_: str, short_id: str):
    cfg = _load_config()
    inbound = cfg.get("inbounds", [{}])[0]
    clients = inbound.setdefault("settings", {}).setdefault("clients", [])
    clients.append({"id": uuid_, "flow": "xtls-rprx-vision"})
    reality = inbound.setdefault("streamSettings", {}).setdefault("realitySettings", {})
    short_ids = reality.setdefault("shortIds", [])
    short_ids.append(short_id)
    _save_config(cfg)
    _restart_xray()


def _remove_client(uuid_: str, short_id: str):
    cfg = _load_config()
    inbound = cfg.get("inbounds", [{}])[0]
    clients = inbound.get("settings", {}).get("clients", [])
    inbound["settings"]["clients"] = [c for c in clients if c.get("id") != uuid_]
    reality = inbound.get("streamSettings", {}).get("realitySettings", {})
    reality["shortIds"] = [s for s in reality.get("shortIds", []) if s != short_id]
    _save_config(cfg)
    _restart_xray()


def _build_link(uuid_: str, short_id: str, label: str, sni: str | None = None) -> str:
    params = {
        "type": "tcp",
        "security": "reality",
        "fp": FINGERPRINT,
        "pbk": SERVER_PUBLIC_KEY,
        "sni": sni or SNI,
        "sid": short_id,
        "flow": "xtls-rprx-vision",
    }
    return f"vless://{uuid_}@{SERVER_DOMAIN}:{SERVER_PORT}?{urlencode(params)}#{label}"


def _get_cpu_percent() -> float:
    if _HAS_PSUTIL and psutil is not None:
        try:
            # Short sampling to estimate current CPU percent
            return float(psutil.cpu_percent(interval=0.2))
        except Exception:  # noqa: BLE001
            pass
    # Fallback: approximate via 1-min load average
    try:
        la1, _la5, _la15 = os.getloadavg()
        num_cpus = os.cpu_count() or 1
        percent = (la1 / float(num_cpus)) * 100.0
        if percent < 0:
            percent = 0.0
        if percent > 100.0:
            percent = 100.0
        return float(percent)
    except Exception:  # noqa: BLE001
        return 0.0

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ProfileOut(BaseModel):
    id: int
    uuid: str
    short_id: str
    created_at: datetime.datetime


class TempProfileOut(BaseModel):
    id: int
    uuid: str
    short_id: str
    created_at: datetime.datetime
    expires_at: datetime.datetime

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post("/profiles", response_model=ProfileOut)
def create_profile(token: str = Query(...)):
    _require_token(token)
    uuid_ = _generate_uuid()
    short_id = _generate_short_id()
    now = datetime.datetime.utcnow()

    _add_client(uuid_, short_id)

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO vless_profiles (uuid, short_id, created_at) VALUES (%s, %s, %s)",
            (uuid_, short_id, now),
        )
        profile_id = cur.lastrowid
    db.commit()

    return ProfileOut(id=profile_id, uuid=uuid_, short_id=short_id, created_at=now)


@app.get("/profiles", response_model=List[ProfileOut])
def list_profiles(token: str = Query(...)):
    _require_token(token)
    with db.cursor(dictionary=True) as cur:
        cur.execute("SELECT id, uuid, short_id, created_at FROM vless_profiles")
        return cur.fetchall()


@app.get(
    "/profiles/{profile_id}/config",
    response_class=Response,
    responses={200: {"content": {"text/plain": {}}}},
)
def get_config(profile_id: int = FPath(..., ge=1), token: str = Query(...), sni: str | None = Query(None)):
    _require_token(token)
    with db.cursor(dictionary=True) as cur:
        cur.execute(
            "SELECT uuid, short_id FROM vless_profiles WHERE id=%s",
            (profile_id,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")
    link = _build_link(row["uuid"], row["short_id"], f"profile-{profile_id}", sni=sni)
    return Response(content=link, media_type="text/plain")


@app.delete("/profiles/{profile_id}")
def delete_profile(profile_id: int = FPath(..., ge=1), token: str = Query(...)):
    _require_token(token)
    with db.cursor(dictionary=True) as cur:
        cur.execute(
            "SELECT uuid, short_id FROM vless_profiles WHERE id=%s",
            (profile_id,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")

    _remove_client(row["uuid"], row["short_id"])

    with db.cursor() as cur:
        cur.execute("DELETE FROM vless_profiles WHERE id=%s", (profile_id,))
    db.commit()

    return {"status": "deleted", "profile_id": profile_id}

# -------------------- Temporary profiles (separate DB) ----------------------

@app.post("/temp-profiles", response_model=TempProfileOut)
def create_temp_profile(token: str = Query(...)):
    _require_token(token)
    uuid_ = _generate_uuid()
    short_id = _generate_short_id()
    now = datetime.datetime.utcnow()
    expires_at = now + datetime.timedelta(days=1)

    _add_client(uuid_, short_id)

    if not temp_db.is_connected():
        temp_db.reconnect()

    with temp_db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO temp_vless_profiles (uuid, short_id, created_at, expires_at)
            VALUES (%s, %s, %s, %s)
            """,
            (uuid_, short_id, now, expires_at),
        )
        temp_id = cur.lastrowid
    temp_db.commit()

    return TempProfileOut(
        id=temp_id, uuid=uuid_, short_id=short_id, created_at=now, expires_at=expires_at
    )


@app.get("/temp-profiles", response_model=List[TempProfileOut])
def list_temp_profiles(token: str = Query(...)):
    _require_token(token)
    if not temp_db.is_connected():
        temp_db.reconnect()
    with temp_db.cursor(dictionary=True) as cur:
        cur.execute(
            "SELECT id, uuid, short_id, created_at, expires_at FROM temp_vless_profiles ORDER BY id"
        )
        return cur.fetchall()


@app.get(
    "/temp-profiles/{temp_id}/config",
    response_class=Response,
    responses={200: {"content": {"text/plain": {}}}},
)
def get_temp_config(temp_id: int = FPath(..., ge=1), token: str = Query(...), sni: str | None = Query(None)):
    _require_token(token)
    if not temp_db.is_connected():
        temp_db.reconnect()
    with temp_db.cursor(dictionary=True) as cur:
        cur.execute(
            "SELECT uuid, short_id, expires_at FROM temp_vless_profiles WHERE id=%s",
            (temp_id,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Temp profile not found")
    if isinstance(row.get("expires_at"), datetime.datetime) and row["expires_at"] <= datetime.datetime.utcnow():
        # Expired: remove and report 404
        try:
            _remove_client(row["uuid"], row["short_id"])
        finally:
            if not temp_db.is_connected():
                temp_db.reconnect()
            with temp_db.cursor() as cur:
                cur.execute("DELETE FROM temp_vless_profiles WHERE id=%s", (temp_id,))
            temp_db.commit()
        raise HTTPException(status_code=404, detail="Temp profile expired")

    link = _build_link(row["uuid"], row["short_id"], f"temp-{temp_id}", sni=sni)
    return Response(content=link, media_type="text/plain")


@app.delete("/temp-profiles/{temp_id}")
def delete_temp_profile(temp_id: int = FPath(..., ge=1), token: str = Query(...)):
    _require_token(token)
    if not temp_db.is_connected():
        temp_db.reconnect()
    with temp_db.cursor(dictionary=True) as cur:
        cur.execute(
            "SELECT uuid, short_id FROM temp_vless_profiles WHERE id=%s",
            (temp_id,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Temp profile not found")

    _remove_client(row["uuid"], row["short_id"])

    if not temp_db.is_connected():
        temp_db.reconnect()
    with temp_db.cursor() as cur:
        cur.execute("DELETE FROM temp_vless_profiles WHERE id=%s", (temp_id,))
    temp_db.commit()

    return {"status": "deleted", "temp_profile_id": temp_id}

# ---------------------------------------------------------------------------
# Periodic status reporting and temp cleanup
# ---------------------------------------------------------------------------

def cleanup_expired_temp_profiles():
    try:
        if not temp_db.is_connected():
            temp_db.reconnect()
        # Find all expired
        with temp_db.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT id, uuid, short_id FROM temp_vless_profiles WHERE expires_at <= UTC_TIMESTAMP()"
            )
            expired = cur.fetchall()
        for row in expired:
            try:
                _remove_client(row["uuid"], row["short_id"])
            except HTTPException as exc:
                print(f"Failed to remove expired temp client from xray: {exc}", file=sys.stderr)
            try:
                if not temp_db.is_connected():
                    temp_db.reconnect()
                with temp_db.cursor() as cur:
                    cur.execute("DELETE FROM temp_vless_profiles WHERE id=%s", (row["id"],))
                temp_db.commit()
            except mysql.connector.Error as exc:
                print(f"Failed to delete expired temp profile from DB: {exc}", file=sys.stderr)
    except mysql.connector.Error as exc:
        print(f"Temp DB cleanup error: {exc}", file=sys.stderr)


def send_status_update():
    profiles: list[dict] = []
    try:
        if not db.is_connected():
            db.reconnect()
        with db.cursor(dictionary=True) as cur:
            cur.execute("SELECT id, uuid, short_id, created_at FROM vless_profiles ORDER BY id")
            db_profiles = cur.fetchall()
        for p in db_profiles:
            if isinstance(p.get("created_at"), datetime.datetime):
                p["created_at"] = p["created_at"].isoformat()
        profiles = db_profiles
    except mysql.connector.Error as e:
        print(f"Could not retrieve profiles from database: {e}", file=sys.stderr)

    payload = {
        "api_key": API_TOKEN,
        "server_domain": SERVER_DOMAIN,
        "server_port": SERVER_PORT,
        "server_public_key": SERVER_PUBLIC_KEY,
        "profiles": profiles,
        # Fields kept for compatibility with the legacy WireGuard service
        "wg_interface": WG_INTERFACE,
        "server_endpoint_ip": SERVER_ENDPOINT_IP,
        "server_endpoint_port": SERVER_ENDPOINT_PORT,
        "vpn_network": VPN_NETWORK_STR,
        "dns_servers": DNS_SERVERS,
        # New: CPU usage percent
        "cpu_percent": _get_cpu_percent(),
    }
    try:
        print("Sending status update to mvpn.space...")
        response = requests.post("https://mvpn.space/status", json=payload, timeout=30)
        response.raise_for_status()
        print(f"Status update sent successfully (HTTP {response.status_code}).")
    except requests.exceptions.RequestException as e:
        print(f"Failed to send status update: {e}", file=sys.stderr)


def run_periodic_reporter():
    while True:
        # Clean up expired temporary profiles on each tick
        cleanup_expired_temp_profiles()
        send_status_update()
        time.sleep(60)

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
def on_startup():
    threading.Thread(target=run_periodic_reporter, daemon=True).start()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("wg_service:app", host="0.0.0.0", port=API_PORT, reload=True)
