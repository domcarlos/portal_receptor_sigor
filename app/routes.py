"""API Routes for Portal do Receptor"""
import logging
import uuid
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from app.mtr_manager_client import MTRManagerClient
from app.baixa_processor import process_baixa_batch, request_cancel
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
# STATS (BIG NUMBERS)
# =====================

@router.get("/stats")
async def get_stats(receiver_name: Optional[str] = None):
    """Compute real totals for the dashboard big numbers."""
    client = get_mtr_manager()

    # 1) Total emitted (from MTR Manager, just 1 record to get count)
    data = await client.list_manifests(
        receiver_name=receiver_name,
        status="issued",
        page=1,
        page_size=1,
    )
    total_emitidos = data.get("pagination", {}).get("count", 0)

    # 2) Count completos from local DB (have both motorista AND placa)
    completos_count = await database.count_completos()

    # 3) Incompletos = total emitted - completos in local DB
    incompletos = max(0, total_emitidos - completos_count)

    # 4) Processando / falha / validado from local validation_jobs table
    job_stats = await database.count_validation_jobs()

    return {
        "pendente_baixa": total_emitidos,
        "incompletos": incompletos,
        "completos": completos_count,
        "processando": job_stats.get("processando", 0),
        "falha": job_stats.get("falha", 0),
        "validado": job_stats.get("validado", 0),
    }


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

    # Enrich results with saved metadata (motorista, placa, peso_coletado)
    collect_ids = [r["collect_id"] for r in data.get("results", []) if r.get("collect_id")]
    if collect_ids:
        metadata = await database.get_bulk_collection_metadata(collect_ids)
        for r in data["results"]:
            cid = r.get("collect_id")
            if cid and cid in metadata:
                r["motorista"] = metadata[cid].get("motorista", "")
                r["placa"] = metadata[cid].get("placa", "")
                r["peso_coletado"] = metadata[cid].get("peso_coletado", 0)
            else:
                r["motorista"] = ""
                r["placa"] = ""
                r["peso_coletado"] = 0
    else:
        for r in data.get("results", []):
            r["motorista"] = ""
            r["placa"] = ""
            r["peso_coletado"] = 0

    return data


# =====================
# BAIXA DE MTRs (SIGOR)
# =====================

class BaixaItem(BaseModel):
    collect_id: str
    mtr_number: str
    motorista: str
    placa: str
    peso_coletado: float = 0


class BaixaRequest(BaseModel):
    """Full baixa request with MTR data from frontend."""
    items: List[BaixaItem]


@router.post("/baixa")
async def trigger_baixa(request: BaixaRequest, background_tasks: BackgroundTasks):
    """
    Trigger SIGOR baixa for selected MTRs.
    
    - Validates credentials exist and have responsavel
    - Validates all items have motorista + placa + mtr_number
    - Creates validation_jobs as 'processando'
    - Creates batch_progress for tracking
    - Kicks off background processor with rate limiting
    - Returns immediately with batch_id for polling
    """
    # 1) Check credentials
    creds = await database.list_credentials()
    if not creds:
        raise HTTPException(400, "Nenhuma credencial SIGOR cadastrada. Configure em Credenciais.")

    credential = creds[0]
    if not credential.get("login") or not credential.get("senha") or not credential.get("unidade_codigo"):
        raise HTTPException(400, "Credencial incompleta. Verifique login, senha e codigo da unidade.")

    responsaveis = credential.get("responsaveis", [])
    if not responsaveis or not responsaveis[0]:
        raise HTTPException(400, "Nenhum responsavel cadastrado nas credenciais. O nome deve ser identico ao cadastrado no SIGOR.")

    # 2) Validate items
    valid_items = []
    errors = []
    for item in request.items:
        if not item.motorista or not item.placa:
            errors.append(f"MTR {item.mtr_number}: sem motorista/placa")
            continue
        if not item.mtr_number:
            errors.append(f"Coleta {item.collect_id[:12]}: sem numero MTR")
            continue
        valid_items.append(item.model_dump())

    if not valid_items:
        raise HTTPException(400, "Nenhum MTR valido para baixa. " + "; ".join(errors))

    # 3) Create batch and jobs
    batch_id = f"baixa-{uuid.uuid4().hex[:12]}"

    await database.create_batch_progress(batch_id, len(valid_items))

    for item in valid_items:
        await database.create_validation_job(
            collect_id=item["collect_id"],
            mtr_number=item["mtr_number"],
            batch_id=batch_id,
            state="processando",
            message="Na fila, aguardando processamento...",
        )

    # 4) Run in background with rate limiting
    background_tasks.add_task(
        process_baixa_batch,
        batch_id=batch_id,
        credential=credential,
        mtrs=valid_items,
    )

    return {
        "batch_id": batch_id,
        "message": f"Baixa iniciada para {len(valid_items)} MTR(s). Rate limit: ~10 MTRs/min.",
        "total": len(valid_items),
        "errors": errors,
        "estimated_minutes": round(len(valid_items) * 0.27, 1),  # ~6s fetch + ~1s overhead per MTR
    }


@router.post("/baixa/{batch_id}/cancel")
async def cancel_baixa(batch_id: str):
    """Cancel an in-progress baixa batch."""
    progress = await database.get_batch_progress(batch_id)
    if not progress:
        raise HTTPException(404, "Batch nao encontrado")
    if progress["state"] in ("completed", "cancelled", "failed"):
        raise HTTPException(400, f"Batch ja finalizado com estado: {progress['state']}")

    request_cancel(batch_id)
    await database.update_batch_progress(batch_id, {
        "state": "cancelling",
        "message": "Cancelamento solicitado, finalizando MTR atual...",
    })
    return {"message": "Cancelamento solicitado"}


