"""API Routes for Portal do Receptor"""
import logging, uuid, time
from datetime import datetime
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
    if _mtr_manager is None: _mtr_manager = MTRManagerClient()
    return _mtr_manager

@router.get("/stats")
async def get_stats(receiver_name: Optional[str] = None):
    client = get_mtr_manager()
    data = await client.list_manifests(receiver_name=receiver_name, status="issued", page=1, page_size=1)
    total = data.get("pagination", {}).get("count", 0)
    completos = await database.count_completos()
    jobs = await database.count_validation_jobs()
    return {"pendente_baixa": total, "incompletos": max(0, total - completos), "completos": completos, "processando": jobs.get("processando", 0), "falha": jobs.get("falha", 0), "validado": jobs.get("validado", 0)}

@router.get("/mtrs")
async def list_mtrs(receiver_name: Optional[str] = None, status: Optional[str] = None, issuer: Optional[str] = None, generator_name: Optional[str] = None, hauler_name: Optional[str] = None, material_name: Optional[str] = None, number: Optional[str] = None, planned_date_at: Optional[str] = None, page: int = 1, page_size: int = 100):
    client = get_mtr_manager()
    data = await client.list_manifests(receiver_name=receiver_name, status=status, issuer=issuer, generator_name=generator_name, hauler_name=hauler_name, material_name=material_name, number=number, planned_date_at=planned_date_at, page=page, page_size=page_size)
    cids = [r["collect_id"] for r in data.get("results", []) if r.get("collect_id")]
    if cids:
        meta = await database.get_bulk_collection_metadata(cids)
        for r in data["results"]:
            c = r.get("collect_id")
            if c and c in meta: r["motorista"] = meta[c].get("motorista", ""); r["placa"] = meta[c].get("placa", ""); r["peso_coletado"] = meta[c].get("peso_coletado", 0)
            else: r["motorista"] = ""; r["placa"] = ""; r["peso_coletado"] = 0
    else:
        for r in data.get("results", []): r["motorista"] = ""; r["placa"] = ""; r["peso_coletado"] = 0
    return data

class BaixaItem(BaseModel):
    collect_id: str
    mtr_number: str
    motorista: str
    placa: str
    peso_coletado: float = 0

class BaixaRequest(BaseModel):
    items: List[BaixaItem]
    responsavel: str = ""
    data_recebimento: str = ""

@router.post("/baixa")
async def trigger_baixa(request: BaixaRequest, background_tasks: BackgroundTasks):
    print(f"[BAIXA] {len(request.items)} items, resp={request.responsavel}, date={request.data_recebimento}")
    creds = await database.list_credentials()
    if not creds: raise HTTPException(400, "Nenhuma credencial cadastrada.")
    cred = creds[0]
    if not cred.get("login") or not cred.get("senha") or not cred.get("unidade_codigo"):
        raise HTTPException(400, "Credencial incompleta.")
    resp = request.responsavel.strip()
    if not resp:
        rs = cred.get("responsaveis", [])
        if rs and rs[0]: resp = rs[0]
    if not resp: raise HTTPException(400, "Nenhum responsavel.")
    data_ms = 0
    if request.data_recebimento:
        try: data_ms = int(datetime.fromisoformat(request.data_recebimento).timestamp() * 1000)
        except: data_ms = int(time.time() * 1000)
    else: data_ms = int(time.time() * 1000)
    valid, errors = [], []
    for item in request.items:
        if not item.motorista or not item.placa: errors.append(f"{item.mtr_number}: sem motorista/placa"); continue
        if not item.mtr_number: errors.append(f"{item.collect_id[:12]}: sem MTR"); continue
        valid.append(item.model_dump())
    if not valid: raise HTTPException(400, "Nenhum valido. " + "; ".join(errors))
    batch_id = f"baixa-{uuid.uuid4().hex[:12]}"
    print(f"[BAIXA] batch={batch_id}, {len(valid)} items")
    await database.create_batch_progress(batch_id, len(valid))
    for it in valid: await database.create_validation_job(it["collect_id"], it["mtr_number"], batch_id, "processando", "Na fila...")
    print(f"[BAIXA] Scheduling background task")
    background_tasks.add_task(process_baixa_batch, batch_id=batch_id, credential=cred, mtrs=valid, responsavel=resp, data_recebimento_ms=data_ms)
    return {"batch_id": batch_id, "message": f"Baixa iniciada: {len(valid)} MTR(s).", "total": len(valid), "errors": errors, "estimated_minutes": round(len(valid) * 0.27, 1)}

@router.post("/baixa/{batch_id}/cancel")
async def cancel_baixa(batch_id: str):
    p = await database.get_batch_progress(batch_id)
    if not p: raise HTTPException(404, "Nao encontrado")
    if p["state"] in ("completed", "cancelled", "failed"): raise HTTPException(400, f"Ja finalizado: {p['state']}")
    request_cancel(batch_id)
    await database.update_batch_progress(batch_id, {"state": "cancelling", "message": "Cancelando..."})
    return {"message": "Cancelamento solicitado"}

