"""SQLite database for credentials persistence (MVP)"""
import aiosqlite
import json
import os
from datetime import datetime
from typing import Optional

DB_PATH = os.getenv("DB_PATH", "portal_receptor.db")


async def get_db():
    """Get database connection."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def init_db():
    """Initialize database tables."""
    db = await get_db()
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                orgao TEXT NOT NULL,
                unidade TEXT NOT NULL,
                login TEXT NOT NULL,
                senha TEXT NOT NULL,
                unidade_codigo INTEGER NOT NULL DEFAULT 0,
                responsaveis TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.commit()
    finally:
        await db.close()


# === CRUD Operations ===

async def create_credential(orgao: str, unidade: str, login: str, senha: str,
                            unidade_codigo: int = 0, responsaveis: list[str] = None) -> dict:
    db = await get_db()
    try:
        resp_json = json.dumps(responsaveis or [])
        now = datetime.utcnow().isoformat()
        cursor = await db.execute(
            """INSERT INTO credentials (orgao, unidade, login, senha, unidade_codigo, responsaveis, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (orgao, unidade, login, senha, unidade_codigo, resp_json, now, now)
        )
        await db.commit()
        return await get_credential(cursor.lastrowid)
    finally:
        await db.close()


async def get_credential(credential_id: int) -> Optional[dict]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM credentials WHERE id = ?", (credential_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return _row_to_dict(row)
    finally:
        await db.close()


async def list_credentials() -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM credentials ORDER BY orgao, unidade")
        rows = await cursor.fetchall()
        return [_row_to_dict(row) for row in rows]
    finally:
        await db.close()


async def update_credential(credential_id: int, orgao: str = None, unidade: str = None,
                            login: str = None, senha: str = None, unidade_codigo: int = None,
                            responsaveis: list[str] = None) -> Optional[dict]:
    db = await get_db()
    try:
        existing = await get_credential(credential_id)
        if not existing:
            return None
        updates = []
        values = []
        if orgao is not None:
            updates.append("orgao = ?")
            values.append(orgao)
        if unidade is not None:
            updates.append("unidade = ?")
            values.append(unidade)
        if login is not None:
            updates.append("login = ?")
            values.append(login)
        if senha is not None:
            updates.append("senha = ?")
            values.append(senha)
        if unidade_codigo is not None:
            updates.append("unidade_codigo = ?")
            values.append(unidade_codigo)
        if responsaveis is not None:
            updates.append("responsaveis = ?")
            values.append(json.dumps(responsaveis))
        if not updates:
            return existing
        updates.append("updated_at = ?")
        values.append(datetime.utcnow().isoformat())
        values.append(credential_id)
        await db.execute(
            f"UPDATE credentials SET {', '.join(updates)} WHERE id = ?",
            values
        )
        await db.commit()
        return await get_credential(credential_id)
    finally:
        await db.close()


async def delete_credential(credential_id: int) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM credentials WHERE id = ?", (credential_id,))
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def get_all_responsaveis() -> list[str]:
    """Get unique list of all responsaveis across all credentials."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT responsaveis FROM credentials")
        rows = await cursor.fetchall()
        all_resp = set()
        for row in rows:
            try:
                resp_list = json.loads(row[0])
                all_resp.update(resp_list)
            except (json.JSONDecodeError, TypeError):
                pass
        return sorted(list(all_resp))
    finally:
        await db.close()


async def get_credential_for_orgao(orgao: str) -> Optional[dict]:
    """Get credential for a specific orgao (first match)."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM credentials WHERE orgao = ? LIMIT 1", (orgao,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return _row_to_dict(row)
    finally:
        await db.close()


def _row_to_dict(row) -> dict:
    d = dict(row)
    try:
        d["responsaveis"] = json.loads(d.get("responsaveis", "[]"))
    except (json.JSONDecodeError, TypeError):
        d["responsaveis"] = []
    return d
