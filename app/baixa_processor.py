"""
Baixa Processor - Orchestrates MTR receiving (baixa) via SIGOR API
Designed for high volume (40k+ MTRs) with rate limiting to avoid bans.

Rate limits based on observed SIGOR behavior:
- Emission: ~10 MTRs/min (per user report)
- We use conservative defaults: 6s between fetches, 10s between batch receives
- All configurable via environment variables
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Optional, Callable
from app.sigor_client import SigorClient, SigorAPIError
from app import database

logger = logging.getLogger(__name__)

# ---------------------
# Rate limit config
# ---------------------
# Delay between individual get_manifesto calls (seconds)
FETCH_DELAY = float(__import__("os").getenv("SIGOR_FETCH_DELAY", "6"))
# Max MTRs per receberManifestoLote call
RECEIVE_BATCH_SIZE = int(__import__("os").getenv("SIGOR_BATCH_SIZE", "10"))
# Delay between batch receive calls (seconds)
RECEIVE_BATCH_DELAY = float(__import__("os").getenv("SIGOR_BATCH_DELAY", "10"))
# Max retries per MTR on transient errors
MAX_RETRIES = int(__import__("os").getenv("SIGOR_MAX_RETRIES", "2"))
# Delay before retry (seconds)
RETRY_DELAY = float(__import__("os").getenv("SIGOR_RETRY_DELAY", "30"))


# ---------------------
# Global cancel flag
# ---------------------
_cancel_flags: dict = {}  # batch_id -> bool


def request_cancel(batch_id: str):
    _cancel_flags[batch_id] = True


def is_cancelled(batch_id: str) -> bool:
    return _cancel_flags.get(batch_id, False)


def clear_cancel(batch_id: str):
    _cancel_flags.pop(batch_id, None)


async def process_baixa_batch(
    batch_id: str,
    credential: dict,
    mtrs: list,
):
    """
    Process a batch of MTRs for baixa with rate limiting.
    
    This runs as a background task and updates validation_jobs in DB.
    
    Args:
        batch_id: Unique batch identifier for progress tracking
        credential: Dict with login, senha, unidade_codigo, responsaveis
        mtrs: List of dicts with: collect_id, mtr_number, motorista, placa, peso_coletado
    """
    total = len(mtrs)
    logger.info(f"[Baixa {batch_id}] Starting batch: {total} MTRs, "
                f"fetch_delay={FETCH_DELAY}s, batch_size={RECEIVE_BATCH_SIZE}, "
                f"batch_delay={RECEIVE_BATCH_DELAY}s")

    # Update batch progress
    await database.update_batch_progress(batch_id, {
        "total": total,
        "processed": 0,
        "success": 0,
        "failed": 0,
        "state": "authenticating",
    })

    # 1) Authenticate with SIGOR
    client = SigorClient()
    try:
        await client.authenticate(
            cpf=credential["login"],
            senha=credential["senha"],
            unidade=credential["unidade_codigo"],
        )
        logger.info(f"[Baixa {batch_id}] SIGOR auth OK")
    except SigorAPIError as e:
        logger.error(f"[Baixa {batch_id}] Auth failed: {e}")
        # Mark all as failed
        for mtr in mtrs:
            await database.update_validation_job(
                mtr["collect_id"], "falha", f"Erro autenticacao SIGOR: {e.message}"
            )
        await database.update_batch_progress(batch_id, {
            "state": "failed",
            "processed": total,
            "failed": total,
            "message": f"Autenticacao falhou: {e.message}",
        })
        await client.close()
        return

    responsavel = credential.get("responsaveis", [""])[0]
    if not responsavel:
        for mtr in mtrs:
            await database.update_validation_job(
                mtr["collect_id"], "falha", "Nenhum responsavel cadastrado"
            )
        await database.update_batch_progress(batch_id, {
            "state": "failed", "processed": total, "failed": total,
            "message": "Nenhum responsavel cadastrado nas credenciais",
        })
        await client.close()
        return

    await database.update_batch_progress(batch_id, {"state": "fetching_details"})

    # 2) Phase 1: Fetch manifesto details with rate limiting
    #    Build receive payloads one by one
    payloads = []       # (collect_id, payload_dict)
    processed = 0
    failed = 0
    now_ms = int(time.time() * 1000)

    for i, mtr in enumerate(mtrs):
        # Check cancel
        if is_cancelled(batch_id):
            logger.info(f"[Baixa {batch_id}] Cancelled at fetch {i}/{total}")
            for remaining in mtrs[i:]:
                await database.update_validation_job(
                    remaining["collect_id"], "falha", "Cancelado pelo usuario"
                )
                failed += 1
            break

        cid = mtr["collect_id"]
        mtr_num = mtr["mtr_number"]

        # Update individual job
        await database.update_validation_job(cid, "processando",
            f"Buscando detalhes ({i+1}/{total})...")

        # Fetch with retry
        details = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                details = await client.get_manifesto(mtr_num)
                break
            except SigorAPIError as e:
                if attempt < MAX_RETRIES and e.status_code in (429, 500, 502, 503):
                    logger.warning(f"[Baixa {batch_id}] MTR {mtr_num} fetch attempt {attempt} failed: {e}, retrying...")
                    await asyncio.sleep(RETRY_DELAY)
                    # Re-auth in case token expired
                    try:
                        await client._ensure_token()
                    except Exception:
                        pass
                else:
                    logger.error(f"[Baixa {batch_id}] MTR {mtr_num} fetch failed: {e}")
                    await database.update_validation_job(cid, "falha", f"Erro ao buscar MTR: {e.message}")
                    failed += 1
                    details = None
                    break

        if details:
            # Check MTR status - must be active to receive
            situacao = details.get("situacaoManifesto", {})
            sim_desc = situacao.get("simDescricao", "")
            # simCodigo: 1=Ativo, 2=Cancelado, 3=Recebido
            sim_code = situacao.get("simCodigo", 0)

            if sim_code == 3 or sim_desc == "Recebido":
                await database.update_validation_job(cid, "falha", "MTR ja recebido no SIGOR")
                failed += 1
            elif sim_code == 2 or sim_desc == "Cancelado":
                await database.update_validation_job(cid, "falha", "MTR cancelado no SIGOR")
                failed += 1
            else:
                # Build payload
                peso = mtr.get("peso_coletado") or None
                if peso and peso <= 0:
                    peso = None

                try:
                    payload = client.build_receive_payload(
                        manifesto=details,
                        responsavel=responsavel,
                        data_recebimento_ms=now_ms,
                        motorista_override=mtr["motorista"],
                        placa_override=mtr["placa"],
                        quantidade_recebida_override=peso,
                    )
                    payloads.append((cid, payload))
                except Exception as e:
                    logger.error(f"[Baixa {batch_id}] MTR {mtr_num} payload build error: {e}")
                    await database.update_validation_job(cid, "falha", f"Erro montando payload: {str(e)}")
                    failed += 1

        processed += 1

        # Update batch progress
        if processed % 10 == 0 or processed == total:
            await database.update_batch_progress(batch_id, {
                "processed": processed,
                "failed": failed,
                "state": "fetching_details",
                "message": f"Buscando detalhes: {processed}/{total}",
            })

        # Rate limit between fetches
        if i < len(mtrs) - 1:
            await asyncio.sleep(FETCH_DELAY)

    # 3) Phase 2: Send receive in batches with rate limiting
    if not payloads:
        logger.info(f"[Baixa {batch_id}] No valid payloads to send")
        await database.update_batch_progress(batch_id, {
            "state": "completed",
            "processed": total,
            "failed": failed,
            "success": 0,
            "message": f"Nenhum MTR valido para recebimento. {failed} falha(s).",
        })
        await client.close()
        clear_cancel(batch_id)
        return

    await database.update_batch_progress(batch_id, {
        "state": "sending_receives",
        "message": f"Enviando recebimento: 0/{len(payloads)} MTRs em lotes de {RECEIVE_BATCH_SIZE}",
    })

    success = 0
    receive_processed = 0

    # Split into batches
    for batch_start in range(0, len(payloads), RECEIVE_BATCH_SIZE):
        if is_cancelled(batch_id):
            logger.info(f"[Baixa {batch_id}] Cancelled at receive batch {batch_start}")
            for _, (cid, _) in enumerate(payloads[batch_start:]):
                await database.update_validation_job(cid, "falha", "Cancelado pelo usuario")
                failed += 1
            break

        batch_items = payloads[batch_start:batch_start + RECEIVE_BATCH_SIZE]
        batch_payloads = [p for _, p in batch_items]
        batch_cids = {p["manNumero"]: cid for cid, p in batch_items}

        # Update status for items in this batch
        for cid, _ in batch_items:
            await database.update_validation_job(cid, "processando", "Enviando recebimento ao SIGOR...")

        # Send with retry
        response = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.receber_manifesto_lote(batch_payloads)
                break
            except SigorAPIError as e:
                if attempt < MAX_RETRIES and e.status_code in (429, 500, 502, 503):
                    logger.warning(f"[Baixa {batch_id}] Batch receive attempt {attempt} failed: {e}, retrying...")
                    await asyncio.sleep(RETRY_DELAY)
                    try:
                        await client._ensure_token()
                    except Exception:
                        pass
                else:
                    logger.error(f"[Baixa {batch_id}] Batch receive failed: {e}")
                    for cid, _ in batch_items:
                        await database.update_validation_job(cid, "falha", f"Erro SIGOR: {e.message}")
                        failed += 1
                    receive_processed += len(batch_items)
                    response = None
                    break

        if response:
            # Parse individual results
            respostas = response.get("objetoResposta", [])
            if isinstance(respostas, list):
                for resp_item in respostas:
                    mtr_num = resp_item.get("manNumero", "")
                    cid = batch_cids.get(mtr_num)
                    if not cid:
                        continue

                    valido = resp_item.get("restResponseValido", False)
                    msg = resp_item.get("restResponseMensagem", "")

                    # Check residuo-level errors
                    residuo_errs = []
                    for res in resp_item.get("listaManifestoResiduo", []):
                        if not res.get("restResponseValido", True):
                            residuo_errs.append(res.get("restResponseMensagem", "Erro residuo"))

                    if valido and not residuo_errs:
                        await database.update_validation_job(cid, "validado", "Recebimento efetuado com sucesso!")
                        success += 1
                    else:
                        error_msg = msg or "; ".join(residuo_errs) or "Erro no recebimento"
                        await database.update_validation_job(cid, "falha", error_msg)
                        failed += 1
                    receive_processed += 1
            else:
                # Batch-level error
                batch_msg = response.get("mensagem", "Erro desconhecido no lote")
                for cid, _ in batch_items:
                    await database.update_validation_job(cid, "falha", batch_msg)
                    failed += 1
                receive_processed += len(batch_items)

        # Update batch progress
        await database.update_batch_progress(batch_id, {
            "processed": processed,
            "success": success,
            "failed": failed,
            "state": "sending_receives",
            "message": f"Recebimento: {receive_processed}/{len(payloads)} enviados, {success} OK, {failed} falha(s)",
        })

        # Rate limit between batch receive calls
        if batch_start + RECEIVE_BATCH_SIZE < len(payloads):
            await asyncio.sleep(RECEIVE_BATCH_DELAY)

    # 4) Done
    final_state = "completed" if is_cancelled(batch_id) is False else "cancelled"
    await database.update_batch_progress(batch_id, {
        "state": final_state,
        "processed": total,
        "success": success,
        "failed": failed,
        "message": f"Concluido: {success} recebidos, {failed} falha(s) de {total} total",
    })

    logger.info(f"[Baixa {batch_id}] Finished: {success} success, {failed} failed, {total} total")
    await client.close()
    clear_cancel(batch_id)
