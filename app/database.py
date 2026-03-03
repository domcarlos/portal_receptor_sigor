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
    await db.execute("""
        CREATE TABLE IF NOT EXISTS validation_jobs (
            collect_id TEXT PRIMARY KEY,
            mtr_number TEXT DEFAULT '',
            batch_id TEXT DEFAULT '',
            state TEXT DEFAULT 'processando',
            message TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS batch_progress (
            batch_id TEXT PRIMARY KEY,
            total INTEGER DEFAULT 0,
            processed INTEGER DEFAULT 0,
            success INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0,
            state TEXT DEFAULT 'pending',
            message TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # Migrations for existing tables
    for sql in [
        "ALTER TABLE collection_metadata ADD COLUMN peso_coletado REAL DEFAULT 0",
        "ALTER TABLE validation_jobs ADD COLUMN batch_id TEXT DEFAULT ''",
    ]:
        try:
            await db.execute(sql)
        except Exception:
            pass
    await db.commit()


# === CREDENTIALS ===

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
        (data["orgao"], data["unidade"], data["unidade_codigo"],
         data["login"], data["senha"],
         json.dumps(data.get("responsaveis", [])), now, now),
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

async def get_collection_metadata(collect_id: str):
    """Get metadata for a single collection."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM collection_metadata WHERE collect_id = ?", (collect_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


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


# === STATS ===

async def count_completos():
    """Count collection_metadata records that have BOTH motorista and placa filled."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT COUNT(*) as cnt FROM collection_metadata WHERE motorista != '' AND placa != ''"
    )
    row = await cursor.fetchone()
    return row["cnt"] if row else 0


async def count_validation_jobs():
    """Count validation jobs by state."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT state, COUNT(*) as cnt FROM validation_jobs GROUP BY state"
    )
    rows = await cursor.fetchall()
    result = {"processando": 0, "falha": 0, "validado": 0}
    for row in rows:
        result[row["state"]] = row["cnt"]
    return result


# === VALIDATION JOBS ===

async def create_validation_job(collect_id: str, mtr_number: str, batch_id: str = "",
                                 state: str = "processando", message: str = ""):
    db = await get_db()
    now = datetime.utcnow().isoformat()
    await db.execute(
        """INSERT INTO validation_jobs (collect_id, mtr_number, batch_id, state, message, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(collect_id) DO UPDATE SET
               mtr_number=excluded.mtr_number, batch_id=excluded.batch_id,
               state=excluded.state, message=excluded.message, updated_at=excluded.updated_at""",
        (collect_id, mtr_number, batch_id, state, message, now, now),
    )
    await db.commit()


async def update_validation_job(collect_id: str, state: str, message: str = ""):
    db = await get_db()
    now = datetime.utcnow().isoformat()
    await db.execute(
        "UPDATE validation_jobs SET state=?, message=?, updated_at=? WHERE collect_id=?",
        (state, message, now, collect_id),
    )
    await db.commit()


async def get_validation_jobs(states: list = None, batch_id: str = None, limit: int = 500):
    db = await get_db()
    conditions, params = [], []
    if states:
        placeholders = ",".join(["?" for _ in states])
        conditions.append(f"state IN ({placeholders})")
        params.extend(states)
    if batch_id:
        conditions.append("batch_id = ?")
        params.append(batch_id)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    cursor = await db.execute(
        f"SELECT * FROM validation_jobs {where} ORDER BY updated_at DESC LIMIT ?", params,
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def delete_validation_job(collect_id: str):
    db = await get_db()
    await db.execute("DELETE FROM validation_jobs WHERE collect_id=?", (collect_id,))
    await db.commit()


async def delete_validation_jobs_by_batch(batch_id: str):
    db = await get_db()
    await db.execute("DELETE FROM validation_jobs WHERE batch_id=?", (batch_id,))
    await db.commit()


# === BATCH PROGRESS ===

async def create_batch_progress(batch_id: str, total: int):
    db = await get_db()
    now = datetime.utcnow().isoformat()
    await db.execute(
        """INSERT INTO batch_progress (batch_id, total, state, created_at, updated_at)
           VALUES (?, ?, 'pending', ?, ?)
           ON CONFLICT(batch_id) DO UPDATE SET total=?, state='pending', updated_at=?""",
        (batch_id, total, now, now, total, now),
    )
    await db.commit()


async def update_batch_progress(batch_id: str, data: dict):
    db = await get_db()
    now = datetime.utcnow().isoformat()
    # Upsert: create if not exists
    existing = await get_batch_progress(batch_id)
    if not existing:
        await db.execute(
            """INSERT INTO batch_progress (batch_id, total, processed, success, failed, state, message, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (batch_id, data.get("total", 0), data.get("processed", 0),
             data.get("success", 0), data.get("failed", 0),
             data.get("state", "pending"), data.get("message", ""), now, now),
        )
    else:
        fields = ["updated_at = ?"]
        values = [now]
        for key in ("total", "processed", "success", "failed", "state", "message"):
            if key in data:
                fields.append(f"{key} = ?")
                values.append(data[key])
        values.append(batch_id)
        await db.execute(
            f"UPDATE batch_progress SET {', '.join(fields)} WHERE batch_id = ?", values,
        )
    await db.commit()


async def get_batch_progress(batch_id: str):
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM batch_progress WHERE batch_id = ?", (batch_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_batch_progress(limit: int = 20):
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM batch_progress ORDER BY created_at DESC LIMIT ?", (limit,)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]
