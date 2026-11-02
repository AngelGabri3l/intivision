import argparse
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import db  # noqa: E402


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

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
