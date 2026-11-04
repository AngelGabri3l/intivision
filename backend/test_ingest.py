"""
test_ingest.py — Prueba manual del endpoint /ingest del backend.

Desde VIGIA-23, /ingest exige el token de la mini PC (cada equipo tiene el suyo). Para crearlo:

    docker compose exec backend python -m backend.manage create-edge-device \
        --community-slug cuadra-01 --name mini-cuadra-01

Uso (con el backend corriendo):
    EDGE_TOKEN=<token> BACKEND=https://intivision.online python backend/test_ingest.py

El evento queda en la comunidad de esa mini PC, como cámara de zona común: lo ve el
administrador de la comunidad, no los residentes.
"""
import os
import sys

import requests

BACKEND = os.environ.get("BACKEND", "http://localhost:8000")
TOKEN = os.environ.get("EDGE_TOKEN")

evento = {
    "level": "ALTO",
    "score": 6,
    "reasons": ["persona detectada", "en zona restringida", "horario sensible"],
    "people": 1,
}

if __name__ == "__main__":
    if not TOKEN:
        sys.exit("Define EDGE_TOKEN con el token de la mini PC (ver el encabezado de este archivo).")
    r = requests.post(f"{BACKEND}/ingest", json=evento,
                      headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10)
    print("Respuesta /ingest:", r.status_code, r.json())
