"""FastAPI сервис управления Xray (VLESS + Reality)
===================================================

API без учётных записей. Все операции защищены единым API_TOKEN.
Поддерживаемые операции:
    * POST   /clients      — создать клиента
    * GET    /clients      — список клиентов
    * GET    /clients/{id}/config — VLESS ссылка
    * DELETE /clients/{id} — удалить клиента
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import List

import mysql.connector
from fastapi import FastAPI, HTTPException, Query, Path as FPath
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# ENVIRONMENT
# ---------------------------------------------------------------------------

API_TOKEN: str = os.getenv("API_TOKEN", "ReplaceMe")

MYSQL_HOST: str = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_DB: str = os.getenv("MYSQL_DB", "xray_panel")
MYSQL_USER: str = os.getenv("MYSQL_USER", "xray_user")
MYSQL_PASS: str = os.getenv("MYSQL_PASSWORD", "xray_pass")

XRAY_CONFIG: Path = Path(os.getenv("XRAY_CONFIG", "/usr/local/etc/xray/config.json"))
XRAY_DOMAIN: str = os.getenv("XRAY_DOMAIN", "example.com")
XRAY_PORT: int = int(os.getenv("XRAY_PORT", "443"))
XRAY_PUBLIC_KEY: str = os.getenv("XRAY_PUBLIC_KEY", "<pubkey>")
XRAY_SNI: str = os.getenv("XRAY_SNI", XRAY_DOMAIN)
XRAY_SHORT_ID: str = os.getenv("XRAY_SHORT_ID", "0000000000000000")

# ---------------------------------------------------------------------------
# DATABASE
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
        CREATE TABLE IF NOT EXISTS xray_clients (
            id INT PRIMARY KEY AUTO_INCREMENT,
            uuid CHAR(36) NOT NULL UNIQUE,
            label VARCHAR(255) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """
    )

# ---------------------------------------------------------------------------
# APP
# ---------------------------------------------------------------------------

app = FastAPI(title="Xray VLESS API", version="1.0.0")


def _require_token(token: str | None):
    if token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing token")


def _restart_xray():
    subprocess.run(["systemctl", "restart", "xray"], check=True)


def _sync_config():
    """Пересобирает clients в config.json из БД и рестартует Xray."""
    with db.cursor(dictionary=True) as cur:
        cur.execute("SELECT uuid, label FROM xray_clients ORDER BY id")
        rows = cur.fetchall()

    if XRAY_CONFIG.exists():
        with open(XRAY_CONFIG, "r", encoding="utf-8") as fh:
            config = json.load(fh)
    else:
        raise HTTPException(status_code=500, detail="Xray config not found")

    clients = [
        {"id": r["uuid"], "flow": "xtls-rprx-vision", "email": r["label"]}
        for r in rows
    ]

    try:
        config["inbounds"][0]["settings"]["clients"] = clients
    except (KeyError, IndexError) as exc:
        raise HTTPException(status_code=500, detail=f"Invalid Xray config: {exc}")

    with open(XRAY_CONFIG, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)

    _restart_xray()


class ClientOut(BaseModel):
    id: int
    uuid: str
    label: str
    created_at: str


@app.post("/clients", response_model=ClientOut)
def create_client(label: str = Query(...), token: str = Query(...)):
    _require_token(token)
    new_uuid = str(uuid.uuid4())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO xray_clients (uuid, label) VALUES (%s,%s)",
            (new_uuid, label),
        )
        client_id = cur.lastrowid

    _sync_config()

    with db.cursor(dictionary=True) as cur:
        cur.execute(
            "SELECT id, uuid, label, created_at FROM xray_clients WHERE id=%s",
            (client_id,),
        )
        row = cur.fetchone()

    return row


@app.get("/clients", response_model=List[ClientOut])
def list_clients(token: str = Query(...)):
    _require_token(token)
    with db.cursor(dictionary=True) as cur:
        cur.execute("SELECT id, uuid, label, created_at FROM xray_clients ORDER BY id")
        rows = cur.fetchall()
    return rows


@app.get("/clients/{client_id}/config")
def client_config(client_id: int = FPath(..., ge=1), token: str = Query(...)):
    _require_token(token)
    with db.cursor(dictionary=True) as cur:
        cur.execute("SELECT uuid, label FROM xray_clients WHERE id=%s", (client_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Client not found")

    link = (
        f"vless://{row['uuid']}@{XRAY_DOMAIN}:{XRAY_PORT}?type=tcp&security=reality"
        f"&fp=chrome&pbk={XRAY_PUBLIC_KEY}&sni={XRAY_SNI}&sid={XRAY_SHORT_ID}"
        f"&flow=xtls-rprx-vision#{row['label']}"
    )
    return {"id": client_id, "link": link}


@app.delete("/clients/{client_id}")
def delete_client(client_id: int = FPath(..., ge=1), token: str = Query(...)):
    _require_token(token)
    with db.cursor() as cur:
        cur.execute("DELETE FROM xray_clients WHERE id=%s", (client_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Client not found")

    _sync_config()
    return {"status": "deleted", "id": client_id}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("wg_service:app", host="0.0.0.0", port=int(os.getenv("API_PORT", "8080")))

