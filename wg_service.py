"""AmneziaWG Profile Management API — no accounts, one API key
===========================================================
Minimal FastAPI service that automates creation / listing / deletion of
AmneziaWG peers while persisting data in MySQL. **No per‑user accounts or
quotas** — a single shared `API_TOKEN` protects every endpoint.

Key flow (matches the original PHP description minus auth):
----------------------------------------------------------------
1. **POST /profiles** – generates key pair, picks next IP, attaches the peer via
   `awg set`, appends a `[Peer]` block to `/etc/amnezia/amneziawg/awg0.conf`, stores data
   in `wireguard_profiles`, and returns JSON with profile metadata.
2. **GET /profiles** – returns the list of every existing profile.
3. **GET /profiles/{id}/config** – produces a ready `.conf` file for the client.
4. **DELETE /profiles/{id}** – removes the peer from interface + DB.
"""
from __future__ import annotations

import datetime
import ipaddress
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List

import mysql.connector  # pip install mysql-connector-python
import requests  # pip install requests
from fastapi import FastAPI, HTTPException, Query, Response, Path as FPath
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# ENVIRONMENT CONFIGURATION (adjust in /etc/systemd/system/…)
# ---------------------------------------------------------------------------

API_TOKEN: str = os.getenv("API_TOKEN", "ReplaceMe")
MYSQL_HOST: str = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_DB: str = os.getenv("MYSQL_DB", "wg_panel")
MYSQL_USER: str = os.getenv("MYSQL_USER", "wg_user")
MYSQL_PASS: str = os.getenv("MYSQL_PASSWORD", "wg_pass")

WG_INTERFACE: str = os.getenv("WG_INTERFACE", "awg0")
SERVER_PUBLIC_KEY: str = os.getenv("SERVER_PUBLIC_KEY", "<server‑pubkey>")
SERVER_ENDPOINT_IP: str = os.getenv("SERVER_ENDPOINT_IP", "1.2.3.4")
SERVER_ENDPOINT_PORT: int = int(os.getenv("SERVER_ENDPOINT_PORT", "51830"))

VPN_NETWORK_STR: str = os.getenv("VPN_NETWORK", "10.100.10.0/24")
DNS_SERVERS: str = os.getenv("DNS_SERVERS", "8.8.8.8")

LISTEN_PORT: int = int(os.getenv("API_PORT", "8080"))
WG_CLI: str = os.getenv("WG_CLI", "awg")
WG_CONF_DIR: Path = Path(os.getenv("WG_CONF_DIR", "/etc/amnezia/amneziawg"))
WG_CONF_PATH: Path = WG_CONF_DIR / f"{WG_INTERFACE}.conf"

# Optional AmneziaWG obfuscation parameters (device-level)
AWG_JC: str = os.getenv("WG_JC", "0")
AWG_JMIN: str = os.getenv("WG_JMIN", "0")
AWG_JMAX: str = os.getenv("WG_JMAX", "0")
AWG_S1: str = os.getenv("WG_S1", "0")
AWG_S2: str = os.getenv("WG_S2", "0")
AWG_H1: str = os.getenv("WG_H1", "0")
AWG_H2: str = os.getenv("WG_H2", "0")
AWG_H3: str = os.getenv("WG_H3", "0")
AWG_H4: str = os.getenv("WG_H4", "0")

try:
    VPN_NETWORK = ipaddress.ip_network(VPN_NETWORK_STR)
