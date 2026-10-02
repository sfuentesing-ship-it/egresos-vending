import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from collections import defaultdict

from flask import Flask, jsonify, render_template, request, Response

from parrotfy import ParrotfyClient, SessionExpiredError

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
CACHE_PATH = os.path.join(os.path.dirname(__file__), "cache.json")

app = Flask(__name__)
# _details guarda el detalle de cada egreso (grupo) para siempre: no cambia una vez creado
_details = {}
_cache = {"key": None, "ids": None, "egresos": None, "ts": 0}
_fetch_lock = threading.Lock()

# Cargar caché guardada en disco (para no perder datos al reiniciar)
try:
    with open(CACHE_PATH, encoding="utf-8") as f:
        _saved = json.load(f)
        _details = {int(k): v for k, v in _saved.get("details", {}).items()}
        if not _details and _saved.get("egresos"):
            # formato antiguo de caché
            _details = {int(e["group_id"]): e for e in _saved["egresos"]}
        key = tuple(_saved["key"]) if _saved.get("key") else None
        ids = _saved.get("ids") or list(_details.keys())
        egresos = [_details[i] for i in ids if i in _details]
        if egresos:
            _cache.update(key=key, ids=ids, egresos=egresos, ts=_saved.get("ts", 0))
except Exception:
    pass


def _save_cache():
    try:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "key": list(_cache["key"]) if _cache["key"] else None,
                "ids": _cache["ids"],
                "ts": _cache["ts"],
                "details": {str(k): v for k, v in _details.items()},
            }, f)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass


def load_config():
    # En la nube se usan variables de entorno; en local, config.json
    if os.environ.get("PARROTFY_COOKIE"):
        return {
            "tenant": os.environ.get("TENANT", "vendingstore"),
            "cookie": os.environ["PARROTFY_COOKIE"],
            "csrf": os.environ.get("PARROTFY_CSRF", ""),
        }
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def get_client():
    cfg = load_config()
    return ParrotfyClient(cfg["tenant"], cfg["cookie"], cfg["csrf"]), cfg


def fetch_egresos(date_from: date, date_to: date, force=False):
    key = (date_from, date_to)
    now = datetime.now().timestamp()
    if not force and _cache["key"] == key and now - _cache["ts"] < 4 * 60 * 60:
        return _cache["egresos"]
    with _fetch_lock:
        # Si otra consulta acaba de actualizar (o se está actualizando), reutilizar
        now = datetime.now().timestamp()
        if _cache["key"] == key and now - _cache["ts"] < 60:
            return _cache["egresos"]
        client, _ = get_client()
        lines = client.fetch_movement_lines(date_from, date_to)
        ids = []
        for ln in lines:
            if ln["group_id"] and ln["group_id"] not in ids:
                ids.append(ln["group_id"])
        # Solo se piden a Parrofy los egresos que aún no están guardados
        missing = [gid for gid in ids if gid not in _details]
        if missing:
            def _fetch(gid):
                try:
                    return gid, client.fetch_group_detail(gid)
                except Exception:
                    return gid, None
            with ThreadPoolExecutor(max_workers=4) as ex:
                for gid, det in ex.map(_fetch, missing):
                    if det:
                        _details[gid] = det
        egresos = [_details[gid] for gid in ids if gid in _details]
        if ids and not egresos:
            raise RuntimeError("No se pudo obtener el detalle de los egresos (límite de consultas de Parrofy).")
        egresos.sort(key=_cronologico, reverse=True)
        _cache.update(key=key, ids=ids, egresos=egresos, ts=now)
        _save_cache()
        return egresos


def _cronologico(e):
    try:
        return (datetime.strptime(e["fecha"], "%d/%m/%Y"), e.get("creado") or "")
    except Exception:
        return (datetime.min, "")


