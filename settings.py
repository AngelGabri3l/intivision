import os
import logging

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv es opcional: sin él solo se leen las variables ya definidas
    load_dotenv = None

log = logging.getLogger("settings")
ROOT = os.path.dirname(os.path.abspath(__file__))

# variable de entorno -> (sección, clave) dentro de config.yaml
ENV_VARS = {
    "TELEGRAM_TOKEN": ("notifier", "telegram_token"),
    "TELEGRAM_CHAT_ID": ("notifier", "telegram_chat_id"),
    "MQTT_USERNAME": ("actuator", "username"),
    "MQTT_PASSWORD": ("actuator", "password"),
}


def load_config(path="config.yaml"):
    """Lee config.yaml y le superpone las credenciales que haya en el entorno / .env."""
    if load_dotenv:
        load_dotenv(os.path.join(ROOT, ".env"))  # no pisa variables que ya estén definidas

    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.warning("No se encontro %s; uso valores por defecto.", path)
        cfg = {}

    for var, (section, key) in ENV_VARS.items():
        value = os.environ.get(var, "").strip()
        if value:  # una variable vacía cuenta como no definida
            if not isinstance(cfg.get(section), dict):
                cfg[section] = {}
            cfg[section][key] = value

    n = cfg.get("notifier") or {}
    if n.get("telegram_token") and n.get("telegram_chat_id"):
        log.info("Telegram configurado (credenciales cargadas desde el entorno o .env).")
    else:
        log.warning("Telegram sin configurar: define TELEGRAM_TOKEN y TELEGRAM_CHAT_ID en el archivo .env.")
    return cfg
