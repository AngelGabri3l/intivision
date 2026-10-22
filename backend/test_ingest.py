"""
test_ingest.py — Prueba del endpoint /ingest del backend.

Simula un evento del detector en vivo y lo envia al backend, para verificar que
queda guardado y aparece en el panel junto a los eventos de video.

Uso (con el backend corriendo en localhost:8000):
    python backend/test_ingest.py
"""
import requests

BACKEND = "http://localhost:8000"

evento = {
    "zone": "Cuadra 01 - Piloto",
    "level": "ALTO",
    "score": 6,
    "reasons": ["persona detectada", "en zona restringida", "horario sensible"],
    "people": 1,
}

if __name__ == "__main__":
    r = requests.post(f"{BACKEND}/ingest", json=evento, timeout=10)
    print("Respuesta /ingest:", r.status_code, r.json())
    hist = requests.get(f"{BACKEND}/events", timeout=10).json()
    print("Eventos en el historial:", len(hist))
    if hist:
        print("Ultimo evento:", hist[0]["level"], "-", hist[0]["reasons"])
