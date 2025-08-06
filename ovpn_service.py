"""OpenVPN Profile Management API with TLS-crypt-v2.
=================================================

FastAPI service that manages OpenVPN client profiles using
TLS-crypt-v2 keys. Profiles are stored in MySQL.
"""
from __future__ import annotations

import datetime
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List

import mysql.connector  # type: ignore
import requests  # type: ignore
from fastapi import FastAPI, HTTPException, Query, Response, Path as FPath
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# ENV CONFIGURATION
# ---------------------------------------------------------------------------
API_TOKEN: str = os.getenv("API_TOKEN", "ReplaceMe")
MYSQL_HOST: str = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_DB: str = os.getenv("MYSQL_DB", "ovpn_panel")
MYSQL_USER: str = os.getenv("MYSQL_USER", "ovpn_user")
MYSQL_PASS: str = os.getenv("MYSQL_PASSWORD", "ovpn_pass")

SERVER_ENDPOINT_IP: str = os.getenv("SERVER_ENDPOINT_IP", "1.2.3.4")
# Default OpenVPN server port
SERVER_ENDPOINT_PORT: int = int(os.getenv("SERVER_ENDPOINT_PORT", "21"))
DNS_SERVERS: str = os.getenv("DNS_SERVERS", "8.8.8.8")

EASYRSA_DIR: Path = Path(os.getenv("EASYRSA_DIR", "/etc/openvpn/easy-rsa"))
TLS_CRYPT_V2_SERVER_KEY: Path = Path(
    os.getenv("TLS_CRYPT_V2_SERVER_KEY", "/etc/openvpn/server/tc_v2_server.key")
)
CLIENT_KEYS_DIR: Path = Path(os.getenv("CLIENT_KEYS_DIR", "/etc/openvpn/clients"))
STATUS_LOG: Path = Path(os.getenv("STATUS_LOG", "/var/log/openvpn-status.log"))

LISTEN_PORT: int = int(os.getenv("API_PORT", "8080"))

