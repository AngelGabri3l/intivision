#!/usr/bin/env python3
"""
evaluar_clips.py — Mide el motor de VIGÍA sobre clips grabados y dice, señal por señal, qué acertó, qué falsa alarma dio y qué se le escapó.

Es el arnés para AFINAR el motor con datos y no a ojo (H3), y produce la tabla de aciertos y falsas alarmas que pide H10.

Cómo se usa (desde la carpeta del proyecto, con las librerías del motor instaladas: ultralytics, opencv):

    python tools/evaluar_clips.py --manifiesto clips/manifiesto.yaml
    python tools/evaluar_clips.py --manifiesto clips/manifiesto.yaml --salida resultados.csv --cache clips/.cache
    python tools/evaluar_clips.py --manifiesto clips/manifiesto.yaml --set scoring.proximity_seconds=2 --set pose.every_n=2
    python tools/evaluar_clips.py --manifiesto clips/manifiesto.yaml --barrido scoring.proximity_seconds=1,1.5,2,3

El manifiesto (mira tools/manifiesto.ejemplo.yaml) lista los clips y lo que se espera de cada uno. Los clips tienen personas: se graban con su
consentimiento y NO se suben al repositorio (van al ticket de la historia).

Cómo funciona: cada clip se pasa UNA vez por los modelos YOLO (lo lento) a `--fps` cuadros por segundo, y esas detecciones se guardan. El puntaje
(lo rápido) se calcula después sobre lo guardado, así que probar otro valor de scoring.* no repite el trabajo de los modelos. Los parámetros que
cambian lo que VEN los modelos (detector.*, pose.*) piden volver a pasar los clips: usa --set y vuelve a correr.

Código de salida: 0 si todo lo esperado se cumplió, 1 si algún clip no cumplió o no se pudo leer, 2 si el manifiesto es inválido, 3 si no están las
librerías del motor.
"""
import argparse
import copy
import csv
import datetime
import hashlib
import json
import os
import sys
import time

import yaml

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

NIVELES = ["NINGUNO", "BAJO", "MEDIO", "ALTO"]
CODIGOS = [("persona detectada", "person_present"), ("en zona restringida", "in_restricted_zone"), ("horario sensible", "sensitive_hour"),
           ("merodeo", "loitering"), ("grupo de", "group"), ("cercanía entre personas", "proximity"), ("trepando", "climbing"),
           ("persona en el suelo", "fallen"), ("posible asalto", "hands_up")]
SENALES = {codigo for _, codigo in CODIGOS}
CLAVES_ESPERADO = {"nivel_minimo", "nivel_maximo", "debe_tener", "no_debe_tener"}
CLAVES_CLIP = {"archivo", "descripcion", "hora", "zona", "esperado"}


class ManifiestoError(Exception):
    pass


class ClipError(Exception):
    pass


# ------------------------------------------------------------------ manifiesto
def _hora(valor, donde, errores):
    if valor is None:
        return (12, 0)
    try:
        if isinstance(valor, int) and not isinstance(valor, bool):
            h, m = valor, 0
        else:
            h, m = (int(x) for x in str(valor).strip().split(":"))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return (h, m)
    except ValueError:
        pass
    errores.append(f"{donde}: 'hora' debe ser HH:MM entre 00:00 y 23:59, no {valor!r}")
    return (12, 0)


def _lista_senales(valor, donde, errores):
    if valor is None:
        return []
    if not isinstance(valor, list) or not all(isinstance(x, str) for x in valor):
        errores.append(f"{donde} debe ser una lista de señales")
        return []
    malas = [x for x in valor if x not in SENALES]
    if malas:
        errores.append(f"{donde}: señales que no existen: {', '.join(malas)} (las válidas: {', '.join(sorted(SENALES))})")
    return [x for x in valor if x in SENALES]


