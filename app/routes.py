"""API Routes for Portal do Receptor"""
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.mtr_manager_client import MTRManagerClient
from app import database
from typing import Optional, List

logger = logging.getLogger(__name__)
router = APIRouter()

_mtr_manager = None


def get_mtr_manager():
    global _mtr_manager
    if _mtr_manager is None:
        _mtr_manager = MTRManagerClient()
    return _mtr_manager


# =====================
# MTR MANAGER PROXY
# =====================

@router.get("/mtrs")
async def list_mtrs(
    receiver_name: Optional[str] = None,
    status: Optional[str] = None,
    issuer: Optional[str] = None,
    generator_name: Optional[str] = None,
    hauler_name: Optional[str] = None,
    material_name: Optional[str] = None,
    number: Optional[str] = None,
    planned_date_at: Optional[str] = None,
    page: int = 1,
    page_size: int = 100,
):
    """Proxy to MTR Manager GET /manifests with filters."""
    client = get_mtr_manager()
    data = await client.list_manifests(
        receiver_name=receiver_name,
        status=status,
        issuer=issuer,
        generator_name=generator_name,
        hauler_name=hauler_name,
        material_name=material_name,
        number=number,
        planned_date_at=planned_date_at,
        page=page,
        page_size=page_size,
    )

    # Enrich results with saved metadata (motorista, placa)
    collect_ids = [r["collect_id"] for r in data.get("results", []) if r.get("collect_id")]
    if collect_ids:
        metadata = await database.get_bulk_collection_metadata(collect_ids)
        for r in data["results"]:
            cid = r.get("collect_id")
            if cid and cid in metadata:
                r["motorista"] = metadata[cid].get("motorista", "")
                r["placa"] = metadata[cid].get("placa", "")
            else:
                r["motorista"] = ""
                r["placa"] = ""
    else:
        for r in data.get("results", []):
            r["motorista"] = ""
            r["placa"] = ""

    return data


# =====================
# COLLECTION METADATA
# =====================

class MetadataUpdate(BaseModel):
    motorista: Optional[str] = None
    placa: Optional[str] = None
    observacao: Optional[str] = None


@router.put("/coletas/{collect_id}/metadata")
async def update_collection_metadata(collect_id: str, data: MetadataUpdate):
    """Save motorista/placa for a collection."""
    update_data = {k: v for k, v in data.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="Nenhum campo para atualizar")
    result = await database.upsert_collection_metadata(collect_id, update_data)
    return result


class BulkMetadataUpdate(BaseModel):
    collect_ids: List[str]
    motorista: Optional[str] = None
    placa: Optional[str] = None


@router.put("/coletas/metadata/bulk")
async def bulk_update_metadata(data: BulkMetadataUpdate):
    """Save motorista/placa for multiple collections at once."""
    update_data = {}
    if data.motorista is not None:
        update_data["motorista"] = data.motorista
    if data.placa is not None:
        update_data["placa"] = data.placa
    if not update_data:
        raise HTTPException(status_code=400, detail="Nenhum campo para atualizar")
    results = []
    for cid in data.collect_ids:
        r = await database.upsert_collection_metadata(cid, update_data)
        results.append(r)
    return {"updated": len(results), "results": results}
