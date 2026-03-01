from fastapi import APIRouter, Query
from typing import Optional
from app.mtr_manager_client import MTRManagerClient

mtr_router = APIRouter(tags=["mtr"])
_mtr_client = None

def get_mtr_client():
    global _mtr_client
    if _mtr_client is None:
        _mtr_client = MTRManagerClient()
    return _mtr_client

@mtr_router.get("/coletas")
async def list_coletas(
    receiver_name: Optional[str] = Query(None),
    generator_name: Optional[str] = Query(None),
    hauler_name: Optional[str] = Query(None),
    material_name: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    issuer: Optional[str] = Query(None),
    number: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
):
    client = get_mtr_client()
    return await client.list_manifests(
        receiver_name=receiver_name, generator_name=generator_name,
        hauler_name=hauler_name, material_name=material_name,
        status=status, issuer=issuer, number=number,
        page=page, page_size=page_size,
    )
