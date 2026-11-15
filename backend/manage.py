"""
Administración desde la terminal. Se ejecuta DENTRO del contenedor del backend:

  docker compose exec backend python -m backend.manage create-community \
      --name "Cuadra 01" --slug cuadra-01 --district-ubigeo 150131 --district-name "San Isidro"
  docker compose exec -it backend python -m backend.manage create-user \
      --community-slug cuadra-01 --role community_admin --name "Nombre Apellido" --email correo@ejemplo.com
  docker compose exec backend python -m backend.manage create-edge-device \
      --community-slug cuadra-01 --name mini-cuadra-01
  docker compose exec backend python -m backend.manage list-users
  docker compose exec backend python -m backend.manage create-camera \
      --community-slug cuadra-01 --name "Entrada de la cuadra" --kind common --edge-device mini-cuadra-01
  docker compose exec backend python -m backend.manage create-camera \
      --community-slug cuadra-01 --name "Garaje" --kind private --owner correo@ejemplo.com
  docker compose exec backend python -m backend.manage list-cameras
  docker compose exec backend python -m backend.manage archivar-camaras-navegador            (simulacro)
  docker compose exec backend python -m backend.manage archivar-camaras-navegador --si       (archiva)
  docker compose exec backend python -m backend.manage demo-datos --community-slug cuadra-01 --eventos 300 --dias 30
  docker compose exec backend python -m backend.manage demo-borrar --community-slug cuadra-01
  docker compose exec -it backend python -m backend.manage set-password --user correo@ejemplo.com
  docker compose exec backend python -m backend.manage set-status --user correo@ejemplo.com --status suspended

La contraseña se pide por teclado (no se ve y no queda en el historial). Para automatizar se puede
pasar el nombre de una variable de entorno con --password-env.
"""
import argparse
import datetime
import getpass
import os
import random
import secrets
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import auth, cameras, db, mapa  # noqa: E402


def ask_password(args, *contexto):
    """Pide la contraseña y la rechaza si es débil. `contexto`: datos del usuario que no deben aparecer en ella."""
    if args.password_env:
        password = os.environ.get(args.password_env, "")
        if not password:
            sys.exit(f"La variable de entorno {args.password_env} está vacía.")
    else:
        password = getpass.getpass("Contraseña: ")
        if password != getpass.getpass("Repite la contraseña: "):
            sys.exit("Las contraseñas no coinciden.")
    problema = auth.password_issue(password, *contexto)
    if problema:
        sys.exit(problema)
    return password


def community_id(cur, slug):
    cur.execute("SELECT id FROM communities WHERE slug = %s", (slug,))
    row = cur.fetchone()
    if row is None:
        sys.exit(f"No existe la comunidad «{slug}». Créala primero con create-community.")
    return row["id"]


def create_community(args):
    with db.cursor() as cur:
        cur.execute("""INSERT INTO districts (ubigeo, department, province, name)
                       VALUES (%s,%s,%s,%s) ON CONFLICT (ubigeo) DO NOTHING""",
                    (args.district_ubigeo, args.department, args.province, args.district_name))
        cur.execute("""INSERT INTO communities (name, slug, district_ubigeo) VALUES (%s,%s,%s)
                       ON CONFLICT (slug) DO NOTHING RETURNING id""",
                    (args.name, args.slug, args.district_ubigeo))
        row = cur.fetchone()
    print("Comunidad creada." if row else "Esa comunidad ya existía; no se cambió nada.")


def list_communities(args):
    with db.cursor() as cur:
        cur.execute("""SELECT c.slug, c.name, d.name AS distrito,
                              (SELECT count(*) FROM events e WHERE e.community_id = c.id) AS eventos
                       FROM communities c JOIN districts d ON d.ubigeo = c.district_ubigeo
                       ORDER BY c.slug""")
        rows = cur.fetchall()
    for r in rows:
        print(f'{r["slug"]:<20} {r["name"]:<28} {r["distrito"]:<20} {r["eventos"]} eventos')
    if not rows:
        print("No hay comunidades.")


