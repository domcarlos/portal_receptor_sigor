"""API Routes for Portal do Receptor"""
import time
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from app.models import (CredentialCreate, CredentialUpdate, ReceiveMTRsRequest,
                         ReceiveMTRsResponse, MTRReceiveResult, MTRDetails, MTRResiduo)
from app.sigor_client import SigorClient, SigorAPIError
from app.mtr_manager_client import MTRManagerClient
from app import database
from typing import Optional

logger = logging.getLogger(__name__)
router = APIRouter()
_mtr_manager = None
_sigor_clients: dict[int, SigorClient] = {}


def get_mtr_manager():
    global _mtr_manager
    if _mtr_manager is None:
        _mtr_manager = MTRManagerClient()
    return _mtr_manager


async def get_sigor_for_credential(credential_id: int) -> SigorClient:
    if credential_id in _sigor_clients:
        client = _sigor_clients[credential_id]
        if client.token and time.time() < client.token_expiry:
            return client
    cred = await database.get_credential(credential_id)
    if not cred:
        raise HTTPException(status_code=404, detail="Credencial nao encontrada")
    client = SigorClient()
    try:
        await client.authenticate(cred["login"].replace(".", "").replace("-", ""), cred["senha"], cred["unidade_codigo"])
    except SigorAPIError as e:
        raise HTTPException(status_code=502, detail=f"Falha autenticacao SIGOR: {e.message}")
    _sigor_clients[credential_id] = client
    return client


# === CREDENTIALS CRUD ===
@router.get("/credentials")
async def list_credentials():
    creds = await database.list_credentials()
    for c in creds:
        c.pop("senha", None)
    return creds

@router.post("/credentials", status_code=201)
async def create_credential(data: CredentialCreate):
    cred = await database.create_credential(data.model_dump())
    cred.pop("senha", None)
    return cred

