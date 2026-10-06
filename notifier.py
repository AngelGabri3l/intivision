"""
notifier.py — Guarda evidencia (foto + registro) y avisa al celular (Telegram).
Si se pasa clip_path (.mp4), envía video; si no, foto. Siempre guarda la foto.
"""
import os
import json
import logging
import datetime

import cv2
import requests

log = logging.getLogger("notifier")


class Notifier:
    def __init__(self, events_dir="events", telegram_token=None, telegram_chat_id=None):
        self.events_dir = events_dir
        os.makedirs(events_dir, exist_ok=True)
        self.token = telegram_token
        self.chat_id = telegram_chat_id
        self.log_path = os.path.join(events_dir, "events.jsonl")

    def notify(self, frame, meta, clip_path=None):
        ts = datetime.datetime.now()
        stamp = ts.strftime("%Y%m%d_%H%M%S")
        img_path = os.path.join(self.events_dir, f"intruso_{stamp}.jpg")
        cv2.imwrite(img_path, frame)

        record = {"timestamp": ts.isoformat(), "image": img_path, "clip": clip_path, **meta}
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.warning(" Evidencia guardada: %s", img_path)

        if self.token and self.chat_id:
            if clip_path:
                self._send_video(clip_path, meta)
            else:
                self._send_photo(img_path, meta)
        return record

    def send_photo(self, img_path, meta):
        """Envía por Telegram una foto que ya está guardada (la usa el backend)."""
        if not (self.token and self.chat_id):
            log.warning("Telegram NO configurado (falta telegram_token o telegram_chat_id): no se envía.")
            return
        self._send_photo(img_path, meta)

    def _caption(self, meta):
        return (" ALERTA DE SEGURIDAD\n"
                f"Zona: {meta.get('zone', 'la cuadra')}\n"
                f"Nivel: {meta.get('level', '')}  (puntaje {meta.get('score', '')})\n"
                f"Motivo: {', '.join(meta.get('reasons', [])) or 'actividad detectada'}\n"
                f"Hora: {meta.get('time', '')}")

    def _send_photo(self, img_path, meta):
        try:
            with open(img_path, "rb") as photo:
                r = requests.post(f"https://api.telegram.org/bot{self.token}/sendPhoto",
                                  data={"chat_id": self.chat_id, "caption": self._caption(meta)},
                                  files={"photo": photo}, timeout=10)
            if r.ok:
                log.warning("📲 Alerta (foto) enviada por Telegram")
            else:
                log.error("❌ Telegram rechazó la foto: %s", r.text[:200])
        except Exception as e:
            log.error("Fallo al enviar foto: %s", e)

    def _send_video(self, clip_path, meta):
        try:
            with open(clip_path, "rb") as video:
                r = requests.post(f"https://api.telegram.org/bot{self.token}/sendVideo",
                                  data={"chat_id": self.chat_id, "caption": self._caption(meta)},
                                  files={"video": video}, timeout=30)
            if r.ok:
                log.warning(" Alerta (video) enviada por Telegram")
            else:
                log.error(" Telegram rechazó el video: %s", r.text[:200])
        except Exception as e:
            log.error("Fallo al enviar video: %s", e)
