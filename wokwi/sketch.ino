/*
 * sketch.ino — ESP32 SIMULADO EN WOKWI para GUARDIAN.
 *
 * Se conecta al WiFi de Wokwi y a un broker MQTT publico, y reacciona a:
 *    "ON:<seg>" -> nivel ALTO : sirena (buzzer) + LED durante N segundos
 *    "WARN"     -> nivel MEDIO: solo LED (advertencia)
 *    "OFF"      -> apaga todo
 *
 * Cada alerta recibida se imprime en el monitor serial con un banner, para
 * comprobar que llegan bien. Cada 30 s imprime un latido "esperando alertas".
 *
 * IMPORTANTE: crea el proyecto en Wokwi como "ESP32 (Arduino)" para que el
 * archivo sea sketch.ino (C++). Si Wokwi crea main.c, el compilador falla.
 * Librerias (Library Manager): PubSubClient.
 * El TOPIC debe ser EXACTAMENTE igual al de config.yaml (actuator.topic).
 */
#include <WiFi.h>
#include <PubSubClient.h>

const char* WIFI_SSID  = "Wokwi-GUEST";
const char* WIFI_PASS  = "";
const char* MQTT_HOST  = "broker.hivemq.com";
const int   MQTT_PORT  = 1883;
const char* MQTT_TOPIC = "guardian/demo7k3x9/siren";  // <-- igual que en config.yaml

const int PIN_BUZZER = 25;
const int PIN_LED    = 26;

WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);

unsigned long offAt = 0;
unsigned long alertCount = 0;
unsigned long lastBeat = 0;

void allOff() {
  noTone(PIN_BUZZER);
  digitalWrite(PIN_LED, LOW);
}

void onMessage(char* topic, byte* payload, unsigned int length) {
  String msg;
  for (unsigned int i = 0; i < length; i++) msg += (char)payload[i];
  alertCount++;

  Serial.println();
  Serial.println("==========================================");
  Serial.printf ("  ALERTA RECIBIDA #%lu   (t = %lu s)\n", alertCount, millis() / 1000);
  Serial.printf ("  topic  : %s\n", topic);
  Serial.printf ("  mensaje: %s\n", msg.c_str());

  if (msg.startsWith("ON")) {
    int seconds = 10;
    int sep = msg.indexOf(':');
    if (sep > 0) seconds = msg.substring(sep + 1).toInt();
    digitalWrite(PIN_LED, HIGH);
    tone(PIN_BUZZER, 1200);
    offAt = millis() + (unsigned long)seconds * 1000UL;
    Serial.printf ("  accion : NIVEL ALTO -> SIRENA + LED por %d s\n", seconds);
  } else if (msg == "WARN") {
    noTone(PIN_BUZZER);
    digitalWrite(PIN_LED, HIGH);
    offAt = millis() + 8000UL;
    Serial.println("  accion : NIVEL MEDIO -> solo LED por 8 s");
  } else if (msg == "OFF") {
    allOff();
    offAt = 0;
    Serial.println("  accion : APAGAR todo");
  } else {
    Serial.println("  accion : mensaje desconocido, se ignora");
  }
  Serial.println("==========================================");
}

void connectWifi() {
  Serial.print("Conectando WiFi");
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) { delay(300); Serial.print("."); }
  Serial.println(" OK");
}

void connectMqtt() {
  while (!mqtt.connected()) {
    String id = "esp32-guardian-" + String((uint32_t)ESP.getEfuseMac(), HEX);
    Serial.printf("Conectando a MQTT %s:%d ...", MQTT_HOST, MQTT_PORT);
    if (mqtt.connect(id.c_str())) {
      mqtt.subscribe(MQTT_TOPIC);
      Serial.println(" OK");
      Serial.printf("Suscrito a: %s\n", MQTT_TOPIC);
      Serial.println("Conectado a MQTT. Esperando alertas.");
    } else {
      Serial.printf(" fallo (rc=%d), reintento en 2 s\n", mqtt.state());
      delay(2000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_BUZZER, OUTPUT);
  pinMode(PIN_LED, OUTPUT);
  allOff();
  connectWifi();
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
  connectMqtt();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) connectWifi();
  if (!mqtt.connected()) connectMqtt();
  mqtt.loop();

  if (offAt != 0 && millis() >= offAt) {
    allOff();
    offAt = 0;
    Serial.println(">> Sirena/LED apagados automaticamente");
  }

  if (millis() - lastBeat >= 30000UL) {
    lastBeat = millis();
    Serial.printf("[t = %lu s] Conectado, esperando alertas... (recibidas: %lu)\n",
                  millis() / 1000, alertCount);
  }
}
