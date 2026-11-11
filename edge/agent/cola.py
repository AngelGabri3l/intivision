"""
Cola local de eventos pendientes de enviar.

Si no hay internet (o el servidor no responde), los eventos NO se pierden: se guardan aquí, en disco, y se envían
en orden cuando vuelve la conexión, aunque el agente se reinicie o el equipo se apague.

  • Los datos van en SQLite (queue.db, modo WAL, con confirmación a disco) y cada foto en su propio archivo.
  • Se envían en el MISMO orden en que llegaron: si el primero no se puede enviar, los demás esperan.
  • Tiene dos topes, de tamaño y de antigüedad. Si se pasan, se descarta lo MÁS VIEJO y se cuenta cuántos.
  • Un evento que el servidor rechaza para siempre (por ejemplo, una foto inválida) se aparta en «rechazados»
    para que no bloquee a los demás.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass

log = logging.getLogger("agent.cola")

MAX_RECHAZADOS = 200            # cuántos rechazados se recuerdan, para diagnosticar


@dataclass
class Item:
    id: int
    edge_event_id: str
    payload: dict
    foto_ruta: str | None
    intentos: int
    creado: float


class ColaLocal:
    def __init__(self, directorio: str, max_bytes: int, max_edad_s: float, reloj=time.time, recuperar: bool = True):
        self.directorio = directorio
        self.dir_fotos = os.path.join(directorio, "fotos")
        self.max_bytes, self.max_edad_s = int(max_bytes), float(max_edad_s)
        self._reloj = reloj
        self._lock = threading.RLock()
        os.makedirs(self.dir_fotos, exist_ok=True)
        self._db = sqlite3.connect(os.path.join(directorio, "queue.db"), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS eventos (
                id INTEGER PRIMARY KEY AUTOINCREMENT, edge_event_id TEXT NOT NULL UNIQUE, creado REAL NOT NULL,
                payload TEXT NOT NULL, foto TEXT, bytes INTEGER NOT NULL, intentos INTEGER NOT NULL DEFAULT 0,
                proximo REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS rechazados (
                id INTEGER PRIMARY KEY AUTOINCREMENT, edge_event_id TEXT, creado REAL, rechazado REAL, motivo TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS meta (clave TEXT PRIMARY KEY, valor INTEGER NOT NULL);
        """)
        if recuperar:       # el comando `cola` lo desactiva: no debe borrar fotos de un agente que está corriendo
            self._recuperar()

    # ------------------------------------------------------------------ lo que usa el resto del agente
    def agregar(self, payload: dict, foto: bytes | None = None) -> str:
        """Guarda un evento (y su foto) y devuelve su edge_event_id. Si ya estaba en la cola, no lo duplica."""
        payload = dict(payload)
        eid = str(payload.get("edge_event_id") or uuid.uuid4())
        payload["edge_event_id"] = eid
        with self._lock:
            if self._db.execute("SELECT 1 FROM eventos WHERE edge_event_id = ?", (eid,)).fetchone():
                return eid
            texto = json.dumps(payload, ensure_ascii=False)
            nombre = None
            if foto:
                nombre = f"{eid}.jpg"
                self._escribir_foto(nombre, foto)
            try:
                self._db.execute("INSERT INTO eventos (edge_event_id, creado, payload, foto, bytes) VALUES (?,?,?,?,?)",
                                 (eid, self._reloj(), texto, nombre, len(texto) + (len(foto) if foto else 0)))
            except BaseException:
                if nombre:
                    self._quitar_foto(nombre)
                raise
            self._aplicar_topes()
        return eid

    def siguiente(self, ahora: float | None = None) -> Item | None:
        """El evento más viejo, si ya toca reintentarlo. Si no toca, devuelve None y los demás esperan: el orden se respeta."""
        ahora = self._reloj() if ahora is None else ahora
        with self._lock:
            fila = self._db.execute("SELECT id, edge_event_id, payload, foto, intentos, creado, proximo FROM eventos ORDER BY id LIMIT 1").fetchone()
        if fila is None or fila[6] > ahora:
            return None
        ruta = os.path.join(self.dir_fotos, fila[3]) if fila[3] else None
        return Item(fila[0], fila[1], json.loads(fila[2]), ruta, fila[4], fila[5])

    def segundos_hasta_el_proximo(self) -> float | None:
        """Cuánto falta para que el primero toque reintentarse (0 si ya toca, None si la cola está vacía)."""
        with self._lock:
            fila = self._db.execute("SELECT proximo FROM eventos ORDER BY id LIMIT 1").fetchone()
        return None if fila is None else max(0.0, fila[0] - self._reloj())

    def confirmar(self, item_id: int) -> None:
        """El servidor lo recibió: sale de la cola."""
        with self._lock:
            fila = self._db.execute("SELECT foto FROM eventos WHERE id = ?", (item_id,)).fetchone()
            self._db.execute("DELETE FROM eventos WHERE id = ?", (item_id,))
        if fila and fila[0]:
            self._quitar_foto(fila[0])

    def reintentar_mas_tarde(self, item_id: int, espera_s: float) -> None:
        with self._lock:
            self._db.execute("UPDATE eventos SET intentos = intentos + 1, proximo = ? WHERE id = ?", (self._reloj() + espera_s, item_id))

    def rechazar(self, item_id: int, motivo: str) -> None:
        """El servidor lo rechazó para siempre: se aparta (sin la foto) para que no bloquee a los demás."""
        with self._lock:
            fila = self._db.execute("SELECT edge_event_id, creado, payload, foto FROM eventos WHERE id = ?", (item_id,)).fetchone()
            if fila is None:
                return
            self._db.execute("INSERT INTO rechazados (edge_event_id, creado, rechazado, motivo, payload) VALUES (?,?,?,?,?)",
                             (fila[0], fila[1], self._reloj(), motivo[:300], fila[2]))
            self._db.execute("DELETE FROM rechazados WHERE id NOT IN (SELECT id FROM rechazados ORDER BY id DESC LIMIT ?)", (MAX_RECHAZADOS,))
            self._db.execute("DELETE FROM eventos WHERE id = ?", (item_id,))
        if fila[3]:
            self._quitar_foto(fila[3])

    def estadisticas(self) -> dict:
        with self._lock:
            n, total, mas_viejo = self._db.execute("SELECT count(*), coalesce(sum(bytes), 0), min(creado) FROM eventos").fetchone()
            rechazados = self._db.execute("SELECT count(*) FROM rechazados").fetchone()[0]
            descartados = self._meta("descartados")
        return {"pendientes": n, "bytes": total, "mas_viejo_s": round(self._reloj() - mas_viejo, 1) if mas_viejo else 0.0,
                "descartados": descartados, "rechazados": rechazados}

    def rechazados(self, limite: int = 20) -> list[dict]:
        with self._lock:
            filas = self._db.execute("SELECT edge_event_id, creado, rechazado, motivo FROM rechazados ORDER BY id DESC LIMIT ?", (limite,)).fetchall()
        return [{"edge_event_id": f[0], "creado": f[1], "rechazado": f[2], "motivo": f[3]} for f in filas]

    def vaciar(self) -> int:
        """Borra todos los eventos pendientes. Devuelve cuántos eran."""
        with self._lock:
            fotos = [f[0] for f in self._db.execute("SELECT foto FROM eventos WHERE foto IS NOT NULL").fetchall()]
            n = self._db.execute("DELETE FROM eventos").rowcount
        for nombre in fotos:
            self._quitar_foto(nombre)
        return n

    def cerrar(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ por dentro
    def _meta(self, clave: str) -> int:
        fila = self._db.execute("SELECT valor FROM meta WHERE clave = ?", (clave,)).fetchone()
        return fila[0] if fila else 0

    def _sumar_meta(self, clave: str, n: int) -> None:
        self._db.execute("INSERT INTO meta (clave, valor) VALUES (?, ?) ON CONFLICT(clave) DO UPDATE SET valor = valor + ?", (clave, n, n))

    def _aplicar_topes(self) -> None:
        """Descarta lo más viejo si la cola pasó de su antigüedad o de su tamaño máximos."""
        descartar = [(f[0], f[1]) for f in self._db.execute(
            "SELECT id, foto FROM eventos WHERE creado < ?", (self._reloj() - self.max_edad_s,)).fetchall()]
        total = self._db.execute("SELECT coalesce(sum(bytes), 0) FROM eventos").fetchone()[0] - self._bytes_de(descartar)
        if total > self.max_bytes:
            for fila in self._db.execute("SELECT id, foto, bytes FROM eventos ORDER BY id").fetchall():
                if total <= self.max_bytes:
                    break
                if fila[0] in {d[0] for d in descartar}:
                    continue
                descartar.append((fila[0], fila[1]))
                total -= fila[2]
        if not descartar:
            return
        self._db.executemany("DELETE FROM eventos WHERE id = ?", [(d[0],) for d in descartar])
        self._sumar_meta("descartados", len(descartar))
        for _, nombre in descartar:
            if nombre:
                self._quitar_foto(nombre)
        log.warning("la cola pasó de su tope: se descartaron %d evento(s) viejos (en total: %d)", len(descartar), self._meta("descartados"))

    def _bytes_de(self, filas) -> int:
        ids = [f[0] for f in filas]
        if not ids:
            return 0
        marcas = ",".join("?" * len(ids))
        return self._db.execute(f"SELECT coalesce(sum(bytes), 0) FROM eventos WHERE id IN ({marcas})", ids).fetchone()[0]

    def _recuperar(self) -> None:
        """Al arrancar: borra fotos huérfanas (sin evento) y, si a un evento le falta su foto, lo deja sin foto."""
        with self._lock:
            conocidas = {f[0] for f in self._db.execute("SELECT foto FROM eventos WHERE foto IS NOT NULL").fetchall()}
            for nombre in os.listdir(self.dir_fotos):
                if nombre not in conocidas:
                    self._quitar_foto(nombre)
            for nombre in conocidas:
                if not os.path.exists(os.path.join(self.dir_fotos, nombre)):
                    self._db.execute("UPDATE eventos SET foto = NULL WHERE foto = ?", (nombre,))
                    log.warning("a un evento pendiente le falta su foto: se enviará sin ella")

    def _escribir_foto(self, nombre: str, datos: bytes) -> None:
        destino, temporal = os.path.join(self.dir_fotos, nombre), os.path.join(self.dir_fotos, f".{nombre}.tmp")
        try:
            with open(temporal, "wb") as fh:
                fh.write(datos)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporal, destino)
        except BaseException:
            self._quitar_foto(f".{nombre}.tmp")
            raise

    def _quitar_foto(self, nombre: str) -> None:
        try:
            os.remove(os.path.join(self.dir_fotos, nombre))
        except OSError:
            pass
