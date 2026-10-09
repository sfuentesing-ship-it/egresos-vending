"""Cliente para la API interna de Parrotfy (movimientos de inventario)."""
import re
import time
from datetime import datetime, date, timedelta
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://{tenant}.parrotfy.com"

HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "/inventory_movements",
}


class SessionExpiredError(Exception):
    pass


class ParrotfyClient:
    def __init__(self, tenant: str, cookie: str, csrf: str):
        self.base = BASE.format(tenant=tenant)
        # Limpiar espacios/saltos de línea que se cuelan al pegar las credenciales
        csrf = re.sub(r"[\r\n\t]+", "", str(csrf)).strip()
        cookie = re.sub(r"[\r\n\t]+", " ", str(cookie)).strip()
        self.session = requests.Session()
        self.session.headers.update({
            **HEADERS,
            "X-CSRF-Token": csrf,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0 Safari/537.36",
        })
        for part in cookie.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                self.session.cookies.set(k.strip(), v.strip(), domain=f".{tenant}.parrotfy.com", path="/")

    def _check(self, r: requests.Response):
        if r.status_code in (401, 403) or "login" in r.headers.get("Location", "").lower():
            raise SessionExpiredError("Sesión de Parrotfy expirada o cookie inválida")
        r.raise_for_status()

    def _get(self, path, params=None):
        last = None
        for attempt in range(5):
            try:
                r = self.session.get(self.base + path, params=params, timeout=30)
                if r.status_code == 429 or r.status_code >= 500:
                    wait = r.headers.get("Retry-After")
                    try:
                        wait = float(wait) if wait else 0
                    except ValueError:
                        wait = 0
                    time.sleep(min(wait or (2 ** attempt) * 2, 60))
                    last = requests.HTTPError(f"HTTP {r.status_code}")
                    continue
                # Si un endpoint .json devuelve HTML (página de login) la sesión expiró
                ctype = r.headers.get("Content-Type", "")
                if path.endswith(".json") and "text/html" in ctype:
                    raise SessionExpiredError("Sesión de Parrotfy expirada o cookie inválida")
                self._check(r)
                return r
            except (requests.ConnectionError, requests.Timeout) as e:
                last = e
                time.sleep(1.5 * (attempt + 1))
        raise last

    def fetch_movement_lines(self, date_from: date, date_to: date, warehouse_id=2, movement_type="withdraw"):
        """Devuelve las líneas de movimiento (producto por producto) en el rango."""
        lines = []
        start = 0
        length = 100
        while True:
            params = {
                "sEcho": 1,
                "iDisplayStart": start,
                "iDisplayLength": length,
                "sSearch": "",
                "iSortCol_0": 0,
                "sSortDir_0": "desc",
                "date_from": date_from.strftime("%d/%m/%Y"),
                "date_to": date_to.strftime("%d/%m/%Y"),
                "category_ids": "",
                "warehouse_id": warehouse_id,
                "movement_type": movement_type,
            }
            r = self._get("/inventory_movements.json", params=params)
            j = r.json()
            for row in j.get("aaData", []):
                lines.append(self._parse_line(row, movement_type))
            total = j.get("iTotalRecords", 0)
            start += length
            if start >= total or not j.get("aaData"):
                break
        return lines

    def _parse_line(self, row, movement_type="withdraw"):
        fecha = row[0]
        prod_html = row[1]
        doc_html = row[2]
        user_html = row[3]
        valor_unit = row[4]
        m = re.search(r"inventory_movement_groups/(\d+)", doc_html)
        group_id = int(m.group(1)) if m else None
        doc = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", doc_html)).strip().lstrip("- /").strip()
        soup = BeautifulSoup(prod_html, "html.parser")
        # El nombre visible puede estar cortado; data-title tiene el nombre completo
        p = soup.find("p")
        producto = (p.get("data-title") if p and p.get("data-title") else soup.get_text(" ", strip=True)).strip()

        def parse_qty(s):
            m2 = re.search(r"[\d\.]+", str(s).replace(",", ""))
            return float(m2.group()) if m2 else 0

        ingreso = parse_qty(row[5]) if len(row) > 5 else 0
        egreso = parse_qty(row[6]) if len(row) > 6 else 0
        disponible = parse_qty(row[7]) if len(row) > 7 else 0
        saldo = _parse_money(row[8]) if len(row) > 8 else 0
        cantidad = ingreso if movement_type == "entry" else egreso
        return {
            "fecha": _parse_date(fecha),
            "producto": producto,
            "group_id": group_id,
            "documento": doc,
            "usuario": BeautifulSoup(user_html, "html.parser").get_text(" ", strip=True),
            "valor_unitario": _parse_money(valor_unit),
            "cantidad": cantidad,
            "ingreso": ingreso,
            "egreso": egreso,
            "disponible": disponible,
            "saldo": saldo,
            "total": cantidad * _parse_money(valor_unit),
        }

    def fetch_group_detail(self, group_id: int):
        r = self._get(f"/inventory_movement_groups/{group_id}")
        return parse_group_detail(r.text, group_id)

    def fetch_stock_lines(self, warehouse_id=2):
        """Stock actual por producto en la bodega (incluye productos con stock 0)."""
        from bs4 import BeautifulSoup as BS
        lines = []
        start = 0
        length = 100
        while True:
            params = {
                "stock": "true",
                "sEcho": 1,
                "iDisplayStart": start,
                "iDisplayLength": length,
                "sSearch": "",
                "iSortCol_0": 1,
                "sSortDir_0": "true",
                "date_from": date.today().strftime("%d/%m/%Y"),
                "date_to": date.today().strftime("%d/%m/%Y"),
                "category_ids": "",
                "warehouse_id": warehouse_id,
                "stock_balance_status": "all",
            }
            r = self._get("/inventory_movements.json", params=params)
            j = r.json()
            for row in j.get("aaData", []):
                soup = BS(str(row[1]), "html.parser")
                p = soup.find("p")
                nombre = (p.get("data-title") if p and p.get("data-title") else soup.get_text(" ", strip=True)).strip()
                m = re.search(r"[\d\.]+", re.sub(r"<[^>]+>", "", str(row[6])))
                stock = float(m.group()) if m else 0
                lines.append({
                    "nombre": nombre,
                    "code": str(row[0]).strip(),
                    "stock": stock,
                })
            total = j.get("iTotalRecords", 0)
            start += length
            if start >= total or not j.get("aaData"):
                break
        return lines

    def fetch_egresos(self, date_from: date, date_to: date, warehouse_id=2):
        from concurrent.futures import ThreadPoolExecutor
        lines = self.fetch_movement_lines(date_from, date_to, warehouse_id=warehouse_id)
        group_ids = []
        for ln in lines:
            if ln["group_id"] and ln["group_id"] not in group_ids:
                group_ids.append(ln["group_id"])

        def _fetch(gid):
            try:
                return self.fetch_group_detail(gid)
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=4) as ex:
            groups = list(ex.map(_fetch, group_ids))
        egresos = [g for g in groups if g]
        if group_ids and not egresos:
            raise RuntimeError("No se pudo obtener el detalle de los egresos (límite de consultas de Parrofy).")

        def _crono(e):
            try:
                return datetime.strptime(e["fecha"], "%d/%m/%Y")
            except Exception:
                return datetime.min

        egresos.sort(key=_crono, reverse=True)
        return egresos


