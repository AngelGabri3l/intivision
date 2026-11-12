"""Comandos del agente:  python -m agent  <listar | leer | motor | detectar | probar | enviar-prueba | cola | vigilar>

  listar         detecta las cámaras una vez y las muestra (o las imprime en JSON)
  leer           abre UNA cámara unos segundos y muestra cuántos cuadros por segundo entrega
  motor          comprueba que el motor de detección funciona y mide cuánto tarda en analizar un cuadro (no necesita cámara)
  detectar       lee UNA cámara y le aplica el motor SIN servidor: muestra personas, puntaje y nivel (sirve para calibrar)
  probar         comprueba la conexión con el servidor y que el token sea válido (no crea nada)
  enviar-prueba  manda UN evento de prueba con foto al servidor (y, con --dos-veces, lo reenvía para ver que no se duplica)
  cola           muestra los eventos pendientes de enviar (y los que el servidor rechazó)
  vigilar        deja el agente corriendo: detecta las cámaras, manda su inventario al servidor, lee las cámaras que el
                 administrador le asignó, las analiza con el motor y envía los eventos (guardándolos en una cola si no hay internet)

La conexión con el servidor se configura con las variables SERVER_URL y EDGE_TOKEN (en Docker: edge/.env).
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
from .analizador import Analizador, MotorNoDisponible, SesionCamara, cargar_motor
from .cliente import ClienteServidor, ErrorTransitorio, NoAutorizado, Rechazado
from .cola import ColaLocal
from .config import ConfigError, cargar
from .config_remota import ConfigRemota, normalizar
from .enviador import Enviador
from .evidencia import imagen_de_prueba
from .inventory import Inventario, Vigilante, sondear_una_vez
from .latido import Latido
from .relevo import Relevo, probar_relevo
from .simulador import SimuladorEventos, evento_de_prueba
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

    captura = Captura(info, cfg.width, cfg.height, cfg.fps, formato=args.formato or cfg.pixel_format)
    captura.start()
    inicio = time.monotonic()
    try:
        while time.monotonic() - inicio < args.segundos:
            time.sleep(min(2.0, max(0.1, args.segundos - (time.monotonic() - inicio))))
            e = captura.estadisticas()
            print(f"[{_hora()}] {info.label}: {e['cuadros']} cuadros, {e['fps']} fps, {e['ancho']}x{e['alto']}"
                  + (f", error: {e['error']}" if e["error"] else "")
                  + (f" | AVISO: {e['aviso']}" if e["aviso"] else ""), flush=True)
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
    print(f"\nresultado: {e['cuadros']} cuadros en {duracion:.0f} s, ahora entrega {e['fps']} fps "
          f"({e['cuadros'] / duracion:.1f} de promedio contando el arranque), "
          f"{e['ancho']}x{e['alto']}, reconexiones: {e['reconexiones']}"
          + (f", la cámara entrega {e['crudos_fps']} cuadros/s (se piden {cfg.fps})" if e["aviso"] else ""))
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


def _cliente(cfg):
    """El cliente del servidor, o None si no se configuró (SERVER_URL y EDGE_TOKEN). ConfigError si está a medias o mal."""
    par = cfg.servidor()
    return ClienteServidor(*par) if par else None


def _sin_servidor() -> int:
    print("Falta configurar el servidor: define SERVER_URL y EDGE_TOKEN (en Docker, en edge/.env; mira edge/.env.example).", file=sys.stderr)
    return 2


def _version(modulo: str) -> str:
    try:
        return getattr(__import__(modulo), "__version__", "?")
    except Exception:       # noqa: BLE001
        return "no instalado"


def _memoria_mb() -> int:
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for linea in f:
                if linea.startswith("VmRSS:"):
                    return int(linea.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return 0


def tempfile_dir(cfg) -> str:
    import tempfile
    os.makedirs(cfg.data_dir, exist_ok=True)
    return tempfile.mkdtemp(prefix="motor_", dir=cfg.data_dir)


def cmd_motor(args, cfg) -> int:
    """Autodiagnóstico del motor: lo carga, mide cuánto tarda en analizar un cuadro y dice cuántos cuadros por segundo aguanta."""
    import shutil
    import cv2
    import numpy as np
    inicio = time.perf_counter()
    try:
        motor = cargar_motor(cfg.engine_config)
    except MotorNoDisponible as e:
        print(f"el motor NO está disponible: {e}", file=sys.stderr)
        return 5
    carga = time.perf_counter() - inicio
    print(f"motor cargado en {carga:.1f} s | python {sys.version.split()[0]}, torch {_version('torch')}, ultralytics {_version('ultralytics')}, "
          f"opencv {cv2.__version__}, numpy {np.__version__}")
    print(f"modelos cargados: personas | postura: {'activada' if motor.pose else 'apagada'} | memoria: {_memoria_mb()} MB")
    rng = np.random.RandomState(0)
    cuadro = np.clip(np.linspace(0, 255, 640 * 480 * 3).reshape(480, 640, 3) + rng.randint(0, 40, (480, 640, 3)), 0, 255).astype(np.uint8)

    def medir(accion, veces=5):
        accion()                                    # la primera vez es más lenta (calentamiento): no cuenta
        tiempos = []
        for _ in range(veces):
            t0 = time.perf_counter()
            accion()
            tiempos.append((time.perf_counter() - t0) * 1000)
        return sorted(tiempos)[len(tiempos) // 2]

    ms_personas = medir(lambda: motor.detector.detect(cuadro))
    ms_pose = medir(lambda: motor.pose.analyze(cuadro)) if motor.pose else 0.0
    tmp = tempfile_dir(cfg)
    try:
        sesion = motor.new_session(tmp)
        ms_total = medir(lambda: sesion.process(cuadro))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    peor = ms_personas + ms_pose
    print(f"un cuadro de 640x480 tarda: detectar personas {ms_personas:.0f} ms" + (f" | postura (solo si hay una persona) {ms_pose:.0f} ms" if motor.pose else "")
          + f" | análisis completo sin personas {ms_total:.0f} ms")
    print(f"con una persona delante (peor caso): ~{peor:.0f} ms por cuadro, es decir, hasta ~{1000 / max(peor, 1):.1f} cuadros por segundo en total entre todas las cámaras")
    objetivo = cfg.analysis_fps
    if peor > 1000 / objetivo:
        print(f"AVISO: no alcanza para {objetivo:g} cuadros por segundo. Baja analysis.fps en agent.yaml o apaga la postura (pose.enabled: false en ENGINE_CONFIG).")
    else:
        print(f"OK: alcanza para {objetivo:g} cuadros por segundo con una cámara; con N cámaras se reparte entre ellas.")
    print(f"memoria al terminar: {_memoria_mb()} MB")
    return 0


def cmd_detectar(args, cfg) -> int:
    """Lee una cámara y le aplica el motor, sin servidor: para ver qué detecta y calibrar."""
    import shutil
    inv = Inventario()
    sondear_una_vez(inv, construir_descubridores(cfg, incluir_onvif=False))
    info, candidatas = _buscar(inv, args.fuente)
    if info is None:
        print(f"No encuentro una cámara única para '{args.fuente}'." if candidatas else f"No encuentro '{args.fuente}'.", file=sys.stderr)
        for f in (candidatas or inv.fuentes()):
            print(f"  - {f.device_key}   ({f.label})", file=sys.stderr)
        return 2
    if not info.uri or info.state != "available":
        print(f"{info.label}: no se puede leer ahora ({ESTADOS_ES.get(info.state, info.state)}: {info.detail or 'sin detalle'}).", file=sys.stderr)
        return 1
    try:
        motor = cargar_motor(cfg.engine_config)
    except MotorNoDisponible as e:
        print(f"el motor NO está disponible: {e}", file=sys.stderr)
        return 5
    motor.event_cooldown_s = cfg.analysis_event_cooldown_s
    destino = args.guardar_eventos or os.path.join(cfg.data_dir, "muestras")
    os.makedirs(destino, exist_ok=True)
    tmp = tempfile_dir(cfg)
    sesion = SesionCamara(motor, tmp)
    captura = Captura(info, cfg.width, cfg.height, cfg.fps, formato=cfg.pixel_format)
    captura.start()
    print(f"analizando {info.label} durante {args.segundos:g} s a ~{cfg.analysis_fps:g} cuadros/s (Ctrl+C para parar). Párate frente a la cámara.", flush=True)
    periodo, inicio, ultimo_cuadro, ultimo_aviso = 1.0 / cfg.analysis_fps, time.monotonic(), None, 0.0
    analizados, eventos, mejor = 0, 0, "NINGUNO"
    orden = {"NINGUNO": 0, "BAJO": 1, "MEDIO": 2, "ALTO": 3}
    try:
        while time.monotonic() - inicio < args.segundos:
            t0 = time.monotonic()
            cuadro, instante = captura.ultimo()
            if cuadro is not None and instante != ultimo_cuadro:
                ultimo_cuadro = instante
                res = sesion.procesar(cuadro)
                analizados += 1
                if orden.get(res.get("level"), 0) > orden[mejor]:
                    mejor = res["level"]
                ev = res.get("event")
                if ev:
                    eventos += 1
                    origen = os.path.join(tmp, os.path.basename(ev["image"]))
                    copia = os.path.join(destino, f"{time.strftime('%Y%m%d_%H%M%S')}_{ev['level']}.jpg")
                    try:
                        shutil.move(origen, copia)
                    except OSError:
                        copia = "(no se pudo guardar la foto)"
                    print(f"[{_hora()}] *** EVENTO {ev['level']} (puntaje {ev['score']}): {ev['reasons']} -> {copia}", flush=True)
                if time.monotonic() - ultimo_aviso >= 1.0:
                    ultimo_aviso = time.monotonic()
                    print(f"[{_hora()}] personas {res.get('people', 0)} | puntaje {res.get('score', 0)} | nivel {res.get('level')}"
                          + (f" | {', '.join(res.get('reasons') or [])}" if res.get("reasons") else "") + f" | {sesion.ms_promedio():.0f} ms por cuadro", flush=True)
            time.sleep(max(0.0, periodo - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        captura.detener()
        captura.join(timeout=3)
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nresultado: {analizados} cuadros analizados ({sesion.ms_promedio():.0f} ms de promedio), {eventos} evento(s), nivel más alto: {mejor}")
    return 0 if analizados else 1


def cmd_probar(args, cfg) -> int:
    try:
        cliente = _cliente(cfg)
    except ConfigError as e:
        print(f"Error de configuración: {e}", file=sys.stderr)
        return 2
    if cliente is None:
        return _sin_servidor()
    print(f"servidor: {cliente.base_url}")
    try:
        bruta = cliente.obtener_config()
    except NoAutorizado:
        print("el servidor NO acepta el token: es inválido o fue revocado. Pide uno nuevo con create-edge-device.", file=sys.stderr)
        return 3
    except ErrorTransitorio as e:
        print(f"no se pudo hablar con el servidor: {e}", file=sys.stderr)
        return 4
    except Rechazado as e:
        print(f"el servidor rechazó la petición: {e}", file=sys.stderr)
        return 4
    remota = normalizar(bruta)
    if remota is None:
        print("el servidor respondió, pero no con una configuración que este agente entienda (¿otra versión?).", file=sys.stderr)
        return 4
    print("token: válido")
    print(f"cámaras asignadas a esta mini PC: {len(remota['cameras'])}")
    for c in remota["cameras"]:
        print(f"  - {c['name'] or '(sin nombre)'}  {c['device_key']}  [{c['kind']}]")
    if remota["sensitive_hours"]:
        print(f"horario sensible: de las {remota['sensitive_hours'][0]}:00 a las {remota['sensitive_hours'][1]}:00")
    if args.inventario:
        inv = Inventario()
        sondear_una_vez(inv, construir_descubridores(cfg, incluir_onvif=False))
        try:
            r = cliente.enviar_inventario(inv.snapshot())
        except (ErrorTransitorio, NoAutorizado, Rechazado) as e:
            print(f"no se pudo enviar el inventario: {e}", file=sys.stderr)
            return 4
        print(f"inventario enviado: {r.get('sources')} cámara(s), {r.get('new')} nueva(s) para el servidor")
    if args.relevo:
        ok, mensaje = probar_relevo(cliente.base_url, cfg.edge_token)
        print(f"relevo de imagen en vivo: {mensaje}", file=sys.stdout if ok else sys.stderr)
        if not ok:
            return 4
    return 0


def cmd_enviar_prueba(args, cfg) -> int:
    import tempfile
    import uuid
    try:
        cliente = _cliente(cfg)
    except ConfigError as e:
        print(f"Error de configuración: {e}", file=sys.stderr)
        return 2
    if cliente is None:
        return _sin_servidor()
    try:
        camaras = (normalizar(cliente.obtener_config()) or {"cameras": []})["cameras"]
    except NoAutorizado:
        print("el servidor NO acepta el token.", file=sys.stderr)
        return 3
    except (ErrorTransitorio, Rechazado) as e:
        print(f"no se pudo obtener la configuración: {e}", file=sys.stderr)
        return 4
    if args.camara:
        camaras = [c for c in camaras if args.camara.lower() in (c["device_key"] + " " + c["name"] + " " + c["camera_id"]).lower()]
    if len(camaras) != 1:
        print("No hay una cámara única para el evento de prueba." if camaras else
              "No hay cámaras asignadas a esta mini PC: asígnala primero (el administrador, en la API /admin/sources).", file=sys.stderr)
        for c in camaras:
            print(f"  - {c['name']}  {c['device_key']}", file=sys.stderr)
        return 2
    cam = camaras[0]
    if args.foto:
        try:
            datos = open(args.foto, "rb").read()
        except OSError as e:
            print(f"no pude leer {args.foto}: {e}", file=sys.stderr)
            return 2
        if len(datos) > 500_000:
            print(f"{args.foto} pesa más de 500 KB: el servidor la rechazaría.", file=sys.stderr)
            return 2
    else:
        datos = imagen_de_prueba(f"PRUEBA DEL AGENTE {_hora()}")
    evento = evento_de_prueba(cam["camera_id"], args.nivel)
    evento["edge_event_id"] = str(uuid.uuid4())
    print(f"cámara: {cam['name']} ({cam['device_key']}), nivel {args.nivel}, evento {evento['edge_event_id'][:8]}…, foto de {len(datos)} bytes")
    with tempfile.NamedTemporaryFile(suffix=".jpg", dir=cfg.data_dir, delete=False) as f:
        f.write(datos)
        ruta = f.name
    try:
        for n in range(2 if args.dos_veces else 1):
            try:
                r = cliente.enviar_evento(evento, ruta)
            except NoAutorizado:
                print("el servidor NO acepta el token.", file=sys.stderr)
                return 3
            except (ErrorTransitorio, Rechazado) as e:
                print(f"no se pudo enviar: {e}", file=sys.stderr)
                return 4
            print(f"envío {n + 1}: recibido (evento {r.event_id}), " + ("DUPLICADO: el servidor lo reconoció y no lo guardó otra vez" if r.duplicate else "guardado como evento nuevo"))
    finally:
        try:
            os.remove(ruta)
        except OSError:
            pass
    return 0


def cmd_cola(args, cfg) -> int:
    if not os.path.exists(os.path.join(cfg.data_dir, "queue.db")):
        print("no hay cola todavía (el agente no ha guardado ningún evento).")
        return 0
    cola = ColaLocal(cfg.data_dir, int(cfg.queue_max_mb * 1e6), cfg.queue_max_age_h * 3600, recuperar=False)
    try:
        if args.vaciar:
            if not args.si:
                print("Esto BORRA los eventos pendientes de enviar. Detén el agente primero y repite con --si para confirmar.", file=sys.stderr)
                return 2
            print(f"se borraron {cola.vaciar()} evento(s) pendientes.")
            return 0
        e = cola.estadisticas()
        print(f"pendientes: {e['pendientes']}  ({e['bytes'] / 1024:.0f} KB, el más viejo hace {e['mas_viejo_s']:.0f} s)")
        print(f"descartados por el tope de la cola: {e['descartados']}   rechazados por el servidor: {e['rechazados']}")
        if args.rechazados:
            for r in cola.rechazados():
                print(f"  - {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r['rechazado']))}  {r['edge_event_id'][:8]}…  {r['motivo']}")
    finally:
        cola.cerrar()
    return 0


def cmd_vigilar(args, cfg) -> int:
    try:
        cliente = _cliente(cfg)
    except ConfigError as e:
        print(f"Error de configuración: {e}", file=sys.stderr)
        return 2
    inv = Inventario()
    # Con servidor, el administrador decide qué cámaras se leen; sin servidor, las de agent.yaml (como en H1).
    gestor = GestorCaptura(inv, [] if cliente else cfg.assigned, cfg.width, cfg.height, cfg.fps, formato=cfg.pixel_format)
    simbolo = {"aparecio": "+ apareció", "desaparecio": "- desapareció", "reaparecio": "+ volvió", "cambio": "~ cambió"}
    cola = remota = latido = enviador = simulador = analizador = relevo = None
    if cliente:
        cola = ColaLocal(cfg.data_dir, int(cfg.queue_max_mb * 1e6), cfg.queue_max_age_h * 3600)

        def aplicar_config(r) -> None:
            gestor.set_asignadas(r.device_keys())
            gestor.sincronizar()

        remota = ConfigRemota(os.path.join(cfg.data_dir, "config_cache.json"), al_cambiar=aplicar_config)
        latido = Latido(cliente, inv, remota, cola, cada_s=cfg.heartbeat_seconds)
        enviador = Enviador(cola, cliente, al_perder_camara=latido.despertar)
        if args.simular_eventos:
            simulador = SimuladorEventos(cola, remota, gestor, enviador, args.simular_eventos, args.nivel_simulado)
        if cfg.live_enabled:
            relevo = Relevo(cliente.base_url, cfg.edge_token, gestor, remota, cfg.live_width, cfg.live_jpeg_quality, cfg.live_max_streams)
        if cfg.analysis_enabled and not args.sin_deteccion:
            analizador = Analizador(lambda: cargar_motor(cfg.engine_config), gestor, remota, cola, enviador, os.path.join(cfg.data_dir, "tmp_evidencia"),
                                    cfg.analysis_fps, cfg.analysis_event_cooldown_s, cfg.analysis_max_gap_s)

    def al_cambiar(eventos) -> None:
        for ev in eventos:
            f = ev.fuente
            extra = f" [{ESTADOS_ES.get(f.state, f.state)}]" if ev.tipo == "cambio" else ""
            print(f"[{_hora()}] {simbolo[ev.tipo]}: {f.kind}  {f.label}  ({f.device_key}){extra}", flush=True)
        _escribir_inventario(inv, cfg.data_dir)
        gestor.sincronizar()
        if latido:
            latido.despertar()          # que el servidor se entere ya de que una cámara apareció o se fue

    vigilante = Vigilante(inv, construir_descubridores(cfg), al_cambiar)
    parar = threading.Event()
    for senal in (signal.SIGINT, signal.SIGTERM):
        signal.signal(senal, lambda *_: parar.set())

    if cliente:
        print(f"agente {__version__} vigilando; servidor {cliente.base_url}; las cámaras las asigna el servidor"
              + (f"; detección con el motor a ~{cfg.analysis_fps:g} cuadros/s" if analizador else "; SIN detección (solo cámaras)")
              + (f"; eventos de prueba cada {args.simular_eventos:g} s (nivel {args.nivel_simulado})" if simulador else ""), flush=True)
        remota.cargar_copia()
    else:
        print(f"agente {__version__} vigilando SIN servidor (modo local); asignadas: {cfg.assigned or 'ninguna'}", flush=True)
    vigilante.start()
    for hilo in (enviador, latido, simulador, analizador, relevo):
        if hilo:
            hilo.start()
    inicio = time.monotonic()
    try:
        while not parar.wait(10):
            gestor.sincronizar()
            for clave, e in gestor.estados().items():
                print(f"[{_hora()}] leyendo {clave}: {e['fps']} fps, {e['ancho']}x{e['alto']}, edad {e['edad_s']} s"
                      + (f", error: {e['error']}" if e["error"] else "")
                      + (f" | AVISO: {e['aviso']}" if e["aviso"] else ""), flush=True)
            if relevo:
                r = relevo.estado()
                print(f"[{_hora()}] relevo: {'conectado' if r['conectado'] else 'sin conexión'}, {r['flujos']} cámara(s) en vivo, {r['enviados']} cuadros enviados", flush=True)
            if analizador:
                a = analizador.estado()
                if a["motor"] == "listo":
                    print(f"[{_hora()}] análisis: {a['cuadros_por_s']} cuadros/s" + "".join(
                        f" | {c['nombre']}: nivel {c['nivel']}, {c['personas']} persona(s), {c['ms']} ms, {c['eventos']} evento(s)" for c in a["camaras"]), flush=True)
                else:
                    print(f"[{_hora()}] análisis: motor {a['motor']}" + (f" ({a['motivo']})" if a["motivo"] else ""), flush=True)
            if cliente:
                q, en = cola.estadisticas(), enviador.estado()
                print(f"[{_hora()}] servidor: cola {q['pendientes']}, enviados {en['enviados']}, fallos seguidos {en['fallos_seguidos']}, "
                      f"configuración {remota.version or 'sin recibir'} ({remota.origen or '-'})"
                      + (f", descartados {q['descartados']}" if q["descartados"] else ""), flush=True)
            if args.segundos and time.monotonic() - inicio >= args.segundos:
                break
    finally:
        vigilante.detener()
        for hilo in (relevo, analizador, simulador, latido, enviador):
            if hilo:
                hilo.detener()
        for hilo in (relevo, analizador, simulador, latido, enviador):
            if hilo and hilo.is_alive():
                hilo.join(timeout=5)
        gestor.detener_todo()
        if cola:
            cola.cerrar()
    return 0


class RedactarToken(logging.Filter):
    """Última defensa: si el token llegara a aparecer en un mensaje (o en el texto de una excepción), se tapa antes de escribirlo."""

    def __init__(self, token: str):
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            mensaje = record.getMessage()
        except Exception:       # noqa: BLE001
            return True
        if self.token in mensaje:
            record.msg, record.args = mensaje.replace(self.token, "***"), None
        if record.exc_info:
            record.exc_text = logging.Formatter().formatException(record.exc_info).replace(self.token, "***")
        return True


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
    s.add_argument("--formato", choices=["auto", "mjpg", "yuyv"],
                   help="formato de píxeles de una cámara USB (por defecto el de capture.pixel_format); prueba yuyv si hay 'Corrupt JPEG data'")
    s.set_defaults(func=cmd_leer)

    s = sub.add_parser("motor", help="comprueba el motor de detección y mide cuánto tarda en analizar un cuadro")
    s.set_defaults(func=cmd_motor)

    s = sub.add_parser("detectar", help="lee una cámara y le aplica el motor, sin servidor (para calibrar)")
    s.add_argument("fuente", help="identificador completo o una parte del nombre o del identificador")
    s.add_argument("--segundos", type=float, default=30, help="cuánto tiempo analizar (por defecto 30)")
    s.add_argument("--guardar-eventos", metavar="CARPETA", help="dónde guardar la foto de cada evento (por defecto, muestras/ dentro de data/)")
    s.set_defaults(func=cmd_detectar)

    s = sub.add_parser("probar", help="comprueba la conexión con el servidor y el token (no crea nada)")
    s.add_argument("--inventario", action="store_true", help="además manda el inventario de cámaras de este equipo")
    s.add_argument("--relevo", action="store_true", help="además comprueba la conexión del relevo de imagen en vivo (WebSocket)")
    s.set_defaults(func=cmd_probar)

    s = sub.add_parser("enviar-prueba", help="manda un evento de prueba con foto al servidor")
    s.add_argument("--camara", help="parte del nombre o del identificador de la cámara (si hay más de una asignada)")
    s.add_argument("--nivel", choices=["BAJO", "MEDIO", "ALTO"], default="BAJO",
                   help="nivel del evento (por defecto BAJO: queda en el historial pero no manda aviso por Telegram)")
    s.add_argument("--foto", metavar="ARCHIVO.jpg", help="una foto tuya (por defecto, una imagen de prueba)")
    s.add_argument("--dos-veces", action="store_true", help="lo manda dos veces: la segunda debe salir como DUPLICADO")
    s.set_defaults(func=cmd_enviar_prueba)

    s = sub.add_parser("cola", help="muestra los eventos pendientes de enviar")
    s.add_argument("--rechazados", action="store_true", help="lista también los que el servidor rechazó, con su motivo")
    s.add_argument("--vaciar", action="store_true", help="borra los eventos pendientes (con el agente detenido)")
    s.add_argument("--si", action="store_true", help="confirma --vaciar")
    s.set_defaults(func=cmd_cola)

    s = sub.add_parser("vigilar", help="deja el agente corriendo")
    s.add_argument("--segundos", type=float, default=0, help="termina solo después de este tiempo (0 = sin límite)")
    s.add_argument("--simular-eventos", type=float, default=0, metavar="SEGUNDOS",
                   help="cada tantos segundos crea un evento de prueba por cada cámara asignada (para probar la cola y el envío)")
    s.add_argument("--nivel-simulado", choices=["BAJO", "MEDIO", "ALTO"], default="BAJO",
                   help="nivel de los eventos de prueba (BAJO no manda aviso por Telegram)")
    s.add_argument("--sin-deteccion", action="store_true", help="no carga el motor: el agente solo lee cámaras y manda inventario (modo solo cámaras)")
    s.set_defaults(func=cmd_vigilar)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    try:
        cfg = cargar(args.config)
    except ConfigError as e:
        print(f"Error de configuración: {e}", file=sys.stderr)
        return 2
    if cfg.edge_token:
        for manejador in logging.getLogger().handlers:
            manejador.addFilter(RedactarToken(cfg.edge_token))
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
