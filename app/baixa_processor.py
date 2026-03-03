"""Baixa Processor - Global try/except prevents silent crashes."""
import asyncio, os, time, traceback
from app.sigor_client import SigorClient, SigorAPIError
from app import database

FETCH_DELAY = float(os.getenv("SIGOR_FETCH_DELAY", "6"))
RECEIVE_BATCH_SIZE = int(os.getenv("SIGOR_BATCH_SIZE", "10"))
RECEIVE_BATCH_DELAY = float(os.getenv("SIGOR_BATCH_DELAY", "10"))
MAX_RETRIES = int(os.getenv("SIGOR_MAX_RETRIES", "2"))
RETRY_DELAY = float(os.getenv("SIGOR_RETRY_DELAY", "30"))

_cancel_flags = {}
def request_cancel(bid): _cancel_flags[bid] = True
def is_cancelled(bid): return _cancel_flags.get(bid, False)
def clear_cancel(bid): _cancel_flags.pop(bid, None)

async def process_baixa_batch(batch_id, credential, mtrs, responsavel="", data_recebimento_ms=0):
    total = len(mtrs)
    client = None
    try:
        print(f"[Baixa {batch_id}] === START === {total} MTRs")
        print(f"[Baixa {batch_id}] login={credential.get('login','?')}, unidade={credential.get('unidade_codigo','?')}, resp={responsavel}")
        await database.update_batch_progress(batch_id, {"total": total, "processed": 0, "success": 0, "failed": 0, "state": "authenticating", "message": "Autenticando no SIGOR..."})

        client = SigorClient()
        try:
            await client.authenticate(cpf=str(credential["login"]), senha=str(credential["senha"]), unidade=int(credential["unidade_codigo"]))
            print(f"[Baixa {batch_id}] Auth OK!")
        except SigorAPIError as e:
            print(f"[Baixa {batch_id}] Auth FAILED: {e.message}")
            for m in mtrs: await database.update_validation_job(m["collect_id"], "falha", f"Auth falhou: {e.message}")
            await database.update_batch_progress(batch_id, {"state": "failed", "processed": total, "failed": total, "message": f"Auth falhou: {e.message}"})
            return
        except Exception as e:
            print(f"[Baixa {batch_id}] Auth CRASH: {e}"); traceback.print_exc()
            for m in mtrs: await database.update_validation_job(m["collect_id"], "falha", f"Auth erro: {str(e)[:100]}")
            await database.update_batch_progress(batch_id, {"state": "failed", "processed": total, "failed": total, "message": f"Auth erro: {str(e)[:200]}"})
            return

        if not responsavel: responsavel = credential.get("responsaveis", [""])[0]
        if not responsavel:
            for m in mtrs: await database.update_validation_job(m["collect_id"], "falha", "Sem responsavel")
            await database.update_batch_progress(batch_id, {"state": "failed", "processed": total, "failed": total, "message": "Sem responsavel"})
            return
        if not data_recebimento_ms: data_recebimento_ms = int(time.time() * 1000)

        await database.update_batch_progress(batch_id, {"state": "fetching_details", "message": f"Buscando: 0/{total}"})
        payloads, processed, failed = [], 0, 0

        for i, mtr in enumerate(mtrs):
            if is_cancelled(batch_id):
                for r in mtrs[i:]: await database.update_validation_job(r["collect_id"], "falha", "Cancelado"); failed += 1
                break
            cid, mtr_num = mtr["collect_id"], mtr["mtr_number"]
            print(f"[Baixa {batch_id}] [{i+1}/{total}] Fetch {mtr_num}")
            await database.update_validation_job(cid, "processando", f"Buscando ({i+1}/{total})...")
            details = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    details = await client.get_manifesto(mtr_num); print(f"[Baixa {batch_id}] {mtr_num} OK"); break
                except SigorAPIError as e:
                    if attempt < MAX_RETRIES and e.status_code in (429, 500, 502, 503):
                        print(f"[Baixa {batch_id}] {mtr_num} retry {attempt}"); await asyncio.sleep(RETRY_DELAY)
                        try: await client._ensure_token()
                        except: pass
                    else:
                        print(f"[Baixa {batch_id}] {mtr_num} FAIL: {e.message}")
                        await database.update_validation_job(cid, "falha", f"Busca: {e.message}"); failed += 1; details = None; break
                except Exception as e:
                    print(f"[Baixa {batch_id}] {mtr_num} ERR: {e}"); traceback.print_exc()
                    await database.update_validation_job(cid, "falha", f"Erro: {str(e)[:100]}"); failed += 1; details = None; break

            if details:
                sit = details.get("situacaoManifesto", {})
                sc, sd = sit.get("simCodigo", 0), str(sit.get("simDescricao", ""))
                print(f"[Baixa {batch_id}] {mtr_num} status={sc} ({sd})")
                if sc == 3 or "Recebido" in sd:
                    await database.update_validation_job(cid, "falha", "MTR ja recebido"); failed += 1
                elif sc == 2 or "Cancelado" in sd:
                    await database.update_validation_job(cid, "falha", "MTR cancelado"); failed += 1
                else:
                    peso = mtr.get("peso_coletado") or None
                    if peso and peso <= 0: peso = None
                    try:
                        payload = client.build_receive_payload(manifesto=details, responsavel=responsavel, data_recebimento_ms=data_recebimento_ms, motorista_override=mtr.get("motorista"), placa_override=mtr.get("placa"), quantidade_recebida_override=peso)
                        payloads.append((cid, payload)); print(f"[Baixa {batch_id}] {mtr_num} payload OK")
                    except Exception as e:
                        print(f"[Baixa {batch_id}] {mtr_num} payload err: {e}"); traceback.print_exc()
                        await database.update_validation_job(cid, "falha", f"Payload: {str(e)[:200]}"); failed += 1
            processed += 1
            if processed % 5 == 0 or processed == total:
                await database.update_batch_progress(batch_id, {"processed": processed, "failed": failed, "state": "fetching_details", "message": f"Buscando: {processed}/{total}"})
            if i < len(mtrs) - 1: await asyncio.sleep(FETCH_DELAY)

        if not payloads:
            await database.update_batch_progress(batch_id, {"state": "completed", "processed": total, "failed": failed, "success": 0, "message": f"Nenhum valido. {failed} falha(s)."})
            return

        print(f"[Baixa {batch_id}] Phase 2: {len(payloads)} payloads")
        await database.update_batch_progress(batch_id, {"state": "sending_receives", "message": f"Enviando: 0/{len(payloads)}"})
        success, recv_done = 0, 0

        for bs in range(0, len(payloads), RECEIVE_BATCH_SIZE):
            if is_cancelled(batch_id):
                for cid, _ in payloads[bs:]: await database.update_validation_job(cid, "falha", "Cancelado"); failed += 1
                break
            items = payloads[bs:bs + RECEIVE_BATCH_SIZE]
            bpayloads = [p for _, p in items]
            cid_map = {p["manNumero"]: cid for cid, p in items}
            for cid, _ in items: await database.update_validation_job(cid, "processando", "Enviando recebimento...")
            print(f"[Baixa {batch_id}] Send batch: {[p['manNumero'] for p in bpayloads]}")
            resp = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    resp = await client.receber_manifesto_lote(bpayloads); print(f"[Baixa {batch_id}] Resp: {str(resp)[:500]}"); break
                except SigorAPIError as e:
                    if attempt < MAX_RETRIES and e.status_code in (429, 500, 502, 503):
                        await asyncio.sleep(RETRY_DELAY)
                        try: await client._ensure_token()
                        except: pass
                    else:
                        for cid, _ in items: await database.update_validation_job(cid, "falha", f"SIGOR: {e.message}"); failed += 1
                        recv_done += len(items); resp = None; break
                except Exception as e:
                    traceback.print_exc()
                    for cid, _ in items: await database.update_validation_job(cid, "falha", f"Erro: {str(e)[:100]}"); failed += 1
                    recv_done += len(items); resp = None; break

            if resp:
                respostas = resp.get("objetoResposta", [])
                if isinstance(respostas, list):
                    for ri in respostas:
                        mn = ri.get("manNumero", ""); cid = cid_map.get(mn)
                        if not cid: continue
                        valido = ri.get("restResponseValido", False)
                        msg = ri.get("restResponseMensagem", "")
                        res_errs = [r.get("restResponseMensagem", "Err") for r in ri.get("listaManifestoResiduo", []) if not r.get("restResponseValido", True)]
                        if valido and not res_errs:
                            print(f"[Baixa {batch_id}] {mn} VALIDATED!"); await database.update_validation_job(cid, "validado", "Recebido com sucesso!"); success += 1
                        else:
                            emsg = msg or "; ".join(res_errs) or "Erro"; await database.update_validation_job(cid, "falha", emsg); failed += 1
                        recv_done += 1
                else:
                    bmsg = resp.get("mensagem", str(resp)[:200])
                    for cid, _ in items: await database.update_validation_job(cid, "falha", f"Lote: {bmsg}"); failed += 1
                    recv_done += len(items)
            await database.update_batch_progress(batch_id, {"processed": processed, "success": success, "failed": failed, "state": "sending_receives", "message": f"Enviando: {recv_done}/{len(payloads)}, {success} OK"})
            if bs + RECEIVE_BATCH_SIZE < len(payloads): await asyncio.sleep(RECEIVE_BATCH_DELAY)

        fs = "completed" if not is_cancelled(batch_id) else "cancelled"
        await database.update_batch_progress(batch_id, {"state": fs, "processed": total, "success": success, "failed": failed, "message": f"Concluido: {success} OK, {failed} falha(s) de {total}"})
        print(f"[Baixa {batch_id}] === DONE === {success}/{failed}/{total}")

    except Exception as e:
        emsg = f"{type(e).__name__}: {str(e)[:300]}"; print(f"[Baixa {batch_id}] === FATAL === {emsg}"); traceback.print_exc()
        try:
            for m in mtrs: await database.update_validation_job(m["collect_id"], "falha", f"Fatal: {emsg}")
            await database.update_batch_progress(batch_id, {"state": "failed", "processed": total, "failed": total, "message": f"Fatal: {emsg}"})
        except: pass
    finally:
        if client:
            try: await client.close()
            except: pass
        clear_cancel(batch_id); print(f"[Baixa {batch_id}] === CLEANUP ===")
