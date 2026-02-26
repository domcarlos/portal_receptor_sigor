"""SQLite database for credentials persistence"""
import aiosqlite
import json
import os
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", "/tmp/portal_receptor.db")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                orgao TEXT NOT NULL,
                unidade TEXT NOT NULL,
                unidade_codigo INTEGER NOT NULL,
                login TEXT NOT NULL,
                senha TEXT NOT NULL,
                responsaveis TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        await db.commit()


async def list_credentials():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM credentials ORDER BY id DESC")
        rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]


async def get_credential(cred_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM credentials WHERE id = ?", (cred_id,))
        row = await cursor.fetchone()
        return _row_to_dict(row) if row else None


async def create_credential(data: dict):
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO credentials (orgao, unidade, unidade_codigo, login, senha, responsaveis, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (data["orgao"], data["unidade"], data["unidade_codigo"], data["login"],
             data["senha"], json.dumps(data.get("responsaveis", [])), now, now),
        )
        await db.commit()
        return await get_credential(cursor.lastrowid)


async def update_credential(cred_id: int, data: dict):
    now = datetime.utcnow().isoformat()
    fields, values = [], []
    for key in ("orgao", "unidade", "unidade_codigo", "login", "senha", "responsaveis"):
        if key in data:
            fields.append(f"{key} = ?")
            values.append(json.dumps(data[key]) if key == "responsaveis" else data[key])
    if not fields:
        return await get_credential(cred_id)
    fields.append("updated_at = ?")
    values.extend([now, cred_id])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE credentials SET {', '.join(fields)} WHERE id = ?", values)
        await db.commit()
    return await get_credential(cred_id)


async def delete_credential(cred_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM credentials WHERE id = ?", (cred_id,))
        await db.commit()


def _row_to_dict(row):
    if row is None:
        return None
    d = dict(row)
    d["responsaveis"] = json.loads(d["responsaveis"])
    return d
