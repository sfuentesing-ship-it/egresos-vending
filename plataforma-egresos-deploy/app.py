import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from collections import defaultdict

from flask import Flask, jsonify, render_template, request, Response

from parrotfy import ParrotfyClient, SessionExpiredError
from stock import build_stock, norm
from telegram_bot import TelegramBot

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
CACHE_PATH = os.path.join(os.path.dirname(__file__), "cache.json")
PRODUCTOS_PATH = os.path.join(os.path.dirname(__file__), "productos.json")
CHATS_PATH = os.path.join(os.path.dirname(__file__), "telegram_chats.json")
UMBRAL_CRITICO = int(os.environ.get("UMBRAL_CRITICO", 20))

app = Flask(__name__)
# _details guarda el detalle de cada egreso (grupo) para siempre: no cambia una vez creado
_details = {}
_cache = {"key": None, "ids": None, "egresos": None, "ts": 0}
_ing = {"key": None, "lines": None, "ts": 0}
_stock = {"lines": None, "ts": 0}
_alertas = {}          # producto -> True si ya se avisó que está crítico
_alertas_init = False  # primera vez: se guarda el estado sin enviar avisos
_fetch_lock = threading.Lock()

try:
    with open(PRODUCTOS_PATH, encoding="utf-8") as f:
        _productos = json.load(f)
except Exception:
    _productos = []

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
        ing = _saved.get("ingresos")
        if ing and ing.get("lines"):
            _ing.update(
                key=tuple(ing["key"]) if ing.get("key") else None,
                lines=[_norm_line(l) for l in ing["lines"]],
                ts=ing.get("ts", 0),
            )
        st = _saved.get("stock")
        if st and st.get("lines"):
            _stock.update(lines=st["lines"], ts=st.get("ts", 0))
        if _saved.get("alertas"):
            _alertas.update(_saved["alertas"])
            _alertas_init = True
except Exception:
    pass


def _norm_line(l):
    # Al leer del caché en disco la fecha viene como texto "YYYY-MM-DD"
    f = l.get("fecha")
    if isinstance(f, str):
        try:
            l["fecha"] = datetime.strptime(f, "%Y-%m-%d").date()
        except Exception:
            pass
    return l


def _save_cache():
    try:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "key": list(_cache["key"]) if _cache["key"] else None,
                "ids": _cache["ids"],
                "ts": _cache["ts"],
                "details": {str(k): v for k, v in _details.items()},
                "ingresos": {
                    "key": list(_ing["key"]) if _ing["key"] else None,
                    "lines": _ing["lines"],
                    "ts": _ing["ts"],
                },
                "stock": {
                    "lines": _stock["lines"],
                    "ts": _stock["ts"],
                },
                "alertas": _alertas,
            }, f, default=str)
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


def _crono_line(l):
    return (l["fecha"], l.get("producto") or "")


def fetch_ingresos(date_from: date, date_to: date, force=False):
    key = (date_from, date_to)
    now = datetime.now().timestamp()
    if not force and _ing["key"] == key and now - _ing["ts"] < 4 * 60 * 60:
        return _ing["lines"]
    with _fetch_lock:
        now = datetime.now().timestamp()
        if _ing["key"] == key and now - _ing["ts"] < 60:
            return _ing["lines"]
        client, _ = get_client()
        lines = client.fetch_movement_lines(date_from, date_to, movement_type="entry")
        lines.sort(key=_crono_line, reverse=True)
        _ing.update(key=key, lines=lines, ts=now)
        _save_cache()
        return lines


def fetch_stock(force=False):
    now = datetime.now().timestamp()
    if not force and _stock["lines"] and now - _stock["ts"] < 4 * 60 * 60:
        return _stock["lines"]
    with _fetch_lock:
        now = datetime.now().timestamp()
        if _stock["lines"] and now - _stock["ts"] < 60:
            return _stock["lines"]
        client, _ = get_client()
        lines = client.fetch_stock_lines()
        _stock.update(lines=lines, ts=now)
        _save_cache()
        return lines


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
    base_desde = min(hoy.replace(day=1), hoy - timedelta(days=30))
    base_hasta = hoy + timedelta(days=1)
    desde, hasta = base_desde, base_hasta
    r_desde = request.args.get("desde")
    r_hasta = request.args.get("hasta")
    if r_desde and r_hasta:
        try:
            desde = min(base_desde, parse_d(r_desde))
            hasta = max(base_hasta, parse_d(r_hasta) + timedelta(days=1))
        except ValueError:
            desde, hasta = base_desde, base_hasta
    # límite de seguridad: máximo 240 días por consulta
    if (hasta - desde).days > 240:
        desde = hasta - timedelta(days=240)
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