def parse_d(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


@app.route("/")
def index():
    return render_template("index.html")


@app.before_request
def _auth():
    # Protección con usuario/contraseña si se configuran (recomendado en la nube)
    user = os.environ.get("APP_USER")
    pwd = os.environ.get("APP_PASSWORD")
    if not user or not pwd:
        return
    a = request.authorization
    if not a or a.username != user or a.password != pwd:
        return Response("Acceso restringido", 401, {"WWW-Authenticate": 'Basic realm="Egresos"'})


@app.after_request
def no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/resumen")
def api_resumen():
    try:
        hoy = parse_d(request.args.get("hoy", date.today().strftime("%Y-%m-%d")))
    except ValueError:
        hoy = date.today()
    desde = min(hoy.replace(day=1), hoy - timedelta(days=30))
    hasta = hoy + timedelta(days=1)
    force = request.args.get("refresh") == "1"
    aviso = None
    try:
        egresos = fetch_egresos(desde, hasta, force=force)
    except SessionExpiredError as e:
        return jsonify({"error": str(e)}), 401
    except Exception as e:
        # Si Parrofy limita o falla, mostrar los últimos datos guardados
        if _cache["egresos"]:
            egresos = _cache["egresos"]
            aviso = "Parrofy no respondió (límite de consultas). Mostrando los últimos datos guardados."
        else:
            msg = "Parrofy limitó las consultas temporalmente (error 429). Espera unos minutos e inténtalo de nuevo." if "429" in str(e) else str(e)
            return jsonify({"error": msg}), 500

    def e_fecha(e):
        try:
            return datetime.strptime(e["fecha"], "%d/%m/%Y").date()
        except Exception:
            return None

    hoy_total = sum(e["total"] for e in egresos if e_fecha(e) == hoy)
    mes_egresos = [e for e in egresos if e_fecha(e) and e_fecha(e).month == hoy.month and e_fecha(e).year == hoy.year]
    mes_total = sum(e["total"] for e in mes_egresos)
    prods_hoy = sum(e["n_productos"] for e in egresos if e_fecha(e) == hoy)
    egresos_hoy = [e for e in egresos if e_fecha(e) == hoy]
    reps_hoy = {e["documento"] for e in egresos_hoy}

    por_reponedor = defaultdict(float)
    for e in mes_egresos:
        por_reponedor[e["documento"]] += e["total"]
    participacion = sorted(
        [{"nombre": k, "total": v} for k, v in por_reponedor.items()],
        key=lambda x: x["total"], reverse=True,
    )

    diario = defaultdict(float)
    for e in egresos:
        f = e_fecha(e)
        if f and f <= hoy:
            diario[f.strftime("%d/%m")] += e["total"]
    daily = [{"fecha": k, "total": diario[k]} for k in sorted(diario, key=lambda x: datetime.strptime(x, "%d/%m"))][-30:]
    avg = (sum(d["total"] for d in daily) / len(daily)) if daily else 0

    return jsonify({
        "hoy": hoy.strftime("%Y-%m-%d"),
        "aviso": aviso,
        "egresado_hoy": hoy_total,
        "mes_actual": mes_total,
        "n_egresos_mes": len(mes_egresos),
        "productos_hoy": prods_hoy,
        "n_egresos_hoy": len(egresos_hoy),
        "n_reponedores_hoy": len(reps_hoy),
        "participacion": participacion,
        "diario": daily,
        "promedio_diario": avg,
        "egresos": [
            {"group_id": e["group_id"], "documento": e["documento"], "fecha": e["fecha"],
             "creado": e["creado"], "total": e["total"], "n_productos": e["n_productos"],
             "unidades": e.get("unidades") or _sum_unidades(e)}
            for e in egresos
        ],
    })


def _sum_unidades(e):
    import re as _re
    total = 0
    for p in e.get("productos", []):
        m = _re.search(r"[\d\.]+", str(p.get("cantidad", "")).replace(",", ""))
        if m:
            total += float(m.group())
    return total


@app.route("/api/egreso/<int:gid>")
def api_egreso(gid):
    det = _details.get(gid)
    if not det:
        return jsonify({"error": "Egreso no encontrado"}), 404
    if not det.get("unidades"):
        det["unidades"] = _sum_unidades(det)
    return jsonify(det)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