@router.get("/validation-jobs")
async def list_validation_jobs(state: Optional[str] = None, batch_id: Optional[str] = None):
    states = [state] if state else None
    jobs = await database.get_validation_jobs(states=states, batch_id=batch_id)
    return {"jobs": jobs, "count": len(jobs)}

@router.delete("/validation-jobs/{collect_id}")
async def remove_validation_job(collect_id: str):
    await database.delete_validation_job(collect_id); return {"message": "Removido"}

@router.post("/validation-jobs/{collect_id}/retry")
async def retry_validation_job(collect_id: str, background_tasks: BackgroundTasks):
    jobs = await database.get_validation_jobs(states=["falha"])
    job = next((j for j in jobs if j["collect_id"] == collect_id), None)
    if not job: raise HTTPException(404, "Nao encontrado ou nao em falha")
    creds = await database.list_credentials()
    if not creds: raise HTTPException(400, "Sem credencial")
    meta = await database.get_collection_metadata(collect_id)
    mot = meta.get("motorista", "") if meta else ""
    pla = meta.get("placa", "") if meta else ""
    if not mot or not pla: raise HTTPException(400, "Sem motorista/placa")
    bid = f"retry-{uuid.uuid4().hex[:8]}"
    await database.create_validation_job(collect_id, job["mtr_number"], bid, "processando", "Retentando...")
    await database.create_batch_progress(bid, 1)
    background_tasks.add_task(process_baixa_batch, batch_id=bid, credential=creds[0], mtrs=[{"collect_id": collect_id, "mtr_number": job["mtr_number"], "motorista": mot, "placa": pla, "peso_coletado": meta.get("peso_coletado", 0) if meta else 0}])
    return {"message": "Retry iniciado", "batch_id": bid}

@router.get("/batch-progress")
async def list_batches():
    return {"batches": await database.list_batch_progress()}

@router.get("/batch-progress/{batch_id}")
async def get_batch(batch_id: str):
    p = await database.get_batch_progress(batch_id)
    if not p: raise HTTPException(404, "Nao encontrado")
    return p

class CredentialCreate(BaseModel):
    orgao: str; unidade: str; unidade_codigo: int; login: str; senha: str; responsaveis: Optional[List[str]] = []

class CredentialUpdate(BaseModel):
    orgao: Optional[str] = None; unidade: Optional[str] = None; unidade_codigo: Optional[int] = None; login: Optional[str] = None; senha: Optional[str] = None; responsaveis: Optional[List[str]] = None

@router.get("/credentials")
async def list_creds(): return await database.list_credentials()

@router.get("/credentials/{cred_id}")
async def get_cred(cred_id: int):
    c = await database.get_credential(cred_id)
    if not c: raise HTTPException(404, "Nao encontrada")
    return c

@router.post("/credentials")
async def create_cred(data: CredentialCreate): return await database.create_credential(data.model_dump())

@router.put("/credentials/{cred_id}")
async def update_cred(cred_id: int, data: CredentialUpdate):
    if not await database.get_credential(cred_id): raise HTTPException(404, "Nao encontrada")
    return await database.update_credential(cred_id, data.model_dump(exclude_unset=True))

@router.delete("/credentials/{cred_id}")
async def delete_cred(cred_id: int):
    if not await database.get_credential(cred_id): raise HTTPException(404, "Nao encontrada")
    await database.delete_credential(cred_id); return {"detail": "Removida"}

class MetadataUpdate(BaseModel):
    motorista: Optional[str] = None; placa: Optional[str] = None; peso_coletado: Optional[float] = None; observacao: Optional[str] = None

@router.put("/coletas/{collect_id}/metadata")
async def update_meta(collect_id: str, data: MetadataUpdate):
    d = {k: v for k, v in data.model_dump().items() if v is not None}
    if not d: raise HTTPException(400, "Nenhum campo")
    return await database.upsert_collection_metadata(collect_id, d)

class BulkMetadataUpdate(BaseModel):
    collect_ids: List[str]; motorista: Optional[str] = None; placa: Optional[str] = None; peso_coletado: Optional[float] = None

@router.put("/coletas/metadata/bulk")
async def bulk_meta(data: BulkMetadataUpdate):
    d = {}
    if data.motorista is not None: d["motorista"] = data.motorista
    if data.placa is not None: d["placa"] = data.placa
    if data.peso_coletado is not None: d["peso_coletado"] = data.peso_coletado
    if not d: raise HTTPException(400, "Nenhum campo")
    res = [await database.upsert_collection_metadata(c, d) for c in data.collect_ids]
    return {"updated": len(res), "results": res}