@app.route("/api/ingresos")
def api_ingresos():
    try:
        hoy = parse_d(request.args.get("hoy", date.today().strftime("%Y-%m-%d")))
    except ValueError:
        hoy = date.today()
    base_desde = min(hoy.replace(day=1), hoy - timedelta(days=30))
    base_hasta = hoy + timedelta(days=1)
    desde, hasta = base_desde, base_hasta
    r_desde = request.args.get("desde")
    r_hasta = request.args.get("hasta")
    if r_desde and r_hasta:
        try:
            desde = min(base_desde, parse_d(r_desde))
            hasta = max(base_hasta, parse_d(r_hasta) + timedelta(days=1))
        except ValueError:
            desde, hasta = base_desde, base_hasta
    if (hasta - desde).days > 240:
        desde = hasta - timedelta(days=240)
    force = request.args.get("refresh") == "1"
    aviso = None
    try:
        lines = fetch_ingresos(desde, hasta, force=force)
    except SessionExpiredError as e:
        return jsonify({"error": str(e)}), 401
    except Exception as e:
        if _ing["lines"]:
            lines = _ing["lines"]
            aviso = "Parrofy no respondió (límite de consultas). Mostrando los últimos datos guardados."
        else:
            msg = "Parrofy limitó las consultas temporalmente (error 429). Espera unos minutos e inténtalo de nuevo." if "429" in str(e) else str(e)
            return jsonify({"error": msg}), 500

    hoy_lines = [l for l in lines if l["fecha"] == hoy]
    mes_lines = [l for l in lines if l["fecha"].month == hoy.month and l["fecha"].year == hoy.year]
    hoy_total = sum(l["total"] for l in hoy_lines)
    mes_total = sum(l["total"] for l in mes_lines)
    prods_hoy = sum(l["cantidad"] for l in hoy_lines)
    n_hoy = len({l["group_id"] for l in hoy_lines})
    n_mes = len({l["group_id"] for l in mes_lines})

    diario = defaultdict(float)
    for l in lines:
        if l["fecha"] <= hoy:
            diario[l["fecha"].strftime("%d/%m")] += l["total"]
    daily = [{"fecha": k, "total": diario[k]} for k in sorted(diario, key=lambda x: datetime.strptime(x, "%d/%m"))][-30:]
    avg = (sum(d["total"] for d in daily) / len(daily)) if daily else 0

    return jsonify({
        "hoy": hoy.strftime("%Y-%m-%d"),
        "aviso": aviso,
        "ingresado_hoy": hoy_total,
        "mes_actual": mes_total,
        "n_ingresos_mes": n_mes,
        "productos_hoy": prods_hoy,
        "n_ingresos_hoy": n_hoy,
        "diario": daily,
        "promedio_diario": avg,
        "lineas": [
            {
                "group_id": l["group_id"],
                "fecha": l["fecha"].strftime("%d/%m/%Y"),
                "producto": l["producto"],
                "documento": l["documento"],
                "usuario": l["usuario"],
                "valor_unitario": l["valor_unitario"],
                "cantidad": l["cantidad"],
                "disponible": l["disponible"],
                "saldo": l["saldo"],
                "total": l["total"],
            }
            for l in lines
        ],
    })


@app.route("/api/stock")
def api_stock():
    force = request.args.get("refresh") == "1"
    aviso = None
    try:
        parro = fetch_stock(force=force)
    except SessionExpiredError as e:
        return jsonify({"error": str(e)}), 401
    except Exception as e:
        if _stock["lines"]:
            parro = _stock["lines"]
            aviso = "Parrofy no respondió. Mostrando el último stock guardado."
        else:
            msg = "Parrofy limitó las consultas temporalmente (error 429). Espera unos minutos e inténtalo de nuevo." if "429" in str(e) else str(e)
            return jsonify({"error": msg}), 500

    items, sin_stock, sin_coincidencia = build_stock(_productos, parro)

    criticos = [i for i in items if i["stock"] < UMBRAL_CRITICO]
    unidades = sum(i["stock"] for i in items)

    return jsonify({
        "aviso": aviso,
        "umbral": UMBRAL_CRITICO,
        "actualizado": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "n_con_stock": len(items),
        "n_criticos": len(criticos),
        "n_sin_stock": len(sin_stock),
        "n_sin_coincidencia": len(sin_coincidencia),
        "unidades_totales": unidades,
        "items": items,
        "sin_stock": sin_stock,
        "sin_coincidencia": sin_coincidencia,
    })


