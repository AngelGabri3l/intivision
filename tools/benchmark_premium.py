"""
Cuántas cámaras aguanta esta mini PC con el Plan Base y con el Premium.

Se corre DENTRO del contenedor del agente (tiene el motor y los modelos):
    docker compose exec agent python tools/benchmark_premium.py --salida /data/benchmark.csv

Mide cuánto tarda en analizar un cuadro el motor normal (Base) y el preciso con el detector de armas (Premium), sobre
imágenes con personas (las de ejemplo de Ultralytics: bus.jpg y zidane.jpg, o las que se indiquen con --imagen), y calcula
cuántas cámaras puede analizar a la vez a los cuadros por segundo pedidos (por defecto 3 por cámara, como el agente).
"""
from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def imagenes_de_ejemplo(rutas):
    import cv2
    if not rutas:
        try:
            from ultralytics.utils import ASSETS
            rutas = [str(ASSETS / "bus.jpg"), str(ASSETS / "zidane.jpg")]
        except Exception:   # noqa: BLE001
            rutas = []
    cuadros = []
    for r in rutas:
        c = cv2.imread(r)
        if c is not None:
            alto, ancho = c.shape[:2]
            cuadros.append(cv2.resize(c, (640, round(alto * 640 / ancho))))
    if not cuadros:
        sys.exit("No hay imágenes para medir: indica alguna con --imagen.")
    return cuadros


def medir(analizar, cuadros, repeticiones):
    """Milisegundos por cuadro (mediana y percentil 90), tras un calentamiento."""
    for c in cuadros:
        analizar(c)
    tiempos = []
    for i in range(repeticiones):
        c = cuadros[i % len(cuadros)]
        t0 = time.perf_counter(); analizar(c); tiempos.append((time.perf_counter() - t0) * 1000)
    tiempos.sort()
    return statistics.median(tiempos), tiempos[min(len(tiempos) - 1, int(len(tiempos) * 0.9))]


def capacidad(ms_por_cuadro, fps_por_camara, uso_max=0.8):
    """Cámaras que caben usando como mucho el 80 % del tiempo de análisis."""
    if ms_por_cuadro <= 0:
        return 0
    return int((1000 * uso_max) // (ms_por_cuadro * fps_por_camara))


def principal(argv=None, cargar_normal=None, cargar_preciso=None, crear_armas=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--imagen", action="append", help="imagen con personas (se puede repetir)")
    ap.add_argument("--repeticiones", type=int, default=30)
    ap.add_argument("--fps", type=float, default=3.0, help="cuadros por segundo por cámara (como analysis.fps del agente)")
    ap.add_argument("--salida", help="archivo CSV con los resultados")
    args = ap.parse_args(argv)
    from edge.agent.analizador import cargar_motor
    from edge.agent.armas import DetectorArmas
    cargar_normal = cargar_normal or (lambda: cargar_motor())
    cargar_preciso = cargar_preciso or (lambda: cargar_motor(None, {"detector": {"model": os.environ.get("PRECISE_MODEL", "yolo11s.pt")}}))
    crear_armas = crear_armas or (lambda: DetectorArmas(os.environ.get("WEAPONS_MODEL")))
    cuadros = imagenes_de_ejemplo(args.imagen)
    filas = []
    normal = cargar_normal().new_session("/tmp/benchmark_normal")
    med, p90 = medir(normal.process, cuadros, args.repeticiones)
    filas.append(("Base (motor normal)", med, p90))
    preciso = cargar_preciso().new_session("/tmp/benchmark_preciso")
    armas = crear_armas()
    med, p90 = medir(lambda c: (preciso.process(c), armas.detectar(c)), cuadros, args.repeticiones)
    filas.append(("Premium (motor preciso + armas)", med, p90))
    print(f"Análisis a {args.fps:g} cuadros por segundo por cámara (con personas en la imagen, el peor caso):")
    salida = []
    for nombre, med, p90 in filas:
        cap = capacidad(p90, args.fps)
        print(f"  {nombre}: {med:.0f} ms por cuadro (p90 {p90:.0f} ms) -> hasta {cap} cámara(s)")
        salida.append({"modo": nombre, "ms_mediana": round(med, 1), "ms_p90": round(p90, 1), "fps_por_camara": args.fps, "camaras_max": cap})
    base, prem = salida[0]["ms_p90"], salida[1]["ms_p90"]
    print("Mezcla: cada cámara Premium ocupa lo que %.1f cámaras Base." % (prem / base if base else 0))
    if args.salida:
        with open(args.salida, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(salida[0])); w.writeheader(); w.writerows(salida)
        print("Resultados en", args.salida)
    return salida


if __name__ == "__main__":
    principal()