# =====================
# VALIDATION JOBS
# =====================

@router.get("/validation-jobs")
async def list_validation_jobs(state: Optional[str] = None, batch_id: Optional[str] = None):
    """List validation jobs, optionally filtered by state and/or batch."""
    states = [state] if state else None
    jobs = await database.get_validation_jobs(states=states, batch_id=batch_id)
    return {"jobs": jobs, "count": len(jobs)}


@router.delete("/validation-jobs/{collect_id}")
async def remove_validation_job(collect_id: str):
    """Remove a single validation job."""
    await database.delete_validation_job(collect_id)
    return {"message": "Job removido"}


@router.post("/validation-jobs/{collect_id}/retry")
async def retry_validation_job(collect_id: str, background_tasks: BackgroundTasks):
    """Retry a single failed validation job."""
    jobs = await database.get_validation_jobs(states=["falha"])
    job = next((j for j in jobs if j["collect_id"] == collect_id), None)
    if not job:
        raise HTTPException(404, "Job nao encontrado ou nao esta em falha")

    creds = await database.list_credentials()
    if not creds:
        raise HTTPException(400, "Nenhuma credencial cadastrada")

    meta = await database.get_collection_metadata(collect_id)
    motorista = meta.get("motorista", "") if meta else ""
    placa = meta.get("placa", "") if meta else ""
    peso = meta.get("peso_coletado", 0) if meta else 0

    if not motorista or not placa:
        raise HTTPException(400, "Coleta sem motorista/placa")

    batch_id = f"retry-{uuid.uuid4().hex[:8]}"
    await database.create_validation_job(collect_id, job["mtr_number"], batch_id, "processando", "Retentando...")
    await database.create_batch_progress(batch_id, 1)

    item = {
        "collect_id": collect_id,
        "mtr_number": job["mtr_number"],
        "motorista": motorista,
        "placa": placa,
        "peso_coletado": peso,
    }
    background_tasks.add_task(
        process_baixa_batch,
        batch_id=batch_id,
        credential=creds[0],
        mtrs=[item],
    )
    return {"message": "Retry iniciado", "batch_id": batch_id}


# =====================
# BATCH PROGRESS
# =====================

@router.get("/batch-progress")
async def list_batches():
    """List recent batch progress records."""
    batches = await database.list_batch_progress()
    return {"batches": batches}


@router.get("/batch-progress/{batch_id}")
async def get_batch(batch_id: str):
    """Get progress for a specific batch."""
    progress = await database.get_batch_progress(batch_id)
    if not progress:
        raise HTTPException(404, "Batch nao encontrado")
    return progress


# =====================
# CREDENTIALS
# =====================

class CredentialCreate(BaseModel):
    orgao: str
    unidade: str
    unidade_codigo: int
    login: str
    senha: str
    responsaveis: Optional[List[str]] = []


class CredentialUpdate(BaseModel):
    orgao: Optional[str] = None
    unidade: Optional[str] = None
    unidade_codigo: Optional[int] = None
    login: Optional[str] = None
    senha: Optional[str] = None
    responsaveis: Optional[List[str]] = None


@router.get("/credentials")
async def list_credentials():
    return await database.list_credentials()


@router.get("/credentials/{cred_id}")
async def get_credential(cred_id: int):
    cred = await database.get_credential(cred_id)
    if not cred:
        raise HTTPException(status_code=404, detail="Credencial nao encontrada")
    return cred


@router.post("/credentials")
async def create_credential(data: CredentialCreate):
    return await database.create_credential(data.model_dump())


@router.put("/credentials/{cred_id}")
async def update_credential(cred_id: int, data: CredentialUpdate):
    existing = await database.get_credential(cred_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Credencial nao encontrada")
    return await database.update_credential(cred_id, data.model_dump(exclude_unset=True))


@router.delete("/credentials/{cred_id}")
async def delete_credential(cred_id: int):
    existing = await database.get_credential(cred_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Credencial nao encontrada")
    await database.delete_credential(cred_id)
    return {"detail": "Credencial removida"}


# =====================
# COLLECTION METADATA
# =====================

class MetadataUpdate(BaseModel):
    motorista: Optional[str] = None
    placa: Optional[str] = None
    peso_coletado: Optional[float] = None
    observacao: Optional[str] = None


@router.put("/coletas/{collect_id}/metadata")
async def update_collection_metadata(collect_id: str, data: MetadataUpdate):
    """Save motorista/placa/peso_coletado for a collection."""
    update_data = {k: v for k, v in data.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="Nenhum campo para atualizar")
    result = await database.upsert_collection_metadata(collect_id, update_data)
    return result


class BulkMetadataUpdate(BaseModel):
    collect_ids: List[str]
    motorista: Optional[str] = None
    placa: Optional[str] = None
    peso_coletado: Optional[float] = None


@router.put("/coletas/metadata/bulk")
async def bulk_update_metadata(data: BulkMetadataUpdate):
    """Save motorista/placa/peso_coletado for multiple collections at once."""
    update_data = {}
    if data.motorista is not None:
        update_data["motorista"] = data.motorista
    if data.placa is not None:
        update_data["placa"] = data.placa
    if data.peso_coletado is not None:
        update_data["peso_coletado"] = data.peso_coletado
    if not update_data:
        raise HTTPException(status_code=400, detail="Nenhum campo para atualizar")

    results = []
    for cid in data.collect_ids:
        r = await database.upsert_collection_metadata(cid, update_data)
        results.append(r)
    return {"updated": len(results), "results": results}
