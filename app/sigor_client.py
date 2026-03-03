"""SIGOR WS API Client - Validated against production 26/02/2026

Critical notes from live testing:
- POST /gettoken (lowercase!) - /getToken returns 401
- Reference lists use GET (not POST!) - POST returns 405
- Token response is in objetoResposta, already includes "Bearer " prefix
- API 14 payload is a JSON array (list), not a single object
"""
import httpx
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class SigorAPIError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class SigorClient:
    """Client for SIGOR WS REST API (CETESB)"""

    def __init__(self, base_url: str = "https://mtrr.cetesb.sp.gov.br/apiws/rest"):
        self.base_url = base_url.rstrip("/")
        self.token: Optional[str] = None
        self.token_expiry: float = 0
        self._credentials: Optional[dict] = None
        self._http = httpx.AsyncClient(timeout=30.0, verify=False)

    async def close(self):
        await self._http.aclose()

    async def authenticate(self, cpf: str, senha: str, unidade: int) -> str:
        self._credentials = {"cpfCnpj": cpf, "senha": senha, "unidade": unidade}
        resp = await self._http.post(f"{self.base_url}/gettoken", json=self._credentials)
        if resp.status_code != 200:
            raise SigorAPIError(f"Auth failed: HTTP {resp.status_code}", resp.status_code)
        data = resp.json()
        token = data.get("objetoResposta")
        if not token or not token.startswith("Bearer "):
            raise SigorAPIError(f"Auth failed: {data.get('mensagem', 'Token nao retornado')}")
        self.token = token
        self.token_expiry = time.time() + 3500
        return token

    async def _ensure_token(self):
        if self.token and time.time() < self.token_expiry:
            return
        if not self._credentials:
            raise SigorAPIError("No credentials. Call authenticate() first.")
        await self.authenticate(self._credentials["cpfCnpj"], self._credentials["senha"], self._credentials["unidade"])

    def _auth_headers(self) -> dict:
        return {"Authorization": self.token}

    async def get_classes(self) -> list:
        await self._ensure_token()
        return self._parse_list_response(await self._http.get(f"{self.base_url}/retornaListaClasse", headers=self._auth_headers()), "classes")

    async def get_unidades(self) -> list:
        await self._ensure_token()
        return self._parse_list_response(await self._http.get(f"{self.base_url}/retornaListaUnidade", headers=self._auth_headers()), "unidades")

    async def get_tratamentos(self) -> list:
        await self._ensure_token()
        return self._parse_list_response(await self._http.get(f"{self.base_url}/retornaListaTratamento", headers=self._auth_headers()), "tratamentos")

    async def get_estados_fisicos(self) -> list:
        await self._ensure_token()
        return self._parse_list_response(await self._http.get(f"{self.base_url}/retornaListaTipoEstado", headers=self._auth_headers()), "estados_fisicos")

    async def get_acondicionamentos(self) -> list:
        await self._ensure_token()
        return self._parse_list_response(await self._http.get(f"{self.base_url}/retornaListaAcondicionamento", headers=self._auth_headers()), "acondicionamentos")

    def _parse_list_response(self, resp, name: str) -> list:
        if resp.status_code != 200:
            raise SigorAPIError(f"Failed to get {name}: HTTP {resp.status_code}", resp.status_code)
        data = resp.json()
        if isinstance(data, list): return data
        if isinstance(data, dict) and "objetoResposta" in data:
            obj = data["objetoResposta"]
            return obj if isinstance(obj, list) else [obj]
        return data

    async def get_manifesto(self, mtr_numero: str) -> dict:
        await self._ensure_token()
        resp = await self._http.get(f"{self.base_url}/retornaManifesto/{mtr_numero}", headers=self._auth_headers())
        if resp.status_code != 200:
            raise SigorAPIError(f"Failed to get MTR {mtr_numero}: HTTP {resp.status_code}", resp.status_code)
        data = resp.json()
        result = data.get("objetoResposta")
        if not result:
            raise SigorAPIError(f"MTR {mtr_numero}: {data.get('mensagem', 'nao encontrado')}")
        return result

    async def receber_manifesto_lote(self, payload: list) -> dict:
        await self._ensure_token()
        resp = await self._http.post(f"{self.base_url}/receberManifestoLote", json=payload,
                                     headers={**self._auth_headers(), "Content-Type": "application/json"})
        if resp.status_code != 200:
            raise SigorAPIError(f"receberManifestoLote failed: HTTP {resp.status_code} - {resp.text[:200]}", resp.status_code)
        return resp.json()

    def build_receive_payload(self, manifesto, responsavel, data_recebimento_ms,
                              motorista_override=None, placa_override=None,
                              quantidade_recebida_override=None, justificativa=None):
        residuos_payload = []
        for res in manifesto.get("listaManifestoResiduo", []):
            quantidade = res.get("marQuantidade", 0)
            qtd_recebida = quantidade_recebida_override or quantidade
            item = {
                "resCodigoIbama": res["residuo"]["resCodigoIbama"],
                "marQuantidade": quantidade, "marQuantidadeRecebida": qtd_recebida,
                "traCodigo": res["tratamento"]["traCodigo"], "uniCodigo": res["unidade"]["uniCodigo"],
                "tieCodigo": res["tipoEstado"]["tieCodigo"], "tiaCodigo": res["tipoAcondicionamento"]["tiaCodigo"],
                "claCodigo": res["classe"]["claCodigo"],
            }
            if qtd_recebida != quantidade:
                item["marJustificativa"] = justificativa or f"Qtd diferente: {qtd_recebida} vs {quantidade}"
            residuos_payload.append(item)
        placa = (placa_override or manifesto.get("manPlacaVeiculo", "")).replace("-", "")[:7]
        return {
            "manNumero": manifesto["manNumero"], "dataRecebimento": data_recebimento_ms,
            "nomeMotorista": motorista_override or manifesto.get("manNomeMotorista", ""),
            "placaVeiculo": placa, "nomeResponsavelRecebimento": responsavel,
            "listaManifestoResiduo": residuos_payload,
        }
