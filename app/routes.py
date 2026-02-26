"""API Routes for Portal do Receptor"""
import time
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException

from app.models import (
    SigorAuthRequest, SigorAuthResponse,
    MTRDetails, MTRResiduo,
    ReceiveMTRsRequest, ReceiveMTRsResponse, MTRReceiveResult,
)
from app.sigor_client import SigorClient, SigorAPIError

logger = logging.getLogger(__name__)
router = APIRouter()

# In-memory state (MVP - no database)
_sigor_client = None
_reference_lists = None


def get_sigor():
    global _sigor_client
    if _sigor_client is None:
        _sigor_client = SigorClient()
    return _sigor_client


# === Auth ===

@router.post("/auth/sigor", response_model=SigorAuthResponse)
async def auth_sigor(req: SigorAuthRequest):
    """Authenticate with SIGOR and store token."""
    client = get_sigor()
    try:
        token = await client.authenticate(
            cpf=req.cpf.replace(".", "").replace("-", ""),
            senha=req.senha,
            unidade=req.unidade,
        )
        return SigorAuthResponse(success=True, token=token[:50] + "...", message="Autenticado com sucesso")
    except SigorAPIError as e:
        return SigorAuthResponse(success=False, message=f"Falha: {e.message}")


# === Reference Lists ===

@router.get("/sigor/lists")
async def get_reference_lists():
    """Get SIGOR reference lists (cached in memory)."""
    global _reference_lists
    client = get_sigor()
    if not client.token:
        raise HTTPException(status_code=401, detail="Autentique primeiro via POST /api/auth/sigor")
    if _reference_lists:
        return _reference_lists
    try:
        _reference_lists = {
            "classes": await client.get_classes(),
            "unidades": await client.get_unidades(),
            "tratamentos": await client.get_tratamentos(),
            "estados_fisicos": await client.get_estados_fisicos(),
            "acondicionamentos": await client.get_acondicionamentos(),
        }
        return _reference_lists
    except SigorAPIError as e:
        raise HTTPException(status_code=502, detail=f"SIGOR error: {e.message}")


# === MTR Details ===

@router.get("/mtrs/{numero}/details", response_model=MTRDetails)
async def get_mtr_details(numero: str):
    """Query a single MTR from SIGOR (API 13)."""
    client = get_sigor()
    if not client.token:
        raise HTTPException(status_code=401, detail="Autentique primeiro via POST /api/auth/sigor")
    try:
        m = await client.get_manifesto(numero)
    except SigorAPIError as e:
        raise HTTPException(status_code=502, detail=e.message)
    return _parse_manifesto(m)


def _parse_manifesto(m):
    """Parse raw SIGOR manifesto into MTRDetails model."""
    residuos = []
    for r in m.get("listaManifestoResiduo", []):
        residuos.append(MTRResiduo(
            res_codigo_ibama=r["residuo"]["resCodigoIbama"],
            res_descricao=r["residuo"]["resDescricao"],
            quantidade=r.get("marQuantidade", 0),
            quantidade_recebida=r.get("marQuantidadeRecebida"),
            unidade_codigo=r["unidade"]["uniCodigo"],
            unidade_sigla=r["unidade"]["uniSigla"],
            tratamento_codigo=r["tratamento"]["traCodigo"],
            tratamento_descricao=r["tratamento"]["traDescricao"],
            estado_codigo=r["tipoEstado"]["tieCodigo"],
            estado_descricao=r["tipoEstado"]["tieDescricao"],
            acondicionamento_codigo=r["tipoAcondicionamento"]["tiaCodigo"],
            acondicionamento_descricao=r["tipoAcondicionamento"]["tiaDescricao"],
            classe_codigo=r["classe"]["claCodigo"],
            classe_descricao=r["classe"]["claDescricao"],
            codigo_interno=r.get("codigoInterno"),
        ))

    data_exp = m.get("manDataExpedicao")
    data_exp_str = None
    if data_exp and isinstance(data_exp, (int, float)):
        try:
            data_exp_str = datetime.fromtimestamp(data_exp / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OSError):
            data_exp_str = str(data_exp)

    data_rec = m.get("manDataRecebimentoDestinador")
    data_rec_str = str(data_rec) if data_rec else None
    sit = m.get("situacaoManifesto", {})

    return MTRDetails(
        numero=m["manNumero"],
        data_expedicao=data_exp_str,
        situacao_codigo=sit.get("simCodigo", 0),
        situacao_descricao=sit.get("simDescricao", "desconhecido"),
        responsavel_emissao=m.get("manResponsavel"),
        motorista=m.get("manNomeMotorista"),
        placa=m.get("manPlacaVeiculo"),
        observacao=m.get("manObservacao"),
        gerador_nome=m.get("parceiroGerador", {}).get("parDescricao", ""),
        gerador_cnpj=m.get("parceiroGerador", {}).get("parCnpj", ""),
        transportador_nome=m.get("parceiroTransportador", {}).get("parDescricao", ""),
        transportador_cnpj=m.get("parceiroTransportador", {}).get("parCnpj", ""),
        destinador_nome=m.get("parceiroDestinador", {}).get("parDescricao", ""),
        destinador_cnpj=m.get("parceiroDestinador", {}).get("parCnpj", ""),
        responsavel_recebimento=m.get("manResponsavelRecebimento"),
        data_recebimento=data_rec_str,
        residuos=residuos,
    )