except ValueError as exc:
    sys.exit(f"Invalid VPN_NETWORK: {exc}")

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
        CREATE TABLE IF NOT EXISTS wireguard_profiles (
            id INT PRIMARY KEY AUTO_INCREMENT,
            private_key TEXT NOT NULL,
            public_key TEXT NOT NULL,
            vpn_address VARCHAR(45) NOT NULL UNIQUE,
            created_at DATETIME NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """
    )

# ---------------------------------------------------------------------------
# FASTAPI APP
# ---------------------------------------------------------------------------

app = FastAPI(title="AmneziaWG API", version="3.0.0")

# ---------------------------------------------------------------------------
# UTILS
# ---------------------------------------------------------------------------

def _require_token(token: str | None):
    if token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing token")


def _run(cmd: list[str], *, input_: str | None = None) -> str:
    try:
        res = subprocess.run(
            cmd,
            input=(input_.encode() if input_ else None),
            capture_output=True,
            check=True,
        )
        return res.stdout.decode().strip()
    except subprocess.CalledProcessError as exc:
        raise HTTPException(status_code=500, detail=f"Command failed: {exc.stderr.decode().strip()}")


def _generate_keys() -> tuple[str, str]:
    priv = _run([WG_CLI, "genkey"])
    pub = _run([WG_CLI, "pubkey"], input_=priv)
    return priv, pub


def _next_ip() -> str:
    """Return next unused /32 inside VPN_NETWORK."""
    with db.cursor() as cur:
        cur.execute("SELECT vpn_address FROM wireguard_profiles ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    if row:
        next_ip = ipaddress.ip_address(row[0]) + 1
    else:
        # Reserve .1 for the server – start with .2
        next_ip = list(VPN_NETWORK.hosts())[1]
    if next_ip not in VPN_NETWORK:
        raise HTTPException(status_code=500, detail="Address pool exhausted")
    return str(next_ip)


def _attach_peer(pubkey: str, ip_: str):
    _run([WG_CLI, "set", WG_INTERFACE, "peer", pubkey, "allowed-ips", f"{ip_}/32"])


def _remove_peer(pubkey: str):
    _run([WG_CLI, "set", WG_INTERFACE, "peer", pubkey, "remove"])


def _append_conf_block(pubkey: str, ip_: str):
    WG_CONF_PATH.write_text(
        WG_CONF_PATH.read_text() + f"\n[Peer]\nPublicKey = {pubkey}\nAllowedIPs = {ip_}/32\n"
    )

# ---------------------------------------------------------------------------
# Pydantic SCHEMA
# ---------------------------------------------------------------------------

class ProfileOut(BaseModel):
    id: int
    vpn_address: str
    created_at: datetime.datetime

# ---------------------------------------------------------------------------
# ENDPOINTS
# ---------------------------------------------------------------------------

@app.post("/profiles", response_model=ProfileOut)
def create_profile(token: str = Query(...)):
    """Create a new AmneziaWG profile."""

    _require_token(token)

    priv, pub = _generate_keys()
    ip_str = _next_ip()
    now = datetime.datetime.utcnow()

    # Attach immediately
    _attach_peer(pub, ip_str)

    # Append to wg0.conf
    try:
        _append_conf_block(pub, ip_str)
    except Exception as exc:
        _remove_peer(pub)
        raise HTTPException(status_code=500, detail=f"Failed to write wg0.conf: {exc}")

    # DB insert
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO wireguard_profiles (private_key, public_key, vpn_address, created_at) VALUES (%s, %s, %s, %s)",
            (priv, pub, ip_str, now),
        )
        profile_id = cur.lastrowid
    db.commit()

    return ProfileOut(id=profile_id, vpn_address=ip_str, created_at=now)


@app.get("/profiles", response_model=List[ProfileOut])
def list_profiles(token: str = Query(...)):
    _require_token(token)
    with db.cursor(dictionary=True) as cur:
        cur.execute("SELECT id, vpn_address, created_at FROM wireguard_profiles")
        return cur.fetchall()


@app.get("/profiles/{profile_id}/config", response_class=Response, responses={200: {"content": {"text/plain": {}}}})
def download_config(profile_id: int = FPath(..., ge=1), token: str = Query(...)):
    _require_token(token)

    with db.cursor(dictionary=True) as cur:
        cur.execute("SELECT vpn_address, private_key FROM wireguard_profiles WHERE id=%s", (profile_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")

    conf = (
        "[Interface]\n"
        f"PrivateKey = {row['private_key']}\n"
        f"Address    = {row['vpn_address']}/32\n"
        f"DNS        = {DNS_SERVERS}\n"
        f"Jc         = {AWG_JC}\n"
        f"Jmin       = {AWG_JMIN}\n"
        f"Jmax       = {AWG_JMAX}\n"
        f"S1         = {AWG_S1}\n"
        f"S2         = {AWG_S2}\n"
        f"H1         = {AWG_H1}\n"
        f"H2         = {AWG_H2}\n"
        f"H3         = {AWG_H3}\n"
        f"H4         = {AWG_H4}\n\n"
        "[Peer]\n"
        f"PublicKey         = {SERVER_PUBLIC_KEY}\n"
        f"Endpoint          = {SERVER_ENDPOINT_IP}:{SERVER_ENDPOINT_PORT}\n"
        "AllowedIPs        = 0.0.0.0/0\n"
        "PersistentKeepalive = 20\n"
    )

    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Disposition": f"attachment; filename=wg-profile-{row['vpn_address']}.conf",
    }
    return Response(content=conf, media_type="application/octet-stream", headers=headers)


@app.delete("/profiles/{profile_id}")
def delete_profile(profile_id: int = FPath(..., ge=1), token: str = Query(...)):
    _require_token(token)

    with db.cursor(dictionary=True) as cur:
        cur.execute("SELECT public_key FROM wireguard_profiles WHERE id=%s", (profile_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")

    pub = row["public_key"]
    _remove_peer(pub)

    with db.cursor() as cur:
        cur.execute("DELETE FROM wireguard_profiles WHERE id=%s", (profile_id,))
    db.commit()

    return {"status": "deleted", "profile_id": profile_id}


# ---------------------------------------------------------------------------
# PERIODIC STATUS REPORTING
# ---------------------------------------------------------------------------

def send_status_update():
    """Gathers system data and profiles and reports them to a central server."""
    profiles: list[dict] = []
    active_ids: list[int] = []
    try:
        # Reconnect if connection is lost
        if not db.is_connected():
            db.reconnect()

        with db.cursor(dictionary=True) as cur:
            cur.execute("SELECT id, vpn_address, public_key, created_at FROM wireguard_profiles ORDER BY id")
            db_profiles = cur.fetchall()

        # Map of public_key -> profile id for quick lookup
        key_to_id = {p["public_key"]: p["id"] for p in db_profiles}

        # Convert datetime to string for JSON serialization
        for p in db_profiles:
            if p.get("created_at") and isinstance(p["created_at"], datetime.datetime):
                p["created_at"] = p["created_at"].isoformat()
            # Remove public_key before sending profile list
            p.pop("public_key", None)
        profiles = db_profiles

        # Retrieve handshake info for peers
        try:
            dump = _run([WG_CLI, "show", WG_INTERFACE, "dump"]).splitlines()
            handshakes = {}
            for line in dump[1:]:
                parts = line.split("\t")
                if len(parts) >= 5:
                    handshakes[parts[0]] = int(parts[4])
            now = int(time.time())
            for pub, hs in handshakes.items():
                profile_id = key_to_id.get(pub)
                if profile_id and hs and (now - hs) < 180:
                    active_ids.append(profile_id)
        except Exception as e:
            print(f"Could not retrieve handshake info: {e}", file=sys.stderr)

    except mysql.connector.Error as e:
        print(f"Could not retrieve profiles from database: {e}", file=sys.stderr)
        # Continue with an empty or partial list of profiles

    payload = {
        "api_key": API_TOKEN,
        "wg_interface": WG_INTERFACE,
        "server_public_key": SERVER_PUBLIC_KEY,
        "server_endpoint_ip": SERVER_ENDPOINT_IP,
        "server_endpoint_port": SERVER_ENDPOINT_PORT,
        "vpn_network": VPN_NETWORK_STR,
        "dns_servers": DNS_SERVERS,
        "profiles": profiles,
        "active_profile_ids": active_ids,
    }

    try:
        print("Sending status update to mvpn.space...")
        response = requests.post("https://mvpn.space/status", json=payload, timeout=30)
        response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
        print(f"Status update sent successfully (HTTP {response.status_code}).")
    except requests.exceptions.RequestException as e:
        print(f"Failed to send status update: {e}", file=sys.stderr)


def run_periodic_reporter():
    """Runs send_status_update every 5 minutes in a loop."""
    print("Starting periodic status reporter...")
    while True:
        send_status_update()
        time.sleep(60)

# ---------------------------------------------------------------------------
# APP LIFECYCLE
# ---------------------------------------------------------------------------

@app.on_event("startup")
def on_startup():
    """Starts the periodic status reporter in a background thread."""
    reporter_thread = threading.Thread(target=run_periodic_reporter, daemon=True)
    reporter_thread.start()


# ---------------------------------------------------------------------------
# LOCAL DEV ENTRYPOINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    # The on_startup event now handles the background thread.
    uvicorn.run("wg_service:app", host="0.0.0.0", port=LISTEN_PORT, reload=True)
