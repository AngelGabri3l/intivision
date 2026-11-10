"""Comandos del agente:  python -m agent  <listar | leer | vigilar>

  listar    detecta las cámaras una vez y las muestra (o las imprime en JSON)
  leer      abre UNA cámara unos segundos y muestra cuántos cuadros por segundo entrega
  vigilar   deja el agente corriendo: avisa cuando una cámara aparece o desaparece y lee
            las cámaras asignadas en agent.yaml
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time

from . import __version__
from .capture import Captura, GestorCaptura
from .config import ConfigError, cargar
from .inventory import Inventario, Vigilante, sondear_una_vez
from .sources import construir_descubridores

log = logging.getLogger("agent")
ESTADOS_ES = {"available": "disponible", "disconnected": "desconectada", "error": "error"}


def _hora() -> str:
    return time.strftime("%H:%M:%S")


def _imprimir_tabla(fuentes) -> None:
    filas = [("TIPO", "ESTADO", "NOMBRE", "IDENTIFICADOR", "RESOLUCIÓN")]
    for f in fuentes:
        filas.append((f.kind, ESTADOS_ES.get(f.state, f.state), f.label, f.device_key, f.resolucion()))
    anchos = [max(len(fila[i]) for fila in filas) for i in range(5)]
    for i, fila in enumerate(filas):
        print("  ".join(celda.ljust(anchos[j]) for j, celda in enumerate(fila)).rstrip())
        if i == 0:
            print("  ".join("-" * a for a in anchos))
    notas = [f for f in fuentes if f.detail]
    for f in notas:
        print(f"  ! {f.label}: {f.detail}")


def _pista_si_no_hay_usb(fuentes) -> None:
    if any(f.kind == "usb" for f in fuentes):
        return
    dev_root = os.environ.get("AGENT_DEV_ROOT", "/dev")
    try:
        hay_video = os.path.isdir(os.path.join(dev_root, "v4l")) or any(n.startswith("video") for n in os.listdir(dev_root))
    except OSError:
        hay_video = False
    if not hay_video:
        print("  ! No hay dispositivos de video visibles. En Docker, ¿montaste /dev (volumes: - /dev:/dev) "
              "y corres en Linux? Docker Desktop en Windows no le pasa las cámaras a los contenedores.", file=sys.stderr)


def cmd_listar(args, cfg) -> int:
    inv = Inventario()
    sondear_una_vez(inv, construir_descubridores(cfg, incluir_onvif=not args.sin_onvif))
    if args.json:
        print(json.dumps(inv.snapshot(), ensure_ascii=False, indent=2))
        return 0
    fuentes = inv.fuentes()
    if not fuentes:
        print("0 cámaras")
        _pista_si_no_hay_usb(fuentes)
        return 0
    _imprimir_tabla(fuentes)
    por_tipo = {t: sum(1 for f in fuentes if f.kind == t) for t in ("usb", "onvif", "rtsp")}
    print(f"\n{len(fuentes)} cámaras ({por_tipo['usb']} usb, {por_tipo['onvif']} onvif, {por_tipo['rtsp']} rtsp)")
    return 0


def _buscar(inv: Inventario, texto: str):
    todas = inv.fuentes()
    exactas = [f for f in todas if f.device_key == texto]
    if exactas:
        return exactas[0], []
    parecidas = [f for f in todas if texto.lower() in f.device_key.lower() or texto.lower() in f.label.lower()]
    return (parecidas[0], []) if len(parecidas) == 1 else (None, parecidas)


def cmd_leer(args, cfg) -> int:
    inv = Inventario()
    sondear_una_vez(inv, construir_descubridores(cfg, incluir_onvif=False))
    info, candidatas = _buscar(inv, args.fuente)
    if info is None:
        print(f"No encuentro una cámara única para '{args.fuente}'." if candidatas else f"No encuentro '{args.fuente}'.", file=sys.stderr)
        for f in (candidatas or inv.fuentes()):
            print(f"  - {f.device_key}   ({f.label})", file=sys.stderr)
        return 2
    if not info.uri:
        print(f"{info.label}: todavía no se puede leer su video (falta usuario y contraseña).", file=sys.stderr)
        return 2
    if info.state != "available":
        print(f"{info.label}: no está disponible ({ESTADOS_ES.get(info.state, info.state)}: {info.detail or 'sin detalle'}).", file=sys.stderr)
        return 1

    captura = Captura(info, cfg.width, cfg.height, cfg.fps)
    captura.start()
    inicio = time.monotonic()
    try:
        while time.monotonic() - inicio < args.segundos:
            time.sleep(min(2.0, max(0.1, args.segundos - (time.monotonic() - inicio))))
            e = captura.estadisticas()
            print(f"[{_hora()}] {info.label}: {e['cuadros']} cuadros, {e['fps']} fps, {e['ancho']}x{e['alto']}"
                  + (f", error: {e['error']}" if e["error"] else ""))
    except KeyboardInterrupt:
        pass
    e = captura.estadisticas()
    duracion = max(time.monotonic() - inicio, 0.001)
    if args.guardar:
        import cv2
        cuadro, _ = captura.ultimo()
        if cuadro is not None and cv2.imwrite(args.guardar, cuadro):
            print(f"muestra guardada en {args.guardar}")
    captura.detener()
    captura.join(timeout=3)
    print(f"\nresultado: {e['cuadros']} cuadros en {duracion:.0f} s, {e['cuadros'] / duracion:.1f} fps de promedio, "
          f"{e['ancho']}x{e['alto']}, reconexiones: {e['reconexiones']}")
    return 0 if e["cuadros"] > 0 else 1


def _escribir_inventario(inv: Inventario, data_dir: str) -> None:
    ruta = os.path.join(data_dir, "inventory.json")
    tmp = ruta + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(inv.snapshot(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, ruta)
    except OSError as e:
        log.warning("no pude escribir %s: %s", ruta, e)


def cmd_vigilar(args, cfg) -> int:
    inv = Inventario()
    gestor = GestorCaptura(inv, cfg.assigned, cfg.width, cfg.height, cfg.fps)
    simbolo = {"aparecio": "+ apareció", "desaparecio": "- desapareció", "reaparecio": "+ volvió", "cambio": "~ cambió"}

    def al_cambiar(eventos) -> None:
        for ev in eventos:
            f = ev.fuente
            extra = f" [{ESTADOS_ES.get(f.state, f.state)}]" if ev.tipo == "cambio" else ""
            print(f"[{_hora()}] {simbolo[ev.tipo]}: {f.kind}  {f.label}  ({f.device_key}){extra}", flush=True)
        _escribir_inventario(inv, cfg.data_dir)
        gestor.sincronizar()

    vigilante = Vigilante(inv, construir_descubridores(cfg), al_cambiar)
    parar = threading.Event()
    for senal in (signal.SIGINT, signal.SIGTERM):
        signal.signal(senal, lambda *_: parar.set())

    print(f"agente {__version__} vigilando; asignadas: {cfg.assigned or 'ninguna'}", flush=True)
    vigilante.start()
    inicio = time.monotonic()
    try:
        while not parar.wait(10):
            gestor.sincronizar()
            for clave, e in gestor.estados().items():
                print(f"[{_hora()}] leyendo {clave}: {e['fps']} fps, {e['ancho']}x{e['alto']}, edad {e['edad_s']} s"
                      + (f", error: {e['error']}" if e["error"] else ""), flush=True)
            if args.segundos and time.monotonic() - inicio >= args.segundos:
                break
    finally:
        vigilante.detener()
        gestor.detener_todo()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agent", description="Agente de la mini PC de VIGÍA")
    p.add_argument("--config", help="ruta de agent.yaml (por defecto: $AGENT_CONFIG o /data/agent.yaml)")
    p.add_argument("-v", "--verbose", action="store_true", help="registro detallado")
    sub = p.add_subparsers(dest="comando", required=True)

    s = sub.add_parser("listar", help="detecta y muestra las cámaras")
    s.add_argument("--json", action="store_true", help="imprime el inventario en JSON (el que se envía al servidor)")
    s.add_argument("--sin-onvif", action="store_true", help="no busca cámaras ONVIF (ahorra unos 3 segundos)")
    s.set_defaults(func=cmd_listar)

    s = sub.add_parser("leer", help="lee una cámara unos segundos y mide sus cuadros por segundo")
    s.add_argument("fuente", help="identificador completo o una parte del nombre o del identificador")
    s.add_argument("--segundos", type=float, default=10, help="cuánto tiempo leer (por defecto 10)")
    s.add_argument("--guardar", metavar="ARCHIVO.jpg", help="guarda el último cuadro leído (para ver qué ve la cámara)")
    s.set_defaults(func=cmd_leer)

    s = sub.add_parser("vigilar", help="deja el agente corriendo")
    s.add_argument("--segundos", type=float, default=0, help="termina solo después de este tiempo (0 = sin límite)")
    s.set_defaults(func=cmd_vigilar)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    try:
        cfg = cargar(args.config)
    except ConfigError as e:
        print(f"Error de configuración: {e}", file=sys.stderr)
        return 2
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