# === Receive MTRs in Batch ===

@router.post("/mtrs/receive", response_model=ReceiveMTRsResponse)
async def receive_mtrs(req: ReceiveMTRsRequest):
    """Receive (validate) multiple MTRs in SIGOR via API 14.

    Flow:
    1. For each MTR, query API 13 to get full data
    2. Skip already-received MTRs (simCodigo != 1)
    3. Build payload using data from API 13 + user inputs
    4. Call API 14 (receberManifestoLote) with the batch
    5. Return results per MTR
    """
    client = get_sigor()
    if not client.token:
        raise HTTPException(status_code=401, detail="Autentique primeiro via POST /api/auth/sigor")

    if req.data_recebimento:
        try:
            dt = datetime.fromisoformat(req.data_recebimento)
            data_rec_ms = int(dt.timestamp() * 1000)
        except ValueError:
            data_rec_ms = int(time.time() * 1000)
    else:
        data_rec_ms = int(time.time() * 1000)

    results = []
    batch_payload = []
    batch_mtr_map = {}

    # Step 1: Query each MTR and build batch
    for numero in req.mtr_numbers:
        try:
            manifesto = await client.get_manifesto(numero)
        except SigorAPIError as e:
            results.append(MTRReceiveResult(
                mtr_numero=numero, success=False,
                message=f"Erro ao consultar: {e.message}",
            ))
            continue

        sit = manifesto.get("situacaoManifesto", {})
        sit_codigo = sit.get("simCodigo", 0)
        sit_descricao = sit.get("simDescricao", "desconhecido")

        if sit_codigo != 1:
            results.append(MTRReceiveResult(
                mtr_numero=numero, success=False,
                message=f"MTR nao esta pendente (status: {sit_descricao})",
                situacao_anterior=sit_descricao,
            ))
            continue

        override = (req.overrides or {}).get(numero)
        item = client.build_receive_payload(
            manifesto=manifesto,
            responsavel=req.responsavel_recebimento,
            data_recebimento_ms=data_rec_ms,
            motorista_override=override.motorista if override else None,
            placa_override=override.placa if override else None,
            quantidade_recebida_override=override.quantidade_recebida if override else None,
            justificativa=override.justificativa if override else None,
        )
        batch_payload.append(item)
        batch_mtr_map[numero] = sit_descricao

    # Step 2: Send batch to SIGOR
    if batch_payload:
        try:
            response = await client.receber_manifesto_lote(batch_payload)
            for resp_item in response.get("objetoResposta", []):
                numero = resp_item.get("manNumero", "?")
                valido = resp_item.get("restResponseValido", False)
                msg = resp_item.get("restResponseMensagem", "")

                residuo_errors = []
                for res_resp in resp_item.get("listaManifestoResiduo", []):
                    if not res_resp.get("restResponseValido", True):
                        residuo_errors.append(res_resp.get("restResponseMensagem", "erro residuo"))

                if valido and not residuo_errors:
                    results.append(MTRReceiveResult(
                        mtr_numero=numero, success=True,
                        message=f"Recebido com sucesso: {msg}",
                        situacao_anterior=batch_mtr_map.get(numero),
                    ))
                else:
                    error_detail = "; ".join(residuo_errors) if residuo_errors else msg
                    results.append(MTRReceiveResult(
                        mtr_numero=numero, success=False,
                        message=f"SIGOR rejeitou: {error_detail}",
                        situacao_anterior=batch_mtr_map.get(numero),
                    ))
        except SigorAPIError as e:
            for numero in batch_mtr_map:
                results.append(MTRReceiveResult(
                    mtr_numero=numero, success=False,
                    message=f"Erro no lote: {e.message}",
                    situacao_anterior=batch_mtr_map.get(numero),
                ))

    success_count = sum(1 for r in results if r.success)
    error_count = sum(1 for r in results if not r.success)
    skipped = len(req.mtr_numbers) - len(results)

    return ReceiveMTRsResponse(
        total=len(req.mtr_numbers),
        success_count=success_count,
        error_count=error_count,
        skipped_count=skipped,
        results=results,
    )