def _parse_date(s: str) -> date:
    return datetime.strptime(s.strip(), "%d/%m/%Y").date()


def _parse_money(s: str) -> float:
    s = re.sub(r"[^\d\-]", "", str(s))
    return float(s) if s else 0.0


def parse_group_detail(html: str, group_id: int) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    def field(label):
        el = soup.find("b", string=re.compile(re.escape(label)))
        if not el:
            return None
        # El valor está en el nodo siguiente (texto o hermano)
        sibling = el.find_next_sibling()
        if sibling:
            return sibling.get_text(strip=True)
        parent = el.parent
        txt = parent.get_text(" ", strip=True)
        return txt.split(label, 1)[-1].strip()

    tipo = field("Tipo de registro:")
    bodega = field("Bodega :")
    doc = field("# Documento:") or "SIN DOCUMENTO"
    f_fecha = field("Fecha:")
    f_creado = None
    m = re.search(r"Creado:\s*([\d/]+\s+\d{1,2}:\d{2})", soup.get_text(" ", strip=True))
    if m:
        f_creado = m.group(1).strip()
    # Si el documento no trae nombre, usar Notas como referencia del reponedor
    notas_m = re.search(r"Notas\s*:?\s*([^\n]*)", soup.get_text("\n", strip=True))
    notas = None
    if notas_m:
        notas = notas_m.group(1).strip()
    if not doc or doc.strip() in ("-", ""):
        if notas:
            doc = notas
    productos = []
    total = 0
    unidades = 0
    for tr in soup.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        nombre = tds[0].get_text(" ", strip=True)
        cantidad = tds[2].get_text(" ", strip=True)
        vu = tds[3].get_text(" ", strip=True)
        vt = tds[4].get_text(" ", strip=True)
        nn = tr.find("td", class_="nn-total-cost")
        val = float(nn.get_text(strip=True) or 0) if nn else _parse_money(vt)
        total += val
        m_qty = re.search(r"[\d\.]+", cantidad.replace(",", ""))
        if m_qty:
            unidades += float(m_qty.group())
        productos.append({
            "producto": nombre,
            "cantidad": cantidad,
            "valor_unitario": vu,
            "valor_total": vt,
        })
    return {
        "group_id": group_id,
        "tipo": tipo,
        "bodega": bodega,
        "documento": doc.strip(),
        "fecha": f_fecha,
        "creado": f_creado,
        "total": total,
        "unidades": unidades,
        "productos": productos,
        "n_productos": len(productos),
    }
