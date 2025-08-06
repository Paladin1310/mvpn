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

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------
API_TOKEN: str = os.getenv("API_TOKEN", "ReplaceMe")
MYSQL_HOST: str = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_DB: str = os.getenv("MYSQL_DB", "xray_panel")
MYSQL_USER: str = os.getenv("MYSQL_USER", "xray_user")
MYSQL_PASS: str = os.getenv("MYSQL_PASSWORD", "xray_pass")

SERVER_DOMAIN: str = os.getenv("SERVER_DOMAIN", "example.com")
SERVER_PORT: int = int(os.getenv("SERVER_PORT", "443"))
SERVER_PUBLIC_KEY: str = os.getenv("SERVER_PUBLIC_KEY", "<pbk>")
XRAY_CONFIG: Path = Path(os.getenv("XRAY_CONFIG", "/usr/local/etc/xray/config.json"))

SNI = "vk.com"
FINGERPRINT = "chrome"
API_PORT: int = int(os.getenv("API_PORT", "8080"))

# ---------------------------------------------------------------------------
# Database initialisation
# ---------------------------------------------------------------------------
db = mysql.connector.connect(
    host=MYSQL_HOST,
    user=MYSQL_USER,
    password=MYSQL_PASS,
    database=MYSQL_DB,
    autocommit=True,
)

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


def _build_link(uuid_: str, short_id: str, label: str) -> str:
    params = {
        "type": "tcp",
        "security": "reality",
        "fp": FINGERPRINT,
        "pbk": SERVER_PUBLIC_KEY,
        "sni": SNI,
        "sid": short_id,
        "flow": "xtls-rprx-vision",
    }
    return f"vless://{uuid_}@{SERVER_DOMAIN}:{SERVER_PORT}?{urlencode(params)}#{label}"

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ProfileOut(BaseModel):
    id: int
    uuid: str
    short_id: str
    created_at: datetime.datetime

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
def get_config(profile_id: int = FPath(..., ge=1), token: str = Query(...)):
    _require_token(token)
    with db.cursor(dictionary=True) as cur:
        cur.execute(
            "SELECT uuid, short_id FROM vless_profiles WHERE id=%s",
            (profile_id,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")
    link = _build_link(row["uuid"], row["short_id"], f"profile-{profile_id}")
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

# ---------------------------------------------------------------------------
# Periodic status reporting
# ---------------------------------------------------------------------------

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
        send_status_update()
        time.sleep(300)

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
def on_startup():
    threading.Thread(target=run_periodic_reporter, daemon=True).start()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("wg_service:app", host="0.0.0.0", port=API_PORT, reload=True)
