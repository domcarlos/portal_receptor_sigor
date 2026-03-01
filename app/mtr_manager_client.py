"""Client for MTR Manager - scrapes public /manifests page"""
import httpx
import logging
import re
from typing import Optional
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

MTR_MANAGER_BASE = "https://mtr-manager.tech.musa.co"


class MTRManagerClient:
    def __init__(self, base_url: str = MTR_MANAGER_BASE):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            timeout=60.0,
            headers={"User-Agent": "PortalReceptor/1.0"},
            follow_redirects=True,
        )

    async def close(self):
        await self._http.aclose()

    async def list_manifests(
        self,
        receiver_name: Optional[str] = None,
        generator_name: Optional[str] = None,
        hauler_name: Optional[str] = None,
        material_name: Optional[str] = None,
        status: Optional[str] = None,
        issuer: Optional[str] = None,
        number: Optional[str] = None,
        collect_id: Optional[str] = None,
        planned_date_at: Optional[str] = None,
        issued_at: Optional[str] = None,
        page: int = 1,
        page_size: int = 100,
    ) -> dict:
        params = {"page_size": page_size}
        if page > 1:
            params["page"] = page
        for k, v in {"receiver_name": receiver_name, "generator_name": generator_name, "hauler_name": hauler_name, "material_name": material_name, "status": status, "issuer": issuer, "number": number, "collect_id": collect_id, "planned_date_at": planned_date_at, "issued_at": issued_at}.items():
            if v:
                params[k] = v
        url = f"{self.base_url}/manifests"
        logger.info(f"Fetching {url} with params {params}")
        resp = await self._http.get(url, params=params)
        if resp.status_code != 200:
            logger.error(f"MTR Manager returned {resp.status_code}")
            return {"pagination": {"page": page, "page_size": page_size, "count": 0}, "results": []}
        return self._parse_html(resp.text, page, page_size)

    def _parse_html(self, html: str, page: int, page_size: int) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        badge = soup.select_one(".badge")
        total_text = badge.get_text(strip=True) if badge else "0"
        count_match = re.search(r"(\d+)", total_text.replace(".", ""))
        count = int(count_match.group(1)) if count_match else 0
        table = soup.select_one("table")
        if not table:
            return {"pagination": {"page": page, "page_size": page_size, "count": 0}, "results": []}
        rows = table.select("tbody tr")
        results = []
        for row in rows:
            cells = row.select("td")
            if len(cells) < 14:
                continue
            detail_link = ""
            if len(cells) >= 15:
                a_tag = cells[-1].select_one("a")
                if a_tag and a_tag.get("href"):
                    detail_link = a_tag["href"]
            results.append({"row_num": cells[0].get_text(strip=True), "orgao": cells[1].get_text(strip=True), "data_coleta": cells[2].get_text(strip=True), "emissao": cells[3].get_text(strip=True), "gerador": cells[4].get_text(strip=True), "transportador": cells[5].get_text(strip=True), "receptor": cells[6].get_text(strip=True), "material": cells[7].get_text(strip=True), "tratamento": cells[8].get_text(strip=True), "peso_estimado": cells[9].get_text(strip=True), "id_coleta": cells[10].get_text(strip=True), "status": cells[11].get_text(strip=True), "numero": cells[12].get_text(strip=True), "numero_cdf": cells[13].get_text(strip=True), "detail_link": detail_link})
        last_page = max(1, -(-count // page_size))
        return {"pagination": {"page": page, "last_page": last_page, "page_size": page_size, "count": count}, "results": results}
