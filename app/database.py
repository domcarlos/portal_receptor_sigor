"""SQLite database for credentials + collection metadata persistence"""
import aiosqlite
import json
import os
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", "portal_receptor.db")

# Single persistent connection to avoid "threads can only be started once"
_db_connection = None


async def get_db():
    global _db_connection
    if _db_connection is None:
        _db_connection = await aiosqlite.connect(DB_PATH)
        _db_connection.row_factory = aiosqlite.Row
    return _db_connection


async def close_db():
    global _db_connection
    if _db_connection is not None:
        await _db_connection.close()
        _db_connection = None


async def init_db():
    db = await get_db()
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
    await db.execute("""
        CREATE TABLE IF NOT EXISTS collection_metadata (
            collect_id TEXT PRIMARY KEY,
            motorista TEXT DEFAULT '',
            placa TEXT DEFAULT '',
            peso_coletado REAL DEFAULT 0,
            observacao TEXT DEFAULT '',
            updated_at TEXT NOT NULL
        )
    """)
    # Migration: add peso_coletado column if table existed before this update
    try:
        await db.execute("ALTER TABLE collection_metadata ADD COLUMN peso_coletado REAL DEFAULT 0")
    except Exception:
        pass  # Column already exists
    await db.commit()


async def list_credentials():
    db = await get_db()
    cursor = await db.execute("SELECT * FROM credentials ORDER BY id DESC")
    rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


async def get_credential(cred_id: int):
    db = await get_db()
    cursor = await db.execute("SELECT * FROM credentials WHERE id = ?", (cred_id,))
    row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


async def create_credential(data: dict):
    now = datetime.utcnow().isoformat()
    db = await get_db()
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
    db = await get_db()
    await db.execute(f"UPDATE credentials SET {', '.join(fields)} WHERE id = ?", values)
    await db.commit()
    return await get_credential(cred_id)


async def delete_credential(cred_id: int):
    db = await get_db()
    await db.execute("DELETE FROM credentials WHERE id = ?", (cred_id,))
    await db.commit()


def _row_to_dict(row):
    if row is None:
        return None
    d = dict(row)
    d["responsaveis"] = json.loads(d["responsaveis"])
    return d


# === COLLECTION METADATA ===

async def get_bulk_collection_metadata(collect_ids: list):
    if not collect_ids:
        return {}
    db = await get_db()
    placeholders = ",".join(["?" for _ in collect_ids])
    cursor = await db.execute(
        f"SELECT * FROM collection_metadata WHERE collect_id IN ({placeholders})",
        collect_ids,
    )
    rows = await cursor.fetchall()
    return {row["collect_id"]: dict(row) for row in rows}


async def upsert_collection_metadata(collect_id: str, data: dict):
    now = datetime.utcnow().isoformat()
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM collection_metadata WHERE collect_id = ?", (collect_id,)
    )
    existing = await cursor.fetchone()
    if existing:
        fields, values = [], []
        for key in ("motorista", "placa", "peso_coletado", "observacao"):
            if key in data:
                fields.append(f"{key} = ?")
                values.append(data[key])
        if fields:
            fields.append("updated_at = ?")
            values.extend([now, collect_id])
            await db.execute(
                f"UPDATE collection_metadata SET {', '.join(fields)} WHERE collect_id = ?",
                values,
            )
    else:
        await db.execute(
            """INSERT INTO collection_metadata (collect_id, motorista, placa, peso_coletado, observacao, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (collect_id, data.get("motorista", ""), data.get("placa", ""),
             data.get("peso_coletado", 0), data.get("observacao", ""), now),
        )
    await db.commit()
    cursor2 = await db.execute(
        "SELECT * FROM collection_metadata WHERE collect_id = ?", (collect_id,)
    )
    row = await cursor2.fetchone()
    return dict(row) if row else None
