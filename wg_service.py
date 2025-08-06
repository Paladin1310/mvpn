"""VLESS Client Management API for Xray (Reality + XTLS-Vision)
================================================================

This FastAPI service manages VLESS clients for an Xray server
configured with Reality and XTLS-RPRX-Vision over TCP.  Endpoints:

* **POST /profiles** – create a new VLESS client
* **GET  /profiles** – list clients
* **GET  /profiles/{id}/config** – return vless:// link
* **DELETE /profiles/{id}** – remove client

All endpoints are protected with a single shared API token passed as
`?token=…` query parameter.
"""

from __future__ import annotations

import datetime
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import List

import mysql.connector  # type: ignore
import requests  # type: ignore
from fastapi import FastAPI, HTTPException, Query, Response, Path as FPath
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# ENVIRONMENT
# ---------------------------------------------------------------------------

API_TOKEN: str = os.getenv("API_TOKEN", "ReplaceMe")
MYSQL_HOST: str = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_DB: str = os.getenv("MYSQL_DB", "xray_panel")
MYSQL_USER: str = os.getenv("MYSQL_USER", "xray_user")
MYSQL_PASS: str = os.getenv("MYSQL_PASSWORD", "xray_pass")

XRAY_CONFIG_PATH: Path = Path(os.getenv("XRAY_CONFIG_PATH", "/usr/local/etc/xray/config.json"))
XRAY_BINARY: str = os.getenv("XRAY_BINARY", "/usr/local/bin/xray")

SERVER_PUBLIC_KEY: str = os.getenv("SERVER_PUBLIC_KEY", "")
SERVER_DOMAIN: str = os.getenv("SERVER_DOMAIN", "1.2.3.4")
SERVER_PORT: int = int(os.getenv("SERVER_PORT", "443"))
SNI: str = os.getenv("SNI", "vk.com")
FP: str = os.getenv("FP", "chrome")

LISTEN_PORT: int = int(os.getenv("API_PORT", "8080"))