def create_user(args):
    if not (args.email or args.phone):
        sys.exit("Indica --email o --phone (o ambos).")
    password = ask_password(args, args.email, args.phone, args.name)
    with db.cursor() as cur:
        cid = community_id(cur, args.community_slug)
        cur.execute("""INSERT INTO users (community_id, role, full_name, email, phone, password_hash)
                       VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (cid, args.role, args.name, args.email, args.phone, auth.hash_password(password)))
        uid = cur.fetchone()["id"]
    print(f"Usuario creado ({args.role}): {uid}")


def create_edge_device(args):
    token = secrets.token_urlsafe(32)
    with db.cursor() as cur:
        cid = community_id(cur, args.community_slug)
        cur.execute("""INSERT INTO edge_devices (community_id, name, token_hash, status)
                       VALUES (%s,%s,%s,'active') RETURNING id""",
                    (cid, args.name, auth.hash_token(token)))
        did = cur.fetchone()["id"]
    print(f"Mini PC creada: {did}")
    print("Token (se muestra UNA sola vez; guárdalo en la configuración de la mini PC):")
    print(token)


def create_camera(args):
    nombre, lugar = cameras.clean_text(args.name), cameras.clean_text(args.place or "")
    problema = cameras.name_issue(nombre) or cameras.place_issue(lugar)
    if problema:
        sys.exit(problema)
    with db.cursor() as cur:
        cid = community_id(cur, args.community_slug)
        owner_id = edge_id = None
        if args.kind == "private":
            if not args.owner:
                sys.exit("Una cámara privada necesita --owner (correo o teléfono de su dueño).")
            cur.execute("""SELECT id FROM users WHERE community_id = %s
                           AND (lower(email) = lower(%s) OR phone = %s) LIMIT 1""",
                        (cid, args.owner.strip(), args.owner.strip()))
            row = cur.fetchone()
            if row is None:
                sys.exit("Ese usuario no existe en esa comunidad.")
            owner_id = row["id"]
        elif args.owner:
            sys.exit("Una cámara de zona común no lleva dueño.")
        if args.edge_device:
            cur.execute("SELECT id FROM edge_devices WHERE community_id = %s AND name = %s LIMIT 1",
                        (cid, args.edge_device))
            row = cur.fetchone()
            if row is None:
                sys.exit("No existe esa mini PC en esa comunidad (revisa el nombre).")
            edge_id = row["id"]
        source = args.source_type or ("rtsp" if edge_id else "browser")
        cur.execute("""INSERT INTO cameras (community_id, user_id, edge_device_id, kind, name, place, source_type)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (community_id, name) DO NOTHING RETURNING id""",
                    (cid, owner_id, edge_id, args.kind, nombre, lugar or None, source))
        row = cur.fetchone()
    print(f"Cámara creada: {row['id']}" if row else "Ya existía una cámara con ese nombre en esa comunidad.")


def list_cameras(args):
    with db.cursor() as cur:
        cur.execute("""SELECT cm.slug, c.kind, c.name, COALESCE(u.full_name, '-') AS dueno, c.source_type,
                              COALESCE(d.name, '-') AS equipo, c.is_active,
                              (SELECT count(*) FROM events e WHERE e.camera_id = c.id) AS eventos
                       FROM cameras c JOIN communities cm ON cm.id = c.community_id
                       LEFT JOIN users u ON u.id = c.user_id LEFT JOIN edge_devices d ON d.id = c.edge_device_id
                       WHERE (%s::text IS NULL OR cm.slug = %s) ORDER BY cm.slug, c.kind, c.name""",
                    (args.community_slug, args.community_slug))
        rows = cur.fetchall()
    for r in rows:
        print(f'{r["slug"]:<16} {r["kind"]:<8} {r["name"]:<26} {r["dueno"]:<22} {r["source_type"]:<8} '
              f'{r["equipo"]:<16} {"activa" if r["is_active"] else "inactiva":<9} {r["eventos"]} eventos')
    if not rows:
        print("No hay cámaras.")


def find_user(cur, identifier):
    cur.execute("SELECT id, full_name FROM users WHERE lower(email) = lower(%s) OR phone = %s LIMIT 1",
                (identifier.strip(), identifier.strip()))
    row = cur.fetchone()
    if row is None:
        sys.exit("No existe un usuario con ese correo o teléfono.")
    return row


def set_password(args):
    """Restablece la contraseña, desbloquea la cuenta y cierra las sesiones que tuviera abiertas."""
    with db.cursor() as cur:
        user = find_user(cur, args.user)
    password = ask_password(args, args.user, user["full_name"])
    with db.cursor() as cur:
        cur.execute("""UPDATE users SET password_hash = %s, failed_attempts = 0, locked_until = NULL
                       WHERE id = %s""", (auth.hash_password(password), user["id"]))
        cur.execute("UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                    (user["id"],))
    print(f"Contraseña de «{user['full_name']}» cambiada. Se cerraron sus sesiones abiertas y se desbloqueó la cuenta.")


def set_status(args):
    """Suspende o reactiva una cuenta. Una cuenta suspendida no puede entrar y pierde sus sesiones."""
    with db.cursor() as cur:
        user = find_user(cur, args.user)
        cur.execute("UPDATE users SET status = %s WHERE id = %s", (args.status, user["id"]))
        if args.status != "active":
            cur.execute("UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                        (user["id"],))
    print(f"«{user['full_name']}» ahora está: {args.status}.")


def list_users(args):
    with db.cursor() as cur:
        cur.execute("""SELECT c.slug, u.role, u.full_name, COALESCE(u.email, u.phone) AS contacto,
                              u.status, u.last_login_at
                       FROM users u JOIN communities c ON c.id = u.community_id
                       ORDER BY c.slug, u.role, u.full_name""")
        rows = cur.fetchall()
    for r in rows:
        print(f'{r["slug"]:<18} {r["role"]:<16} {r["full_name"]:<24} {r["contacto"]:<30} {r["status"]}')
    if not rows:
        print("No hay usuarios.")


# ---------------------------------------------------------------- cámaras del navegador (H11)

def archivar_camaras_navegador(args):
    """
    Archiva las cámaras que creó el navegador (tipo «browser» o «file»): «Analizar» y la cámara del celular o la computadora ya no existen,
    así que esas cámaras no sirven. NO se borra ningún evento. Sin --si es un simulacro: solo cuenta y muestra.
    """
    with db.cursor() as cur:
        filtro, parametros = "", []
        if args.community_slug:
            filtro = " AND c.community_id = %s"
            parametros.append(community_id(cur, args.community_slug))
        cur.execute(f"""SELECT c.id::text AS id, c.name,
                               (SELECT count(*) FROM events e WHERE e.camera_id = c.id) AS eventos
                        FROM cameras c WHERE c.is_active AND c.source_type IN ('browser', 'file'){filtro} ORDER BY c.name""", parametros)
        filas = cur.fetchall()
        total_eventos = sum(int(f["eventos"]) for f in filas)
        print(f"{len(filas)} cámara(s) activa(s) creadas por el navegador, con {total_eventos} evento(s) en total.")
        for f in filas[:20]:
            print(f"  - {f['name']} ({f['eventos']} evento(s))")
        if len(filas) > 20:
            print(f"  ... y {len(filas) - 20} más")
        if not args.si:
            print("SIMULACRO: no se cambió nada. Para archivarlas, ejecuta el mismo comando con --si.")
            return
        for f in filas:
            cur.execute("""UPDATE cameras SET is_active = false,
                                  name = left(name, 35) || ' (archivada ' || to_char(now(), 'YYYY-MM-DD') || ' ' || left(id::text, 8) || ')'
                           WHERE id = %s""", (f["id"],))
            mapa.limpiar_ubicacion(cur, f["id"])           # una cámara archivada pierde su ubicación
    print(f"Listo: {len(filas)} cámara(s) archivada(s). Sus eventos se conservan.")


# ---------------------------------------------------------------- datos de demostración (H8)
# Los eventos de demostración llevan un edge_event_id con este comienzo fijo (la columna es un UUID, no admite texto libre), para
# poder borrarlos sin tocar los reales. 20 caracteres fijos: no hay forma práctica de que un identificador real empiece así.
DEMO_PREFIJO = "deadc0de-0000-4000-8000-"
LIMA = datetime.timezone(datetime.timedelta(hours=-5))
_SENALES = [("loitering", 5), ("proximity", 2), ("group", 2), ("hands_up", 1), ("fallen", 1), ("climbing", 1),
            ("in_restricted_zone", 2), ("sensitive_hour", 3)]


def _elegir(rnd, pares):
    total = sum(p for _, p in pares)
    x = rnd.uniform(0, total)
    for valor, peso in pares:
        x -= peso
        if x <= 0:
            return valor
    return pares[-1][0]


def demo_datos(args):
    """
    Siembra eventos FALSOS (con la hora, el nivel, el estado y las señales repartidos como en la vida real: más de noche y en fin de
    semana) para ensayar el panel y sacar capturas sin esperar incidentes reales. Quedan marcados y se borran con demo-borrar.
    """
    if not 1 <= args.eventos <= 20000:
        sys.exit("--eventos debe estar entre 1 y 20000.")
    if not 1 <= args.dias <= 366:
        sys.exit("--dias debe estar entre 1 y 366.")
    rnd = random.Random(args.semilla)
    ahora = datetime.datetime.now(datetime.timezone.utc)
    with db.cursor() as cur:
        cid = community_id(cur, args.community_slug)
        # Solo en las cámaras que el administrador puede ver: en una que su dueño le oculta, el panel no mostraría nada.
        cur.execute("""SELECT id::text AS id FROM cameras WHERE community_id = %s AND is_active
                       AND (kind = 'common' OR admin_can_view) ORDER BY name""", (cid,))
        camaras = [r["id"] for r in cur.fetchall()]
        if not camaras:
            sys.exit("Esa comunidad no tiene cámaras activas que el administrador pueda ver. Crea o asigna una primero (create-camera o el panel de Equipos).")
        cur.execute("SELECT id::text AS id FROM users WHERE community_id = %s AND role = 'community_admin' AND status = 'active' ORDER BY created_at LIMIT 1", (cid,))
        admin = cur.fetchone()
        revisor = admin["id"] if admin else None
        horas = [(h, 6 if (h >= 22 or h <= 5) else (2 if 18 <= h <= 21 else 1)) for h in range(24)]
        for k in range(args.eventos):
            while True:                                                       # viernes, sábado y domingo concentran más incidentes
                dia = rnd.randint(0, args.dias - 1)
                local = (ahora.astimezone(LIMA) - datetime.timedelta(days=dia)).replace(hour=0, minute=0, second=0, microsecond=0)
                if rnd.random() < (1.0 if local.weekday() >= 4 else 0.55):
                    break
            hora = _elegir(rnd, horas)
            ocurrio = (local + datetime.timedelta(hours=hora, minutes=rnd.randint(0, 59), seconds=rnd.randint(0, 59))).astimezone(datetime.timezone.utc)
            ocurrio = min(ocurrio, ahora - datetime.timedelta(minutes=1))
            alto = rnd.random() < 0.30
            nivel, puntaje = ("ALTO", rnd.randint(6, 9)) if alto else ("MEDIO", rnd.randint(3, 5))
            estado = _elegir(rnd, [("falsa alarma", 15), ("verificado", 25), ("sin verificar", 60)])
            revisado = ocurrio + datetime.timedelta(seconds=rnd.randint(20, 400)) if estado != "sin verificar" else None
            escalado = ocurrio + datetime.timedelta(seconds=180) if (estado == "sin verificar" and rnd.random() < 0.08) else None
            cur.execute("""INSERT INTO events (community_id, camera_id, source, level, score, people_count, status, reviewed_at, reviewed_by,
                                               escalated_at, occurred_at, edge_event_id)
                           VALUES (%s,%s,'edge',%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (cid, rnd.choice(camaras), nivel, puntaje, _elegir(rnd, [(1, 5), (2, 3), (3, 2), (4, 1)]), estado, revisado,
                         revisor if revisado else None, escalado, ocurrio, DEMO_PREFIJO + "%012x" % rnd.getrandbits(48)))
            ev = cur.fetchone()["id"]
            cur.execute("INSERT INTO event_signals (event_id, signal_code) VALUES (%s, 'person_present')", (ev,))
            for codigo in {_elegir(rnd, _SENALES) for _ in range(rnd.randint(1, 2))}:
                detalle = f"merodeo {rnd.randint(15, 60)}s" if codigo == "loitering" else (f"grupo de {rnd.randint(3, 5)}" if codigo == "group" else None)
                cur.execute("INSERT INTO event_signals (event_id, signal_code, detail) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (ev, codigo, detalle))
    print(f"Listo: {args.eventos} eventos de DEMOSTRACIÓN en «{args.community_slug}» (últimos {args.dias} días, sobre {len(camaras)} cámara(s)).")
    print("Se borran con:  python -m backend.manage demo-borrar --community-slug", args.community_slug)


def demo_borrar(args):
    """Borra SOLO los eventos de demostración de la comunidad (con sus señales); los reales no se tocan."""
    with db.cursor() as cur:
        cid = community_id(cur, args.community_slug)
        cur.execute("DELETE FROM events WHERE community_id = %s AND edge_event_id::text LIKE %s", (cid, DEMO_PREFIJO + "%"))
        n = cur.rowcount
    print(f"Se borraron {n} eventos de demostración de «{args.community_slug}».")


def main():
    ap = argparse.ArgumentParser(description="Administración de INTIVISION")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-community")
    p.add_argument("--name", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--district-ubigeo", required=True)
    p.add_argument("--district-name", required=True)
    p.add_argument("--department", default="Lima")
    p.add_argument("--province", default="Lima")
    p.set_defaults(fn=create_community)

    p = sub.add_parser("list-communities")
    p.set_defaults(fn=list_communities)

    p = sub.add_parser("create-user")
    p.add_argument("--community-slug", required=True)
    p.add_argument("--role", choices=auth.ROLES, default="resident")
    p.add_argument("--name", required=True)
    p.add_argument("--email")
    p.add_argument("--phone")
    p.add_argument("--password-env")
    p.set_defaults(fn=create_user)

    p = sub.add_parser("create-edge-device")
    p.add_argument("--community-slug", required=True)
    p.add_argument("--name", required=True)
    p.set_defaults(fn=create_edge_device)

    p = sub.add_parser("list-users")
    p.set_defaults(fn=list_users)

    p = sub.add_parser("create-camera")
    p.add_argument("--community-slug", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--place")
    p.add_argument("--kind", choices=["private", "common"], default="common")
    p.add_argument("--owner", help="correo o teléfono del dueño (solo cámaras privadas)")
    p.add_argument("--edge-device", help="nombre de la mini PC que alimenta esta cámara")
    p.add_argument("--source-type", choices=["browser", "rtsp", "file"])
    p.set_defaults(fn=create_camera)

    p = sub.add_parser("list-cameras")
    p.add_argument("--community-slug")
    p.set_defaults(fn=list_cameras)

    p = sub.add_parser("archivar-camaras-navegador", help="archiva las cámaras que creó el navegador (por defecto solo cuenta; con --si las archiva)")
    p.add_argument("--community-slug")
    p.add_argument("--si", action="store_true", help="aplicar de verdad; sin esto es un simulacro")
    p.set_defaults(fn=archivar_camaras_navegador)

    p = sub.add_parser("demo-datos", help="siembra eventos falsos de demostración para ensayar el panel")
    p.add_argument("--community-slug", required=True)
    p.add_argument("--eventos", type=int, default=300)
    p.add_argument("--dias", type=int, default=30)
    p.add_argument("--semilla", type=int, default=1, help="la misma semilla da los mismos datos")
    p.set_defaults(fn=demo_datos)

    p = sub.add_parser("demo-borrar", help="borra los eventos de demostración (los reales no se tocan)")
    p.add_argument("--community-slug", required=True)
    p.set_defaults(fn=demo_borrar)

    p = sub.add_parser("set-password")
    p.add_argument("--user", required=True, help="correo o teléfono del usuario")
    p.add_argument("--password-env")
    p.set_defaults(fn=set_password)

    p = sub.add_parser("set-status")
    p.add_argument("--user", required=True, help="correo o teléfono del usuario")
    p.add_argument("--status", required=True, choices=["active", "suspended", "canceled"])
    p.set_defaults(fn=set_status)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
