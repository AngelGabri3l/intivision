"""
Administración desde la terminal. Se ejecuta DENTRO del contenedor del backend:

  docker compose exec backend python -m backend.manage create-community \
      --name "Cuadra 01" --slug cuadra-01 --district-ubigeo 150131 --district-name "San Isidro"
  docker compose exec -it backend python -m backend.manage create-user \
      --community-slug cuadra-01 --role community_admin --name "Nombre Apellido" --email correo@ejemplo.com
  docker compose exec backend python -m backend.manage create-edge-device \
      --community-slug cuadra-01 --name mini-cuadra-01
  docker compose exec backend python -m backend.manage list-users
  docker compose exec -it backend python -m backend.manage set-password --user correo@ejemplo.com
  docker compose exec backend python -m backend.manage set-status --user correo@ejemplo.com --status suspended

La contraseña se pide por teclado (no se ve y no queda en el historial). Para automatizar se puede
pasar el nombre de una variable de entorno con --password-env.
"""
import argparse
import getpass
import os
import secrets
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import auth, db  # noqa: E402


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


def main():
    ap = argparse.ArgumentParser(description="Administración de VIGÍA")
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
