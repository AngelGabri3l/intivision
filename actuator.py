"""
actuator.py — Dispara la sirena/LED.

  LogActuator  : modo simulado (solo imprime en consola).
  MQTTActuator : publica la orden en un broker MQTT; el ESP32 (real o Wokwi) la recibe.

Cada orden enviada se registra en la consola (visible con `docker compose logs -f`)
junto con la confirmación del broker, para poder verificar que la alerta salió.
Órdenes: "ON:<seg>" (nivel ALTO, sirena), "WARN" (nivel MEDIO, LED), "OFF".
"""
import logging
import datetime

log = logging.getLogger("actuator")


def _describe(payload):
    if payload.startswith("ON"):
        return f"nivel ALTO -> sirena + LED por {payload.split(':')[-1]} s"
    if payload == "WARN":
        return "nivel MEDIO -> solo LED de advertencia"
    if payload == "OFF":
        return "apagar sirena y LED"
    return payload


class LogActuator:
    """Sirena simulada: no envía nada por la red."""
    def trigger(self, duration=10):
        log.warning("🚨 [SIMULADO] SIRENA ACTIVADA %ss (no se envió por MQTT: actuator.type='log')", duration)

    def warn(self):
        log.warning("💡 [SIMULADO] LED DE ADVERTENCIA (no se envió por MQTT: actuator.type='log')")

    def off(self):
        log.info("[SIMULADO] Sirena apagada")


class MQTTActuator:
    def __init__(self, host, port=1883, topic="guardian/cuadra01/siren",
                 username=None, password=None):
        import paho.mqtt.client as mqtt
        self.host, self.port, self.topic = host, port, topic
        self.sent = 0
        # paho-mqtt 2.x pide la versión de callbacks; 1.x no la conoce.
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except AttributeError:
            self.client = mqtt.Client()
        if username:
            self.client.username_pw_set(username, password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.connect(host, port, 60)
        self.client.loop_start()
        log.warning("📡 Actuador MQTT iniciado -> broker %s:%s | topic '%s'", host, port, topic)

    # Firmas compatibles con paho 1.x y 2.x (el código de resultado está en args[3]).
    def _on_connect(self, *args):
        rc = args[3] if len(args) > 3 else "?"
        log.warning("📡 Conectado al broker MQTT (resultado=%s)", rc)

    def _on_disconnect(self, *args):
        log.warning("📡 Desconectado del broker MQTT (%s)", args[-1] if args else "?")

    def _send(self, payload):
        info = self.client.publish(self.topic, payload, qos=1)
        try:
            info.wait_for_publish(timeout=5)
        except Exception:
            pass
        ok = info.is_published()
        self.sent += 1
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        log.warning("=" * 60)
        log.warning("📡 MQTT ENVIADO #%d  [%s]", self.sent, ts)
        log.warning("   broker : %s:%s", self.host, self.port)
        log.warning("   topic  : %s", self.topic)
        log.warning("   mensaje: %s  (%s)", payload, _describe(payload))
        log.warning("   estado : %s", "✅ confirmado por el broker" if ok
                    else "❌ SIN confirmación (revisa internet, host o puerto)")
        log.warning("=" * 60)
        return ok

    def trigger(self, duration=10):
        return self._send(f"ON:{duration}")

    def warn(self):
        return self._send("WARN")

    def off(self):
        return self._send("OFF")


def build_actuator(cfg):
    """Crea el actuador según config.yaml (sección 'actuator')."""
    a = (cfg or {}).get("actuator", {})
    if a.get("type") == "mqtt":
        try:
            return MQTTActuator(a["host"], a.get("port", 1883),
                                a.get("topic", "guardian/cuadra01/siren"),
                                a.get("username"), a.get("password"))
        except Exception as e:
            log.error("❌ MQTT no disponible (%s). Se usará la sirena SIMULADA.", e)
    else:
        log.warning("Actuador en modo SIMULADO (actuator.type='%s'): no se enviará nada por MQTT.",
                    a.get("type", "log"))
    return LogActuator()