def cargar_manifiesto(ruta):
    """Lee y VALIDA el manifiesto de clips. Si algo está mal, avisa de todo junto (no de a un error por corrida)."""
    try:
        with open(ruta, encoding="utf-8") as f:
            datos = yaml.safe_load(f)
    except OSError as e:
        raise ManifiestoError(f"no se pudo leer el manifiesto {ruta}: {e}") from None
    except yaml.YAMLError as e:
        raise ManifiestoError(f"el manifiesto no es un YAML válido: {e}") from None
    if not isinstance(datos, dict) or not isinstance(datos.get("clips"), list) or not datos["clips"]:
        raise ManifiestoError("el manifiesto debe tener una lista 'clips' con al menos un clip")
    base = os.path.dirname(os.path.abspath(ruta))
    errores, clips, vistos = [], [], set()
    for k, c in enumerate(datos["clips"], 1):
        donde = f"clip {k}"
        if not isinstance(c, dict) or not isinstance(c.get("archivo"), str) or not c["archivo"].strip():
            errores.append(f"{donde}: falta 'archivo'")
            continue
        donde = f"clip {k} ({c['archivo']})"
        if c["archivo"] in vistos:
            errores.append(f"{donde}: el archivo está repetido")
        vistos.add(c["archivo"])
        for extra in sorted(set(c) - CLAVES_CLIP):
            errores.append(f"{donde}: clave desconocida '{extra}'")
        zona = c.get("zona")
        if zona is not None:
            try:
                zona = [[float(x), float(y)] for x, y in zona]
                if len(zona) < 3:
                    raise ValueError
            except (TypeError, ValueError):
                errores.append(f"{donde}: 'zona' debe ser una lista de al menos 3 puntos [x, y]")
                zona = None
        esp = c.get("esperado") or {}
        if not isinstance(esp, dict):
            errores.append(f"{donde}: 'esperado' debe ser un conjunto de claves")
            esp = {}
        for extra in sorted(set(esp) - CLAVES_ESPERADO):
            errores.append(f"{donde}: 'esperado' no conoce '{extra}' (usa {', '.join(sorted(CLAVES_ESPERADO))})")
        niveles = {}
        for clave in ("nivel_minimo", "nivel_maximo"):
            v = esp.get(clave)
            if v is not None and v not in NIVELES:
                errores.append(f"{donde}: {clave} debe ser uno de {', '.join(NIVELES)}, no {v!r}")
                v = None
            niveles[clave] = v
        if niveles["nivel_minimo"] and niveles["nivel_maximo"] and NIVELES.index(niveles["nivel_minimo"]) > NIVELES.index(niveles["nivel_maximo"]):
            errores.append(f"{donde}: nivel_minimo no puede ser mayor que nivel_maximo")
        debe = _lista_senales(esp.get("debe_tener"), f"{donde}: debe_tener", errores)
        no_debe = _lista_senales(esp.get("no_debe_tener"), f"{donde}: no_debe_tener", errores)
        if set(debe) & set(no_debe):
            errores.append(f"{donde}: una señal no puede estar a la vez en debe_tener y en no_debe_tener: {', '.join(sorted(set(debe) & set(no_debe)))}")
        ruta_clip = c["archivo"] if os.path.isabs(c["archivo"]) else os.path.join(base, c["archivo"])
        clips.append({"archivo": ruta_clip, "nombre": os.path.splitext(os.path.basename(c["archivo"]))[0], "descripcion": str(c.get("descripcion") or ""),
                      "hora": _hora(c.get("hora"), donde, errores), "zona": zona,
                      "esperado": {"nivel_minimo": niveles["nivel_minimo"], "nivel_maximo": niveles["nivel_maximo"], "debe_tener": debe, "no_debe_tener": no_debe},
                      "informativo": not (niveles["nivel_minimo"] or niveles["nivel_maximo"] or debe or no_debe)})
    if errores:
        raise ManifiestoError("el manifiesto tiene errores:\n  - " + "\n  - ".join(errores))
    config = datos.get("config")
    return {"base": base, "config": (config if os.path.isabs(config) else os.path.join(base, config)) if isinstance(config, str) else None, "clips": clips}