@app.route("/api/egreso/<int:gid>")
def api_egreso(gid):
    det = _details.get(gid)
    if not det:
        # Puede ser un ingreso: se pide el detalle a Parrofy y se guarda
        try:
            client, _ = get_client()
            det = client.fetch_group_detail(gid)
            _details[gid] = det
            _save_cache()
        except Exception:
            return jsonify({"error": "Detalle no encontrado"}), 404
    if not det.get("unidades"):
        det["unidades"] = _sum_unidades(det)
    return jsonify(det)


def _fmt_money(n):
    return "$" + f"{int(round(n)):,}".replace(",", ".")


def _fmt_num(n):
    return f"{int(round(n)):,}".replace(",", ".")


def _get_telegram_token():
    if os.environ.get("TELEGRAM_TOKEN"):
        return os.environ["TELEGRAM_TOKEN"]
    try:
        return load_config().get("telegram_token") or ""
    except Exception:
        return ""


def check_alerts():
    """Revisa el stock, avisa cuando algo entra en estado crítico y manda resumen diario."""
    global _alertas_init
    lines = fetch_stock(force=True)
    items, _, _ = build_stock(_productos, lines)
    nuevos = []
    for i in items:
        if i["stock"] is None or not i["parrotfy"]:
            continue
        nombre = i["parrotfy"]
        st = i["stock"]
        if st <= UMBRAL_CRITICO and not _alertas.get(nombre):
            if _alertas_init:
                nuevos.append((i["nombre"], st))
            _alertas[nombre] = True
        elif st > UMBRAL_CRITICO and _alertas.get(nombre):
            _alertas[nombre] = False
    if not _alertas_init:
        _alertas_init = True

    if _telegram:
        if nuevos:
            lineas = "\n".join(f"• <b>{n}</b>: {int(s)} unidades" for n, s in nuevos[:30])
            _telegram.notify_all(
                f"⚠️ <b>Stock crítico</b> ({UMBRAL_CRITICO} unidades o menos)\n{lineas}\n\nEscribe /criticos para ver todo."
            )
        # Resumen diario (a partir de las 9:00, una vez al día)
        hoy_str = date.today().strftime("%Y-%m-%d")
        if _alertas.get("_resumen_dia") != hoy_str and datetime.now().hour >= 9:
            _alertas["_resumen_dia"] = hoy_str
            criticos = sorted(
                [i for i in items if i["stock"] is not None and i["stock"] <= UMBRAL_CRITICO],
                key=lambda x: x["stock"],
            )
            if criticos:
                lineas = "\n".join(f"• {i['nombre']}: {_fmt_num(i['stock'])}" for i in criticos[:20])
                _telegram.notify_all(
                    f"☀️ <b>Resumen de stock</b>\n{len(criticos)} productos en estado crítico ({UMBRAL_CRITICO} o menos):\n{lineas}"
                )
    _save_cache()


def _cmd_stock(arg):
    lines = fetch_stock(force=not _stock["lines"])
    if arg:
        q = norm(arg)
        matches = [l for l in lines if q in norm(l["nombre"])]
        if not matches:
            return f"No encontré productos con «{arg}»."
        matches.sort(key=lambda l: (0 if l["stock"] > 0 else 1, norm(l["nombre"])))
        out = []
        for l in matches[:40]:
            st = l["stock"]
            emoji = "🔴" if st == 0 else ("🟠" if st < UMBRAL_CRITICO else "🟢")
            out.append(f"{emoji} {l['nombre']}: <b>{_fmt_num(st)}</b> unidades")
        if len(matches) > 40:
            out.append(f"…y {len(matches) - 40} más")
        return "\n".join(out)
    items, _, _ = build_stock(_productos, lines)
    con_stock = [i for i in items if i["stock"] and i["stock"] > 0]
    criticos = [i for i in items if i["stock"] is not None and i["stock"] < UMBRAL_CRITICO]
    total_u = sum(i["stock"] for i in items if i["stock"])
    return (f"📦 <b>Stock Bodega Vending</b>\n"
            f"Productos con stock: <b>{len(con_stock)}</b>\n"
            f"Críticos (menos de {UMBRAL_CRITICO}): <b>{len(criticos)}</b>\n"
            f"Unidades totales: <b>{_fmt_num(total_u)}</b>\n\n"
            f"Para buscar uno: /stock coca\n"
            f"Lista de críticos: /criticos")