@router.put("/credentials/{cred_id}")
async def update_credential(cred_id: int, data: CredentialUpdate):
    existing = await database.get_credential(cred_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Credencial nao encontrada")
    update_data = {k: v for k, v in data.model_dump().items() if v is not None}
    cred = await database.update_credential(cred_id, update_data)
    cred.pop("senha", None)
    _sigor_clients.pop(cred_id, None)
    return cred

@router.delete("/credentials/{cred_id}", status_code=204)
async def delete_credential(cred_id: int):
    existing = await database.get_credential(cred_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Credencial nao encontrada")
    await database.delete_credential(cred_id)
    _sigor_clients.pop(cred_id, None)

@router.post("/credentials/{cred_id}/test")
async def test_credential(cred_id: int):
    try:
        await get_sigor_for_credential(cred_id)
        return {"success": True, "message": "Autenticacao SIGOR bem-sucedida"}
    except HTTPException as e:
        return {"success": False, "message": e.detail}


# === MTR MANAGER PROXY ===
@router.get("/mtrs")
async def list_mtrs(receiver_name: Optional[str] = None, receiver_id: Optional[int] = None,
                    status: Optional[str] = None, issuer: Optional[str] = None,
                    generator_name: Optional[str] = None, hauler_name: Optional[str] = None,
                    material_name: Optional[str] = None, number: Optional[str] = None,
                    planned_date_at: Optional[str] = None, planned_date_after: Optional[str] = None,
                    planned_date_before: Optional[str] = None, page: int = 1, page_size: int = 100):
    client = get_mtr_manager()
    return await client.list_manifests(
        receiver_name=receiver_name, receiver_id=receiver_id, status=status,
        issuer=issuer, generator_name=generator_name, hauler_name=hauler_name,
        material_name=material_name, number=number, planned_date_at=planned_date_at,
        planned_date_after=planned_date_after, planned_date_before=planned_date_before,
        page=page, page_size=page_size)


# === SIGOR DETAILS ===
@router.get("/mtrs/{numero}/details", response_model=MTRDetails)
async def get_mtr_details(numero: str, credential_id: int):
    client = await get_sigor_for_credential(credential_id)
    try:
        m = await client.get_manifesto(numero)
    except SigorAPIError as e:
        raise HTTPException(status_code=502, detail=e.message)
    return _parse_manifesto(m)


def _parse_manifesto(m):
    residuos = []
    for r in m.get("listaManifestoResiduo", []):
        residuos.append(MTRResiduo(
            res_codigo_ibama=r["residuo"]["resCodigoIbama"], res_descricao=r["residuo"]["resDescricao"],
            quantidade=r.get("marQuantidade", 0), quantidade_recebida=r.get("marQuantidadeRecebida"),
            unidade_codigo=r["unidade"]["uniCodigo"], unidade_sigla=r["unidade"]["uniSigla"],
            tratamento_codigo=r["tratamento"]["traCodigo"], tratamento_descricao=r["tratamento"]["traDescricao"],
            estado_codigo=r["tipoEstado"]["tieCodigo"], estado_descricao=r["tipoEstado"]["tieDescricao"],
            acondicionamento_codigo=r["tipoAcondicionamento"]["tiaCodigo"], acondicionamento_descricao=r["tipoAcondicionamento"]["tiaDescricao"],
            classe_codigo=r["classe"]["claCodigo"], classe_descricao=r["classe"]["claDescricao"],
            codigo_interno=r.get("codigoInterno")))
    data_exp = m.get("manDataExpedicao")
    data_exp_str = None
    if data_exp and isinstance(data_exp, (int, float)):
        try: data_exp_str = datetime.fromtimestamp(data_exp / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        except: data_exp_str = str(data_exp)
    sit = m.get("situacaoManifesto", {})
    data_rec = m.get("manDataRecebimentoDestinador")
    return MTRDetails(
        numero=m["manNumero"], data_expedicao=data_exp_str,
        situacao_codigo=sit.get("simCodigo", 0), situacao_descricao=sit.get("simDescricao", "desconhecido"),
        responsavel_emissao=m.get("manResponsavel"), motorista=m.get("manNomeMotorista"),
        placa=m.get("manPlacaVeiculo"), observacao=m.get("manObservacao"),
        gerador_nome=m.get("parceiroGerador", {}).get("parDescricao", ""),
        gerador_cnpj=m.get("parceiroGerador", {}).get("parCnpj", ""),
        transportador_nome=m.get("parceiroTransportador", {}).get("parDescricao", ""),
        transportador_cnpj=m.get("parceiroTransportador", {}).get("parCnpj", ""),
        destinador_nome=m.get("parceiroDestinador", {}).get("parDescricao", ""),
        destinador_cnpj=m.get("parceiroDestinador", {}).get("parCnpj", ""),
        responsavel_recebimento=m.get("manResponsavelRecebimento"),
        data_recebimento=str(data_rec) if data_rec else None, residuos=residuos)


# === RECEIVE MTRs ===
@router.post("/mtrs/receive", response_model=ReceiveMTRsResponse)
async def receive_mtrs(req: ReceiveMTRsRequest):
    client = await get_sigor_for_credential(req.credential_id)
    data_rec_ms = int(time.time() * 1000)
    if req.data_recebimento:
        try: data_rec_ms = int(datetime.fromisoformat(req.data_recebimento).timestamp() * 1000)
        except ValueError: pass

    results, batch_payload, batch_mtr_map = [], [], {}
    for numero in req.mtr_numbers:
        try:
            manifesto = await client.get_manifesto(numero)
        except SigorAPIError as e:
            results.append(MTRReceiveResult(mtr_numero=numero, success=False, message=f"Erro: {e.message}"))
            continue
        sit = manifesto.get("situacaoManifesto", {})
        if sit.get("simCodigo", 0) != 1:
            results.append(MTRReceiveResult(mtr_numero=numero, success=False,
                           message=f"Nao pendente (status: {sit.get('simDescricao', '?')})",
                           situacao_anterior=sit.get("simDescricao")))
            continue
        override = (req.overrides or {}).get(numero)
        item = client.build_receive_payload(
            manifesto=manifesto, responsavel=req.responsavel_recebimento, data_recebimento_ms=data_rec_ms,
            motorista_override=override.motorista if override else None,
            placa_override=override.placa if override else None,
            quantidade_recebida_override=override.quantidade_recebida if override else None,
            justificativa=override.justificativa if override else None)
        batch_payload.append(item)
        batch_mtr_map[numero] = sit.get("simDescricao")

    if batch_payload:
        try:
            response = await client.receber_manifesto_lote(batch_payload)
            for resp_item in response.get("objetoResposta", []):
                numero = resp_item.get("manNumero", "?")
                valido = resp_item.get("restResponseValido", False)
                msg = resp_item.get("restResponseMensagem", "")
                residuo_errors = [r.get("restResponseMensagem", "") for r in resp_item.get("listaManifestoResiduo", []) if not r.get("restResponseValido", True)]
                if valido and not residuo_errors:
                    results.append(MTRReceiveResult(mtr_numero=numero, success=True, message=f"Recebido: {msg}", situacao_anterior=batch_mtr_map.get(numero)))
                else:
                    results.append(MTRReceiveResult(mtr_numero=numero, success=False, message=f"Rejeitado: {'; '.join(residuo_errors) or msg}", situacao_anterior=batch_mtr_map.get(numero)))
        except SigorAPIError as e:
            for numero in batch_mtr_map:
                results.append(MTRReceiveResult(mtr_numero=numero, success=False, message=f"Erro lote: {e.message}", situacao_anterior=batch_mtr_map.get(numero)))

    sc = sum(1 for r in results if r.success)
    return ReceiveMTRsResponse(total=len(req.mtr_numbers), success_count=sc, error_count=len(results)-sc, skipped_count=len(req.mtr_numbers)-len(results), results=results)