# ---------------------------------------------------------------------------
# DATABASE INITIALISATION (MySQL)
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
            uuid VARCHAR(40) NOT NULL UNIQUE,
            short_id VARCHAR(16) NOT NULL UNIQUE,
            label VARCHAR(255),
            created_at DATETIME NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """
    )

# ---------------------------------------------------------------------------
# FASTAPI APP
# ---------------------------------------------------------------------------

app = FastAPI(title="Xray VLESS API", version="1.0.0")

# ---------------------------------------------------------------------------
# UTILS
# ---------------------------------------------------------------------------


def _require_token(token: str | None) -> None:
    if token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing token")


def _run(cmd: list[str]) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, check=True)
        return res.stdout.decode().strip()
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode().strip()
        stdout = exc.stdout.decode().strip()
        msg = stderr or stdout or f"exit code {exc.returncode}"
        raise HTTPException(status_code=500, detail=f"Command failed: {msg}")


def _load_config() -> dict:
    with XRAY_CONFIG_PATH.open() as f:
        return json.load(f)


def _save_config(cfg: dict) -> None:
    tmp = XRAY_CONFIG_PATH.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, XRAY_CONFIG_PATH)


def _reload_xray() -> None:
    _run(["systemctl", "reload", "xray"])


def _generate_uuid() -> str:
    return str(uuid.uuid4())


def _generate_short_id() -> str:
    return secrets.token_hex(4)  # 8 hex chars


def _build_link(uuid_str: str, sid: str, label: str) -> str:
    return (
        f"vless://{uuid_str}@{SERVER_DOMAIN}:{SERVER_PORT}?type=tcp&security=reality"
        f"&fp={FP}&pbk={SERVER_PUBLIC_KEY}&sni={SNI}&sid={sid}&flow=xtls-rprx-vision#{label}"
    )


class ProfileOut(BaseModel):
    id: int
    uuid: str
    short_id: str
    label: str | None
    created_at: datetime.datetime
    link: str


# ---------------------------------------------------------------------------
# ENDPOINTS
# ---------------------------------------------------------------------------


@app.post("/profiles", response_model=ProfileOut)
def create_profile(token: str = Query(...), label: str | None = Query(default=None)):
    """Create a new VLESS client."""

    _require_token(token)

    uuid_str = _generate_uuid()
    sid = _generate_short_id()
    if not label:
        label = uuid_str[:8]
    now = datetime.datetime.utcnow()

    cfg = _load_config()
    inbound = cfg["inbounds"][0]
    inbound["settings"]["clients"].append({"id": uuid_str, "flow": "xtls-rprx-vision", "email": label})
    inbound["streamSettings"]["realitySettings"]["shortIds"].append(sid)
    _save_config(cfg)
    _reload_xray()

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO vless_profiles (uuid, short_id, label, created_at) VALUES (%s, %s, %s, %s)",
            (uuid_str, sid, label, now),
        )
        profile_id = cur.lastrowid
    db.commit()

    link = _build_link(uuid_str, sid, label)
    return ProfileOut(id=profile_id, uuid=uuid_str, short_id=sid, label=label, created_at=now, link=link)


@app.get("/profiles", response_model=List[ProfileOut])
def list_profiles(token: str = Query(...)):
    _require_token(token)
    with db.cursor(dictionary=True) as cur:
        cur.execute("SELECT id, uuid, short_id, label, created_at FROM vless_profiles")
        rows = cur.fetchall()

    profiles: List[ProfileOut] = []
    for r in rows:
        lbl = r.get("label") or r["uuid"][:8]
        link = _build_link(r["uuid"], r["short_id"], lbl)
        profiles.append(
            ProfileOut(
                id=r["id"],
                uuid=r["uuid"],
                short_id=r["short_id"],
                label=r.get("label"),
                created_at=r["created_at"],
                link=link,
            )
        )
    return profiles


@app.get("/profiles/{profile_id}/config", response_class=Response, responses={200: {"content": {"text/plain": {}}}})
def download_config(profile_id: int = FPath(..., ge=1), token: str = Query(...)):
    _require_token(token)

    with db.cursor(dictionary=True) as cur:
        cur.execute("SELECT uuid, short_id, label FROM vless_profiles WHERE id=%s", (profile_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")

    lbl = row.get("label") or row["uuid"][:8]
    link = _build_link(row["uuid"], row["short_id"], lbl)
    headers = {
        "Content-Type": "text/plain",
        "Content-Disposition": f"attachment; filename=vless-{profile_id}.txt",
    }
    return Response(content=link, media_type="text/plain", headers=headers)


@app.delete("/profiles/{profile_id}")
def delete_profile(profile_id: int = FPath(..., ge=1), token: str = Query(...)):
    _require_token(token)

    with db.cursor(dictionary=True) as cur:
        cur.execute("SELECT uuid, short_id FROM vless_profiles WHERE id=%s", (profile_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")

    uuid_str = row["uuid"]
    sid = row["short_id"]

    cfg = _load_config()
    inbound = cfg["inbounds"][0]
    inbound["settings"]["clients"] = [c for c in inbound["settings"]["clients"] if c.get("id") != uuid_str]
    reality = inbound["streamSettings"]["realitySettings"]
    reality["shortIds"] = [s for s in reality.get("shortIds", []) if s != sid]
    if not reality["shortIds"]:
        reality["shortIds"].append(_generate_short_id())
    _save_config(cfg)
    _reload_xray()

    with db.cursor() as cur:
        cur.execute("DELETE FROM vless_profiles WHERE id=%s", (profile_id,))
    db.commit()

    return {"status": "deleted", "profile_id": profile_id}


# ---------------------------------------------------------------------------
# PERIODIC STATUS REPORTING
# ---------------------------------------------------------------------------


def send_status_update() -> None:
    profiles: list[dict] = []
    try:
        if not db.is_connected():
            db.reconnect()

        with db.cursor(dictionary=True) as cur:
            cur.execute("SELECT id, uuid, short_id, label, created_at FROM vless_profiles ORDER BY id")
            db_profiles = cur.fetchall()

        for p in db_profiles:
            if p.get("created_at") and isinstance(p["created_at"], datetime.datetime):
                p["created_at"] = p["created_at"].isoformat()
        profiles = db_profiles
    except mysql.connector.Error as e:
        print(f"Could not retrieve profiles from database: {e}", file=sys.stderr)

    payload = {
        "api_key": API_TOKEN,
        "server_public_key": SERVER_PUBLIC_KEY,
        "server_domain": SERVER_DOMAIN,
        "server_port": SERVER_PORT,
        "profiles": profiles,
        "active_profile_ids": [],
    }

    try:
        print("Sending status update to mvpn.space...")
        response = requests.post("https://mvpn.space/status", json=payload, timeout=30)
        response.raise_for_status()
        print(f"Status update sent successfully (HTTP {response.status_code}).")
    except requests.exceptions.RequestException as e:
        print(f"Failed to send status update: {e}", file=sys.stderr)


def run_periodic_reporter() -> None:
    print("Starting periodic status reporter...")
    while True:
        send_status_update()
        time.sleep(300)


# ---------------------------------------------------------------------------
# APP LIFECYCLE
# ---------------------------------------------------------------------------


@app.on_event("startup")
def on_startup() -> None:
    reporter_thread = threading.Thread(target=run_periodic_reporter, daemon=True)
    reporter_thread.start()


# ---------------------------------------------------------------------------
# LOCAL DEV ENTRYPOINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("wg_service:app", host="0.0.0.0", port=LISTEN_PORT, reload=True)