def _cmd_criticos():
    lines = fetch_stock(force=not _stock["lines"])
    items, _, _ = build_stock(_productos, lines)
    criticos = sorted([i for i in items if i["stock"] is not None and i["stock"] < UMBRAL_CRITICO], key=lambda x: x["stock"])
    if not criticos:
        return "✅ No hay productos en estado crítico."
    out = [f"⚠️ <b>Productos críticos</b> (menos de {UMBRAL_CRITICO} unidades):"]
    for i in criticos[:25]:
        emoji = "🔴" if i["stock"] == 0 else "🟠"
        out.append(f"{emoji} {i['nombre']}: <b>{_fmt_num(i['stock'])}</b>")
    if len(criticos) > 25:
        out.append(f"…y {len(criticos) - 25} más")
    return "\n".join(out)


def _cmd_sin():
    lines = fetch_stock(force=not _stock["lines"])
    _, sin_stock, _ = build_stock(_productos, lines)
    if not sin_stock:
        return "✅ Ningún producto de tu lista está en 0."
    out = [f"🔴 <b>Sin stock (de tu lista)</b>: {len(sin_stock)} productos"]
    for i in sin_stock[:25]:
        out.append(f"• {i['nombre']}")
    if len(sin_stock) > 25:
        out.append(f"…y {len(sin_stock) - 25} más")
    return "\n".join(out)


def _cmd_periodo(hoy_only):
    hoy = date.today()
    desde = min(hoy.replace(day=1), hoy - timedelta(days=30))
    hasta = hoy + timedelta(days=1)
    egresos = fetch_egresos(desde, hasta)
    ingresos = fetch_ingresos(desde, hasta)
    if hoy_only:
        eh = [e for e in egresos if e["fecha"] == hoy.strftime("%d/%m/%Y")]
        ih = [l for l in ingresos if l["fecha"] == hoy]
        return (f"📅 <b>Hoy {hoy.strftime('%d/%m/%Y')}</b>\n"
                f"Egresos: <b>{_fmt_money(sum(e['total'] for e in eh))}</b> ({len(eh)})\n"
                f"Ingresos: <b>{_fmt_money(sum(l['total'] for l in ih))}</b> ({len({l['group_id'] for l in ih})})")
    em = [e for e in egresos if datetime.strptime(e["fecha"], "%d/%m/%Y").month == hoy.month and datetime.strptime(e["fecha"], "%d/%m/%Y").year == hoy.year]
    im = [l for l in ingresos if l["fecha"].month == hoy.month and l["fecha"].year == hoy.year]
    return (f"📆 <b>{hoy.strftime('%B %Y').capitalize()}</b>\n"
            f"Egresos: <b>{_fmt_money(sum(e['total'] for e in em))}</b> ({len(em)})\n"
            f"Ingresos: <b>{_fmt_money(sum(l['total'] for l in im))}</b> ({len({l['group_id'] for l in im})})")


def _buscar_producto(query):
    """Búsqueda flexible de productos por nombre (ignora acentos y mayúsculas)."""
    lines = fetch_stock(force=not _stock["lines"])
    q = norm(query)
    tokens_q = [t for t in q.split() if len(t) > 2]
    if not tokens_q:
        return "Dime el nombre de un producto, por ejemplo: stock de coca cola"
    exactos, parciales = [], []
    for l in lines:
        palabras = set(norm(l["nombre"]).split())
        ne = sum(1 for t in tokens_q if t in palabras)
        if ne:
            exactos.append((ne, l))
            continue
        np_ = sum(1 for t in tokens_q if any(w.startswith(t) for w in palabras if len(w) > 3))
        if np_:
            parciales.append((np_, l))
    resultados = exactos or parciales
    if not resultados:
        return f"No encontré productos parecidos a «{query}».\nPrueba con otra palabra, ej: /stock coca"
    resultados.sort(key=lambda x: (0 if x[1]["stock"] > 0 else 1, -x[0], norm(x[1]["nombre"])))
    out = [f"🔎 Resultados para «{query}» ({len(resultados)}):"]
    for score, l in resultados[:40]:
        st = l["stock"]
        emoji = "🔴" if st == 0 else ("🟠" if st < UMBRAL_CRITICO else "🟢")
        out.append(f"{emoji} {l['nombre']}: <b>{_fmt_num(st)}</b> unidades")
    if len(resultados) > 40:
        out.append(f"…y {len(resultados) - 40} más")
    return "\n".join(out)


