"""
Cliente mínimo de la API de Telegram. Nunca deja el token del bot en un mensaje de error ni en el registro:
las excepciones de `requests` incluyen la URL completa, y esa URL lleva el token.
"""
import json
import logging
import os
import re

import requests

API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
_TOKEN_SHAPE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")


def clean(text, token=None) -> str:
    """Quita el token (el real y cualquier cosa con su forma) de un texto."""
    text = str(text)
    if token:
        text = text.replace(token, "***")
    return _TOKEN_SHAPE.sub("***", text)


class _QuitarToken(logging.Filter):
    """La librería de red registra la línea de cada petición, y esa línea lleva el token en la ruta."""
    def filter(self, record):
        record.msg, record.args = clean(record.getMessage()), ()
        return True


logging.getLogger("urllib3").setLevel(logging.WARNING)             # ni siquiera en nivel DEBUG se imprimen las rutas
logging.getLogger("urllib3.connectionpool").addFilter(_QuitarToken())


class TelegramError(Exception):
    def __init__(self, code, description, retry_after=None):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description
        self.retry_after = retry_after

    @property
    def blocked(self) -> bool:
        """El usuario bloqueó el bot o borró el chat: no tiene sentido seguir intentando."""
        texto = self.description.lower()
        return self.code == 403 or (self.code == 400 and ("chat not found" in texto or "user is deactivated" in texto))


class Telegram:
    def __init__(self, token):
        self.token = token

    def _call(self, method, data=None, files=None, timeout=15):
        url = f"{API_BASE}/bot{self.token}/{method}"
        try:
            r = requests.post(url, data=data, files=files, timeout=timeout)
        except requests.RequestException as ex:
            raise TelegramError(0, clean(ex, self.token)) from None      # sin la cadena de excepciones: lleva la URL
        try:
            body = r.json()
        except ValueError:
            raise TelegramError(r.status_code, "respuesta no válida de Telegram") from None
        if not isinstance(body, dict) or not body.get("ok"):
            body = body if isinstance(body, dict) else {}
            raise TelegramError(body.get("error_code", r.status_code), clean(body.get("description", ""), self.token),
                                (body.get("parameters") or {}).get("retry_after"))
        return body.get("result")

    def get_me(self):
        return self._call("getMe", timeout=10)

    def delete_webhook(self):
        return self._call("deleteWebhook", timeout=10)

    def get_updates(self, offset=None, timeout=25):
        data = {"timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query"])}
        if offset is not None:
            data["offset"] = offset
        return self._call("getUpdates", data=data, timeout=timeout + 10) or []

    def send_message(self, chat_id, text, reply_markup=None):
        data = {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        return self._call("sendMessage", data=data, timeout=15)

    def answer_callback_query(self, callback_id, text=None, alert=False):
        """Responde al clic en un botón (quita el relojito del botón y, si se pide, muestra un aviso)."""
        data = {"callback_query_id": callback_id, "show_alert": "true" if alert else "false"}
        if text:
            data["text"] = text[:190]
        return self._call("answerCallbackQuery", data=data, timeout=10)

    def edit_message_caption(self, chat_id, message_id, caption, reply_markup=None):
        """Cambia el texto de un mensaje con foto. Sin reply_markup, se quitan los botones."""
        data = {"chat_id": chat_id, "message_id": message_id, "caption": caption,
                "reply_markup": json.dumps(reply_markup if reply_markup is not None else {"inline_keyboard": []})}
        return self._call("editMessageCaption", data=data, timeout=15)

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        """Cambia el texto de un mensaje sin foto. Sin reply_markup, se quitan los botones."""
        data = {"chat_id": chat_id, "message_id": message_id, "text": text, "disable_web_page_preview": "true",
                "reply_markup": json.dumps(reply_markup if reply_markup is not None else {"inline_keyboard": []})}
        return self._call("editMessageText", data=data, timeout=15)

    def send_photo(self, chat_id, path, caption, reply_markup=None):
        data = {"chat_id": chat_id, "caption": caption}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        with open(path, "rb") as photo:
            return self._call("sendPhoto", data=data, files={"photo": photo}, timeout=30)
