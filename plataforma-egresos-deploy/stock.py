"""Coincidencias entre la lista de productos del cliente y el stock de Parrotfy."""
import json
import os
import re
import unicodedata

MANUAL_PATH = os.path.join(os.path.dirname(__file__), "productos_manual.json")

STOP = {
    "de", "la", "el", "los", "las", "y", "con", "sin", "un", "una", "x",
    "gr", "grs", "g", "ml", "cc", "lts", "l", "kg", "botella", "lata",
    "pet", "caja", "bolsa", "variedades", "variedad", "un", "sabor", "sabores",
}


def norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(s):
    return {t for t in norm(s).split() if t not in STOP and len(t) > 1}


def load_manual():
    try:
        with open(MANUAL_PATH, encoding="utf-8") as f:
            return {norm(k): v for k, v in json.load(f).items()}
    except Exception:
        return {}


def match_producto(nombre, barcode, parro, manual):
    """Devuelve (producto_parrotfy, confianza). confianza: alta/media/baja/ninguna."""
    manual_name = manual.get(norm(nombre))
    if manual_name:
        for p in parro:
            if p["nombre"] == manual_name:
                return p, "manual"

    en = norm(nombre)
    et = tokens(nombre)
    eb = str(barcode or "").lstrip("0")
    best, best_score, best_conf = None, 0, "ninguna"

    for p in parro:
        pn = p.get("norm") or norm(p["nombre"])
        pc = str(p.get("code") or "").split(".")[0].lstrip("0")
        conf = None
        if eb and pc and eb == pc:
            conf, score = "alta", 1000
        elif en and en == pn:
            conf, score = "alta", 900
        elif en and pn and (en in pn or pn in en) and min(len(en), len(pn)) >= 4:
            conf, score = "media", 700 + min(len(en), len(pn))
        else:
            pt = p.get("nt") or tokens(p["nombre"])
            inter = et & pt
            if inter:
                jac = len(inter) / len(et | pt)
                if jac >= 0.5:
                    conf, score = "media", 500 + jac * 100
                elif jac >= 0.25:
                    conf, score = "baja", 300 + jac * 100
                elif any(len(t) >= 5 for t in inter):
                    conf, score = "baja", 200 + jac * 100
        if conf and score > best_score:
            best, best_score, best_conf = p, score, conf
    return best, best_conf


def build_stock(productos, parro):
    """Cruza la lista del cliente con el stock actual de Parrotfy.

    Devuelve (items, sin_stock, sin_coincidencia):
    - items: todos los productos de Parrofy con stock > 0 (incluye nuevos)
    - sin_stock: productos de la lista del cliente que están en 0
    - sin_coincidencia: productos de la lista que no se encontraron en Parrofy
    """
    manual = load_manual()
    user_matches = {}
    lista_items = []
    for prod in productos:
        p, conf = match_producto(prod["nombre"], prod.get("barcode", ""), parro, manual)
        if p:
            user_matches.setdefault(p["nombre"], prod["nombre"])
        lista_items.append({
            "nombre": prod["nombre"],
            "parrotfy": p["nombre"] if p else None,
            "stock": p["stock"] if p else None,
            "confianza": conf,
        })

    items = []
    for p in parro:
        if p["stock"] > 0:
            items.append({
                "nombre": p["nombre"],
                "code": p.get("code", ""),
                "stock": p["stock"],
                "en_lista": p["nombre"] in user_matches,
                "nombre_lista": user_matches.get(p["nombre"]),
            })
    items.sort(key=lambda x: norm(x["nombre"]))

    sin_stock = [i for i in lista_items if i["stock"] == 0]
    sin_stock.sort(key=lambda x: norm(x["nombre"]))
    sin_coincidencia = [i for i in lista_items if i["stock"] is None]
    sin_coincidencia.sort(key=lambda x: norm(x["nombre"]))
    return items, sin_stock, sin_coincidencia