def _respuesta_libre(text):
    """Interpreta mensajes sin comandos: 'stock de coca', 'cuanto queda de vital', etc."""
    t = norm(text)
    if any(w in t for w in ("critico", "criticos", "critica", "reponer", "comprar", "agotar")):
        return _cmd_criticos()
    if "sin stock" in t or "agotado" in t:
        return _cmd_sin()
    if "ingreso" in t and ("hoy" in t or "dia" in t):
        return _cmd_periodo(True)
    if "egreso" in t and ("hoy" in t or "dia" in t):
        return _cmd_periodo(True)
    if "mes" in t or "mensual" in t:
        return _cmd_periodo(False)
    if ("hoy" in t or "dia" in t) and "stock" not in t:
        return _cmd_periodo(True)
    if "stock" in t or "cuanto" in t or "queda" in t or "quedan" in t or "hay" in t or "unidades" in t:
        # Extraer el nombre del producto quitando las palabras de consulta
        relleno = {
            "stock", "de", "del", "la", "el", "los", "las", "cuanto", "cuantos", "cuanta",
            "cuantas", "queda", "quedan", "hay", "unidades", "unidad", "producto", "productos",
            "bodega", "vending", "me", "dime", "decir", "saber", "tiene", "tienen", "en",
            "quiero", "ver", "consultar", "por", "favor", "cuanto", "q", "y",
        }
        palabras = [p for p in t.split() if p not in relleno and len(p) > 2]
        if palabras:
            return _buscar_producto(" ".join(palabras))
        return _cmd_stock("")
    return ("No entendí ese mensaje 🤔\n\n"
            "Prueba con cosas como:\n"
            "• «stock de coca cola»\n"
            "• «cuánto queda de vital»\n"
            "• «productos críticos»\n"
            "• «egresos de hoy»\n\n"
            "O escribe /ayuda para ver los comandos.")


def telegram_command(text, chat_id):
    if not text.startswith("/"):
        try:
            return _respuesta_libre(text)
        except SessionExpiredError:
            return "⚠️ La sesión de Parrotfy expiró. Hay que actualizar la cookie en la plataforma."
        except Exception as e:
            return "⚠️ Error consultando datos: " + str(e)[:200]
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower().lstrip("/").split("@")[0]
    arg = parts[1].strip() if len(parts) > 1 else ""
    try:
        if cmd in ("start", "ayuda", "help"):
            return ("🤖 <b>Bot Bodega Vending</b>\n\n"
                    "<b>Puedes escribirme con tus palabras</b>, por ejemplo:\n"
                    "• «stock de coca cola»\n"
                    "• «cuánto queda de vital»\n"
                    "• «productos críticos»\n"
                    "• «sin stock»\n"
                    "• «egresos de hoy» / «ingresos de hoy»\n"
                    "• «totales del mes»\n\n"
                    "<b>Comandos:</b>\n"
                    "/stock — resumen de stock\n"
                    "/stock coca — busca un producto\n"
                    "/criticos — productos con menos de 20 unidades\n"
                    "/sin — productos de tu lista en 0\n"
                    "/hoy — egresos e ingresos de hoy\n"
                    "/mes — totales del mes\n\n"
                    "También te avisaré automáticamente cuando un producto quede en estado crítico.")
        if cmd == "stock":
            return _cmd_stock(arg)
        if cmd == "criticos":
            return _cmd_criticos()
        if cmd == "sin":
            return _cmd_sin()
        if cmd == "hoy":
            return _cmd_periodo(True)
        if cmd == "mes":
            return _cmd_periodo(False)
    except SessionExpiredError:
        return "⚠️ La sesión de Parrotfy expiró. Hay que actualizar la cookie en la plataforma."
    except Exception as e:
        return "⚠️ Error consultando datos: " + str(e)[:200]
    return "No reconozco ese comando. Escribe /ayuda"


_telegram = None


def _start_telegram():
    global _telegram
    token = _get_telegram_token()
    if not token:
        return
    # En local se puede desactivar con "telegram_activo": false (para que el bot
    # corra solo en la nube y no choquen los dos)
    if os.environ.get("TELEGRAM_TOKEN"):
        activo = True
    else:
        try:
            activo = load_config().get("telegram_activo", True)
        except Exception:
            activo = True
    if not activo:
        return
    _telegram = TelegramBot(token, CHATS_PATH, telegram_command, check_alerts)
    _telegram.start()


_start_telegram()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
