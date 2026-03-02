"""Client for MTR Manager API (musa internal) - Scraping version.

Phase 1: Scrapes the public HTML page /manifests
Phase 2 (future): Use /v1/mtr-collects with Bearer Token
"""
import httpx
import logging
import re
from typing import Optional
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

MTR_MANAGER_BASE = "https://mtr-manager.tech.musa.co"

# Column indices from the HTML table
# #, Orgao, Data Coleta, Emissao, Gerador, Transportador, Receptor,
# Material, Tratamento, Peso estimado, ID Coleta, Status, Numero, CDF, Acoes
COL_ORGAO = 1
COL_DATA_COLETA = 2
COL_EMISSAO = 3
COL_GERADOR = 4
COL_TRANSPORTADOR = 5
COL_RECEPTOR = 6
COL_MATERIAL = 7
COL_TRATAMENTO = 8
COL_PESO = 9
COL_ID_COLETA = 10
COL_STATUS = 11
COL_NUMERO = 12
COL_CDF = 13


class MTRManagerClient:
    """Consume MTRs from MTR Manager web page (scraping)."""

    def __init__(self, base_url: str = MTR_MANAGER_BASE):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            timeout=60.0,
            headers={"Accept": "text/html"},
            follow_redirects=True,
        )

    async def close(self):
        await self._http.aclose()

    async def list_manifests(
        self,
        receiver_name: Optional[str] = None,
        receiver_id: Optional[int] = None,
        status: Optional[str] = None,
        issuer: Optional[str] = None,
        generator_name: Optional[str] = None,
        hauler_name: Optional[str] = None,
        material_name: Optional[str] = None,
        number: Optional[str] = None,
        planned_date_at: Optional[str] = None,
        planned_date_after: Optional[str] = None,
        planned_date_before: Optional[str] = None,
        page: int = 1,
        page_size: int = 100,
    ) -> dict:
        """GET /manifests and scrape HTML table. Returns {pagination, results}."""
        params = {"page_size": page_size}

        # Map our params to the HTML page's query params
        if receiver_name:
            params["receiver_name"] = receiver_name
        if generator_name:
            params["generator_name"] = generator_name
        if hauler_name:
            params["hauler_name"] = hauler_name
        if material_name:
            params["material_name"] = material_name
        if issuer:
            params["issuer"] = issuer
        if status:
            params["status"] = status
        if number:
            params["number"] = number
        if planned_date_at:
            params["planned_date_at"] = planned_date_at

        try:
            resp = await self._http.get(
                f"{self.base_url}/manifests", params=params
            )
        except Exception as e:
            logger.error(f"MTR Manager request failed: {e}")
            return self._empty_response(page_size)

        if resp.status_code != 200:
            logger.error(
                f"MTR Manager returned {resp.status_code}: {resp.text[:200]}"
            )
            return self._empty_response(page_size)

        return self._parse_html(resp.text, page, page_size)

    def _parse_html(self, html: str, page: int, page_size: int) -> dict:
        """Parse the HTML table into structured JSON."""
        soup = BeautifulSoup(html, "html.parser")
        results = []

        # Extract total count from header like "302848 no total"
        total_count = 0
        badge = soup.find("span", class_="badge")
        if badge:
            match = re.search(r"(\d+)", badge.get_text())
            if match:
                total_count = int(match.group(1))

        # Find the data table
        table = soup.find("table")
        if not table:
            logger.warning("No table found in MTR Manager HTML")
            return self._empty_response(page_size)

        # Get tbody rows (skip header)
        tbody = table.find("tbody")
        rows = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 12:
                continue

            try:
                # Extract text, stripping whitespace
                def cell_text(idx):
                    if idx < len(cells):
                        return cells[idx].get_text(strip=True)
                    return ""

                # Extract ID Coleta (it's usually a link)
                id_coleta = ""
                if COL_ID_COLETA < len(cells):
                    link = cells[COL_ID_COLETA].find("a")
                    if link:
                        id_coleta = link.get_text(strip=True)
                    else:
                        id_coleta = cell_text(COL_ID_COLETA)

                # Parse peso (weight) - handle comma as decimal
                peso_text = cell_text(COL_PESO).replace(",", ".")
                try:
                    peso = float(peso_text) if peso_text else 0.0
                except ValueError:
                    peso = 0.0

                result = {
                    "issuer": cell_text(COL_ORGAO),
                    "planned_date": cell_text(COL_DATA_COLETA),
                    "issued_at": cell_text(COL_EMISSAO),
                    "generator_name": cell_text(COL_GERADOR),
                    "hauler_name": cell_text(COL_TRANSPORTADOR),
                    "receiver_name": cell_text(COL_RECEPTOR),
                    "material_name": cell_text(COL_MATERIAL),
                    "treatment": cell_text(COL_TRATAMENTO),
                    "units_estimated": peso,
                    "collect_id": id_coleta,
                    "collect_status": cell_text(COL_STATUS),
                    "number": cell_text(COL_NUMERO),
                    "certificate": cell_text(COL_CDF),
                }
                results.append(result)

            except Exception as e:
                logger.warning(f"Failed to parse row: {e}")
                continue

        last_page = max(1, (total_count + page_size - 1) // page_size)

        return {
            "pagination": {
                "page": page,
                "last_page": last_page,
                "page_size": page_size,
                "count": total_count,
            },
            "results": results,
        }

    def _empty_response(self, page_size: int = 100) -> dict:
        return {
            "pagination": {
                "page": 1,
                "last_page": 1,
                "page_size": page_size,
                "count": 0,
            },
            "results": [],
        }