# ---------------------------------------------------------------------------
# DATABASE INIT
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
        CREATE TABLE IF NOT EXISTS openvpn_profiles (
            id INT PRIMARY KEY AUTO_INCREMENT,
            name VARCHAR(64) NOT NULL UNIQUE,
            created_at DATETIME NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """
    )

# ---------------------------------------------------------------------------
# FASTAPI APP
# ---------------------------------------------------------------------------
app = FastAPI(title="OpenVPN API", version="1.0.0")

# ---------------------------------------------------------------------------
# UTILS
# ---------------------------------------------------------------------------
def _require_token(token: str | None):
    if token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing token")


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, check=True, env=env)
        return res.stdout.decode().strip()
    except subprocess.CalledProcessError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Command failed: {exc.stderr.decode().strip()}",
        )


def _generate_client(name: str):
    env = os.environ.copy()
    env["EASYRSA_BATCH"] = "1"
    _run(["bash", "-c", f"cd {EASYRSA_DIR} && ./easyrsa build-client-full {name} nopass"], env=env)
    CLIENT_KEYS_DIR.mkdir(parents=True, exist_ok=True)
    tls_path = CLIENT_KEYS_DIR / f"{name}.tls"
    _run(
        [
            "openvpn",
            "--tls-crypt-v2",
            str(TLS_CRYPT_V2_SERVER_KEY),
            "--genkey",
            "tls-crypt-v2-client",
            str(tls_path),
        ]
    )


def _build_config(name: str) -> str:
    ca = (EASYRSA_DIR / "pki/ca.crt").read_text()
    cert = (EASYRSA_DIR / f"pki/issued/{name}.crt").read_text()
    key = (EASYRSA_DIR / f"pki/private/{name}.key").read_text()
    tls_key = (CLIENT_KEYS_DIR / f"{name}.tls").read_text()
    return (
        "client\n"
        "dev tun\n"
        "proto udp\n"
        f"remote {SERVER_ENDPOINT_IP} {SERVER_ENDPOINT_PORT}\n"
        "resolv-retry infinite\n"
        "nobind\n"
        "persist-key\n"
        "persist-tun\n"
        "remote-cert-tls server\n"
        "cipher AES-256-GCM\n"
        "verb 3\n"
        "<ca>\n" + ca + "</ca>\n"
        "<cert>\n" + cert + "</cert>\n"
        "<key>\n" + key + "</key>\n"
        "<tls-crypt-v2>\n" + tls_key + "</tls-crypt-v2>\n"
    )

# ---------------------------------------------------------------------------
# Pydantic schema
# ---------------------------------------------------------------------------
class ProfileOut(BaseModel):
    id: int
    name: str
    created_at: datetime.datetime

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post("/profiles", response_model=ProfileOut)
def create_profile(token: str = Query(...)):
    """Create a new OpenVPN profile."""
    _require_token(token)

    with db.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(id),0)+1 FROM openvpn_profiles")
        next_id = cur.fetchone()[0]
        name = f"client{next_id}"
        now = datetime.datetime.utcnow()
        cur.execute(
            "INSERT INTO openvpn_profiles (id, name, created_at) VALUES (%s, %s, %s)",
            (next_id, name, now),
        )
    db.commit()

    try:
        _generate_client(name)
    except Exception as exc:
        with db.cursor() as cur:
            cur.execute("DELETE FROM openvpn_profiles WHERE id=%s", (next_id,))
        db.commit()
        raise HTTPException(status_code=500, detail=f"Client generation failed: {exc}")

    return ProfileOut(id=next_id, name=name, created_at=now)


@app.get("/profiles", response_model=List[ProfileOut])
def list_profiles(token: str = Query(...)):
    _require_token(token)
    with db.cursor(dictionary=True) as cur:
        cur.execute("SELECT id, name, created_at FROM openvpn_profiles")
        return cur.fetchall()


@app.get(
    "/profiles/{profile_id}/config",
    response_class=Response,
    responses={200: {"content": {"text/plain": {}}}},
)
def download_config(profile_id: int = FPath(..., ge=1), token: str = Query(...)):
    _require_token(token)
    with db.cursor() as cur:
        cur.execute("SELECT name FROM openvpn_profiles WHERE id=%s", (profile_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")
    name = row[0]
    conf = _build_config(name)
    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Disposition": f"attachment; filename={name}.ovpn",
    }
    return Response(content=conf, media_type="application/octet-stream", headers=headers)


@app.delete("/profiles/{profile_id}")
def delete_profile(profile_id: int = FPath(..., ge=1), token: str = Query(...)):
    _require_token(token)
    with db.cursor() as cur:
        cur.execute("SELECT name FROM openvpn_profiles WHERE id=%s", (profile_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")
    name = row[0]
    for p in [
        EASYRSA_DIR / f"pki/issued/{name}.crt",
        EASYRSA_DIR / f"pki/private/{name}.key",
        CLIENT_KEYS_DIR / f"{name}.tls",
    ]:
        try:
            Path(p).unlink()
        except FileNotFoundError:
            pass
    with db.cursor() as cur:
        cur.execute("DELETE FROM openvpn_profiles WHERE id=%s", (profile_id,))
    db.commit()
    return {"status": "deleted", "profile_id": profile_id}

# ---------------------------------------------------------------------------
# Periodic status reporting
# ---------------------------------------------------------------------------
def send_status_update():
    profiles: list[dict] = []
    active_ids: list[int] = []
    try:
        if not db.is_connected():
            db.reconnect()
        with db.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT id, name, created_at FROM openvpn_profiles ORDER BY id"
            )
            db_profiles = cur.fetchall()
        name_to_id = {p["name"]: p["id"] for p in db_profiles}
        for p in db_profiles:
            if isinstance(p.get("created_at"), datetime.datetime):
                p["created_at"] = p["created_at"].isoformat()
        profiles = db_profiles
        if STATUS_LOG.exists():
            with STATUS_LOG.open() as f:
                in_clients = False
                for line in f:
                    line = line.strip()
                    if line.startswith("Common Name"):
                        in_clients = True
                        continue
                    if not in_clients or line == "" or line.startswith("ROUTING") or line.startswith("GLOBAL"):
                        continue
                    parts = line.split(",")
                    name = parts[0]
                    pid = name_to_id.get(name)
                    if pid:
                        active_ids.append(pid)
    except mysql.connector.Error as e:
        print(f"Could not retrieve profiles from database: {e}", file=sys.stderr)

    payload = {
        "api_key": API_TOKEN,
        "server_endpoint_ip": SERVER_ENDPOINT_IP,
        "server_endpoint_port": SERVER_ENDPOINT_PORT,
        "profiles": profiles,
        "active_profile_ids": active_ids,
    }
    try:
        print("Sending status update to mvpn.space...")
        response = requests.post("https://mvpn.space/status", json=payload, timeout=30)
        response.raise_for_status()
        print(f"Status update sent successfully (HTTP {response.status_code}).")
    except requests.exceptions.RequestException as e:
        print(f"Failed to send status update: {e}", file=sys.stderr)


def run_periodic_reporter():
    print("Starting periodic status reporter...")
    while True:
        send_status_update()
        time.sleep(60)

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
@app.on_event("startup")
def on_startup():
    reporter_thread = threading.Thread(target=run_periodic_reporter, daemon=True)
    reporter_thread.start()

# ---------------------------------------------------------------------------
# Local dev entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("ovpn_service:app", host="0.0.0.0", port=LISTEN_PORT, reload=True)
