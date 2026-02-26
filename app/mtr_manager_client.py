"""Client for MTR Manager API (musa internal)"""
import httpx
import logging
from typing import Optional

logger = logging.getLogger(__name__)
MTR_MANAGER_BASE = "https://mtr-manager.tech.musa.co"


class MTRManagerClient:
    def __init__(self, base_url: str = MTR_MANAGER_BASE):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=30.0, headers={"Accept": "application/json"})

    async def close(self):
        await self._http.aclose()

    async def list_manifests(self, receiver_name=None, receiver_id=None, status=None,
                             issuer=None, generator_name=None, hauler_name=None,
                             material_name=None, number=None, planned_date_at=None,
                             planned_date_after=None, planned_date_before=None,
                             page=1, page_size=100) -> dict:
        params = {"page": page, "page_size": page_size}
        for k, v in {"receiver_name": receiver_name, "receiver_id": receiver_id,
                      "status": status, "issuer": issuer, "generator_name": generator_name,
                      "hauler_name": hauler_name, "material_name": material_name,
                      "number": number, "planned_date_at": planned_date_at,
                      "planned_date_after": planned_date_after,
                      "planned_date_before": planned_date_before}.items():
            if v:
                params[k] = v
        resp = await self._http.get(f"{self.base_url}/manifests", params=params)
        if resp.status_code != 200:
            logger.error(f"MTR Manager returned {resp.status_code}")
            return {"pagination": {"page": 1, "last_page": 1, "page_size": page_size, "count": 0}, "results": []}
        return resp.json()