# ------------------------------------------------------------------ configuración
def aplicar_override(cfg, asignacion):
    """Aplica 'scoring.proximity_seconds=2' sobre una copia de la configuración. El valor se lee como YAML (2, 0.5, true, null, [1, 2])."""
    if "=" not in asignacion:
        raise ManifiestoError(f"--set espera clave=valor, no {asignacion!r}")
    clave, _, valor = asignacion.partition("=")
    ruta = [p for p in clave.strip().split(".") if p]
    if not ruta:
        raise ManifiestoError(f"--set: la clave está vacía en {asignacion!r}")
    try:
        valor = yaml.safe_load(valor)
    except yaml.YAMLError:
        pass
    nuevo = copy.deepcopy(cfg)
    nodo = nuevo
    for p in ruta[:-1]:
        if p not in nodo or nodo[p] is None:
            nodo[p] = {}
        if not isinstance(nodo[p], dict):
            raise ManifiestoError(f"--set {clave}: '{p}' no es una sección sino un valor")
        nodo = nodo[p]
    nodo[ruta[-1]] = valor
    return nuevo


def cargar_config(ruta):
    with open(ruta, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ------------------------------------------------------------------ paso 1: los modelos (lento)
def _huella(clip_ruta, fps, cfg):
    st = os.stat(clip_ruta)
    relevante = {"detector": cfg.get("detector"), "pose": cfg.get("pose"), "fps": fps, "tam": st.st_size, "mtime": int(st.st_mtime)}
    return hashlib.sha1(json.dumps(relevante, sort_keys=True, default=str).encode()).hexdigest()[:16]


def extraer(motor, clip_ruta, fps_objetivo, cache=None, cfg=None):
    """
    Pasa el clip por los modelos a `fps_objetivo` cuadros por segundo y devuelve lo que vieron, listo para calcular el puntaje sin repetirlo:
    {"cuadros": [(t, personas, posturas)], "ms": [ms por cuadro], "duracion_s", "desde_cache"}.
    """
    import cv2
    ruta_cache = None
    if cache and cfg is not None and os.path.exists(clip_ruta):
        os.makedirs(cache, exist_ok=True)
        ruta_cache = os.path.join(cache, f"{os.path.splitext(os.path.basename(clip_ruta))[0]}.{_huella(clip_ruta, fps_objetivo, cfg)}.json")
        if os.path.exists(ruta_cache):
            with open(ruta_cache, encoding="utf-8") as f:
                d = json.load(f)
            cuadros = [(t, [{"bbox": tuple(p["bbox"]), "conf": p["conf"]} for p in personas],
                        None if poses is None else [{"postures": set(x["postures"]), "bbox": x["bbox"]} for x in poses]) for t, personas, poses in d["cuadros"]]
            return {"cuadros": cuadros, "ms": d["ms"], "duracion_s": d["duracion_s"], "desde_cache": True}
    cap = cv2.VideoCapture(clip_ruta)
    if not cap.isOpened():
        raise ClipError(f"no se pudo abrir el clip: {clip_ruta}")
    fps_video = cap.get(cv2.CAP_PROP_FPS) or 25.0
    paso = max(1, int(round(fps_video / max(fps_objetivo, 0.1))))
    cuadros, ms, idx, tick = [], [], 0, 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            idx += 1
            if (idx - 1) % paso:
                continue
            t0 = time.perf_counter()
            personas, poses = motor.inferir(frame, tick)
            ms.append((time.perf_counter() - t0) * 1000.0)
            tick += 1
            cuadros.append(((idx - 1) / fps_video,
                            [{"bbox": tuple(int(v) for v in d["bbox"]), "conf": float(d.get("conf", 1.0))} for d in personas],
                            None if poses is None else [{"postures": set(p["postures"]), "bbox": [int(v) for v in p["bbox"]]} for p in poses]))
    finally:
        cap.release()
    duracion = (idx / fps_video) if idx else 0.0
    if ruta_cache:
        with open(ruta_cache, "w", encoding="utf-8") as f:
            json.dump({"cuadros": [(t, [{"bbox": list(p["bbox"]), "conf": p["conf"]} for p in personas],
                                    None if poses is None else [{"postures": sorted(x["postures"]), "bbox": x["bbox"]} for x in poses]) for t, personas, poses in cuadros],
                       "ms": ms, "duracion_s": duracion}, f)
    return {"cuadros": cuadros, "ms": ms, "duracion_s": duracion, "desde_cache": False}


# ------------------------------------------------------------------ paso 2: el puntaje (rápido)
def _codigo(motivo):
    low = motivo.lower()
    for prefijo, codigo in CODIGOS:
        if low.startswith(prefijo):
            return codigo
    return None


def reproducir(extraccion, clip, kwargs_puntaje):
    """Calcula el puntaje sobre lo que vieron los modelos. Devuelve el nivel máximo, las señales vistas y cuándo apareció cada una."""
    from threat_scoring import ThreatScorer
    kw = dict(kwargs_puntaje)
    if clip.get("zona"):
        kw["restricted_zone"] = clip["zona"]
    sc = ThreatScorer(**kw)
    ahora = datetime.datetime(2026, 1, 1, clip["hora"][0], clip["hora"][1])
    nivel_max, puntaje_max, vistas, primer_medio = 0, 0, {}, None
    for t, personas, poses in extraccion["cuadros"]:
        r = sc.evaluate([dict(d) for d in personas], t, ahora, None if poses is None else [{"postures": set(p["postures"]), "bbox": list(p["bbox"])} for p in poses])
        nivel = NIVELES.index(r["level"])
        nivel_max = max(nivel_max, nivel)
        puntaje_max = max(puntaje_max, r["score"])
        if nivel >= NIVELES.index("MEDIO") and primer_medio is None:
            primer_medio = t
        for m in r["reasons"]:
            c = _codigo(m)
            if c and c not in vistas:
                vistas[c] = round(t, 2)
    return {"nivel_max": NIVELES[nivel_max], "puntaje_max": puntaje_max, "senales": vistas, "primer_medio_s": None if primer_medio is None else round(primer_medio, 2),
            "cuadros": len(extraccion["cuadros"])}


def juzgar(resultado, esperado):
    """Compara lo que pasó con lo que se esperaba."""
    visto = set(resultado["senales"])
    idx = NIVELES.index(resultado["nivel_max"])
    fallos = []
    if esperado.get("nivel_maximo") and idx > NIVELES.index(esperado["nivel_maximo"]):
        fallos.append(f"el nivel llegó a {resultado['nivel_max']} y se esperaba como máximo {esperado['nivel_maximo']}")
    if esperado.get("nivel_minimo") and idx < NIVELES.index(esperado["nivel_minimo"]):
        fallos.append(f"el nivel solo llegó a {resultado['nivel_max']} y se esperaba al menos {esperado['nivel_minimo']}")
    aciertos = [s for s in esperado.get("debe_tener", []) if s in visto]
    no_detectado = [s for s in esperado.get("debe_tener", []) if s not in visto]
    falsas = [s for s in esperado.get("no_debe_tener", []) if s in visto]
    fallos += [f"no se detectó {s}" for s in no_detectado] + [f"falsa alarma: apareció {s}" for s in falsas]
    return {"ok": not fallos, "fallos": fallos, "aciertos": aciertos, "no_detectado": no_detectado, "falsas_alarmas": falsas,
            "correctos_ausentes": [s for s in esperado.get("no_debe_tener", []) if s not in visto]}


def resumen(filas):
    """Los totales por señal: aciertos, lo que se escapó, falsas alarmas y ausencias correctas."""
    por_senal = {}
    for f in filas:
        if f.get("error") or f["clip"]["informativo"]:
            continue
        j = f["juicio"]
        for clave, campo in (("aciertos", "aciertos"), ("no_detectado", "no_detectado"), ("falsas_alarmas", "falsas_alarmas"), ("correctos", "correctos_ausentes")):
            for s in j[campo]:
                por_senal.setdefault(s, {"aciertos": 0, "no_detectado": 0, "falsas_alarmas": 0, "correctos": 0})[clave] += 1
    juzgados = [f for f in filas if not f.get("error") and not f["clip"]["informativo"]]
    return {"por_senal": por_senal, "clips": len(filas), "juzgados": len(juzgados), "ok": sum(1 for f in juzgados if f["juicio"]["ok"]),
            "informativos": sum(1 for f in filas if not f.get("error") and f["clip"]["informativo"]), "con_error": sum(1 for f in filas if f.get("error")),
            "niveles_fallidos": sum(1 for f in juzgados if any("nivel" in x for x in f["juicio"]["fallos"]))}


def evaluar(manifiesto, extracciones, kwargs_puntaje):
    filas = []
    for clip in manifiesto["clips"]:
        e = extracciones.get(clip["archivo"])
        if isinstance(e, str):
            filas.append({"clip": clip, "error": e})
            continue
        r = reproducir(e, clip, kwargs_puntaje)
        filas.append({"clip": clip, "resultado": r, "juicio": juzgar(r, clip["esperado"]), "ms": e["ms"], "duracion_s": e["duracion_s"]})
    return filas


# ------------------------------------------------------------------ salida
def tabla(filas):
    lineas = []
    for f in filas:
        c = f["clip"]
        if f.get("error"):
            lineas.append(f"  ERROR     {c['nombre']}: {f['error']}")
            continue
        r, j = f["resultado"], f["juicio"]
        marca = "INFO     " if c["informativo"] else ("CUMPLE   " if j["ok"] else "NO CUMPLE")
        senales = ", ".join(f"{s}@{t:g}s" for s, t in sorted(r["senales"].items(), key=lambda x: x[1]) if s != "person_present") or "(ninguna)"
        lineas.append(f"  {marca} {c['nombre']:<28} nivel {r['nivel_max']:<7} puntaje {r['puntaje_max']:<2} señales: {senales}")
        for x in j["fallos"]:
            lineas.append(f"            ↳ {x}")
    return "\n".join(lineas)


def tabla_resumen(res, filas):
    lineas = [f"  Clips: {res['clips']} | juzgados: {res['juzgados']} (cumplen {res['ok']}) | informativos: {res['informativos']} | con error: {res['con_error']}"]
    if res["por_senal"]:
        lineas.append(f"  {'señal':<20}{'aciertos':>9}{'no detectado':>14}{'falsas alarmas':>16}{'ausencias correctas':>21}")
        for s, v in sorted(res["por_senal"].items()):
            lineas.append(f"  {s:<20}{v['aciertos']:>9}{v['no_detectado']:>14}{v['falsas_alarmas']:>16}{v['correctos']:>21}")
    ms = [m for f in filas if not f.get("error") for m in f["ms"]]
    if ms:
        medio = sum(ms) / len(ms)
        lineas.append(f"  Costo medio de los modelos: {medio:.0f} ms por cuadro analizado (~{1000.0 / max(medio, 1e-9):.1f} cuadros por segundo en una sola cámara)")
    return "\n".join(lineas)


def escribir_csv(ruta, filas):
    with open(ruta, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["clip", "descripcion", "estado", "nivel_max", "puntaje_max", "senales_vistas", "primer_medio_s", "nivel_minimo", "nivel_maximo",
                    "debe_tener", "no_debe_tener", "fallos", "cuadros_analizados", "ms_por_cuadro"])
        for fl in filas:
            c = fl["clip"]
            esp = c["esperado"]
            if fl.get("error"):
                w.writerow([c["nombre"], c["descripcion"], "ERROR", "", "", "", "", "", "", "", "", fl["error"], "", ""])
                continue
            r, j = fl["resultado"], fl["juicio"]
            ms = sum(fl["ms"]) / len(fl["ms"]) if fl["ms"] else 0
            w.writerow([c["nombre"], c["descripcion"], "INFO" if c["informativo"] else ("CUMPLE" if j["ok"] else "NO CUMPLE"), r["nivel_max"], r["puntaje_max"],
                        ";".join(f"{s}@{t:g}" for s, t in sorted(r["senales"].items(), key=lambda x: x[1])), r["primer_medio_s"] if r["primer_medio_s"] is not None else "",
                        esp["nivel_minimo"] or "", esp["nivel_maximo"] or "", ";".join(esp["debe_tener"]), ";".join(esp["no_debe_tener"]), " | ".join(j["fallos"]),
                        r["cuadros"], f"{ms:.0f}"])


def barrido(manifiesto, extracciones, cfg, clave, valores):
    """Prueba varios valores de UN parámetro del puntaje sobre lo ya extraído. Devuelve una fila por valor."""
    from engine import scorer_kwargs
    if not clave.startswith("scoring."):
        raise ManifiestoError(f"--barrido solo prueba parámetros de scoring.*; '{clave}' cambia lo que ven los modelos: corre el arnés de nuevo con --set {clave}=VALOR")
    filas = []
    for v in valores:
        cfg_v = aplicar_override(cfg, f"{clave}={v}")
        ev = evaluar(manifiesto, extracciones, scorer_kwargs(cfg_v))
        res = resumen(ev, )
        filas.append({"valor": v, "cumplen": res["ok"], "juzgados": res["juzgados"],
                      "falsas_alarmas": sum(x["falsas_alarmas"] for x in res["por_senal"].values()),
                      "no_detectado": sum(x["no_detectado"] for x in res["por_senal"].values()), "niveles_fallidos": res["niveles_fallidos"]})
    return filas


# ------------------------------------------------------------------ programa
def main(argv=None):
    ap = argparse.ArgumentParser(description="Mide el motor de VIGÍA sobre clips grabados.")
    ap.add_argument("--manifiesto", required=True, help="YAML con los clips y lo que se espera de cada uno")
    ap.add_argument("--config", help="configuración del motor (por defecto la del manifiesto, o config.yaml del proyecto)")
    ap.add_argument("--fps", type=float, default=3.0, help="cuadros por segundo a los que se analiza cada clip (por defecto 3)")
    ap.add_argument("--set", action="append", default=[], metavar="CLAVE=VALOR", help="cambia un valor de la configuración (se puede repetir)")
    ap.add_argument("--barrido", metavar="CLAVE=V1,V2,...", help="prueba varios valores de un parámetro scoring.* sobre lo ya extraído")
    ap.add_argument("--salida", help="escribe los resultados por clip en este CSV")
    ap.add_argument("--cache", help="carpeta donde guardar lo que vieron los modelos (volver a correr no los repite)")
    args = ap.parse_args(argv)
    try:
        manifiesto = cargar_manifiesto(args.manifiesto)
        ruta_cfg = args.config or manifiesto["config"] or os.path.join(RAIZ, "config.yaml")
        cfg = cargar_config(ruta_cfg)
        for a in args.set:
            cfg = aplicar_override(cfg, a)
    except (ManifiestoError, OSError, yaml.YAMLError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    try:
        from engine import GuardianEngine, scorer_kwargs
        motor = GuardianEngine(cfg)
    except ImportError as e:
        print(f"ERROR: faltan las librerías del motor ({e}). Instálalas (ultralytics, opencv-python) o corre esto donde corre el motor.", file=sys.stderr)
        return 3
    print(f"Configuración: {os.path.relpath(ruta_cfg)}" + (f" con {', '.join(args.set)}" if args.set else "") + f" | {args.fps:g} cuadros/s | "
          + ("un solo modelo" if motor.single_model else "detector + postura" if motor.pose else "solo detector"))
    extracciones = {}
    for clip in manifiesto["clips"]:
        try:
            extracciones[clip["archivo"]] = extraer(motor, clip["archivo"], args.fps, args.cache, cfg)
            e = extracciones[clip["archivo"]]
            print(f"  analizado {clip['nombre']}: {len(e['cuadros'])} cuadros" + (" (de la caché)" if e["desde_cache"] else ""))
        except ClipError as e:
            extracciones[clip["archivo"]] = str(e)
            print(f"  ERROR {clip['nombre']}: {e}")
    filas = evaluar(manifiesto, extracciones, scorer_kwargs(cfg))
    res = resumen(filas)
    print("\nResultado por clip:\n" + tabla(filas))
    print("\nResumen:\n" + tabla_resumen(res, filas))
    if args.salida:
        escribir_csv(args.salida, filas)
        print(f"\nResultados guardados en {args.salida}")
    if args.barrido:
        clave, _, lista = args.barrido.partition("=")
        valores = [v.strip() for v in lista.split(",") if v.strip()]
        if not clave or not valores:
            print("ERROR: --barrido espera CLAVE=V1,V2,...", file=sys.stderr)
            return 2
        try:
            fb = barrido(manifiesto, extracciones, cfg, clave, valores)
        except ManifiestoError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        print(f"\nBarrido de {clave}:\n  {'valor':<10}{'cumplen':>9}{'falsas alarmas':>16}{'no detectado':>14}{'niveles fallidos':>18}")
        for f in fb:
            print(f"  {str(f['valor']):<10}{str(f['cumplen']) + '/' + str(f['juzgados']):>9}{f['falsas_alarmas']:>16}{f['no_detectado']:>14}{f['niveles_fallidos']:>18}")
    return 0 if (res["ok"] == res["juzgados"] and not res["con_error"]) else 1


if __name__ == "__main__":
    sys.exit(main())
