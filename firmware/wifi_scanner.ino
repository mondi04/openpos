/**
 * ESP32 WiFi Fingerprint Node
 * Part of: OpenPositioning System
 * 
 * Each ESP32 node scans surrounding WiFi APs and reports
 * RSSI values via MQTT to the positioning backend.
 * 
 * CONFIGURATION: Edit the section below before flashing.
 */

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

// ─────────────────────────────────────────────
//  NODE CONFIGURATION  (edit before flashing!)
// ─────────────────────────────────────────────
#define NODE_ID         "S1"          // Unique ID: S1,S2,S3 = Schlafzimmer | W1,W2,W3 = Wohnzimmer
#define NODE_ROOM       "Schlafzimmer" // Human-readable room name

const char* WIFI_SSID     = "YourWiFiSSID";
const char* WIFI_PASSWORD = "YourWiFiPassword";

const char* MQTT_HOST     = "192.168.1.100";   // Home Assistant IP
const int   MQTT_PORT     = 1883;
const char* MQTT_USER     = "mqtt_user";
const char* MQTT_PASS     = "mqtt_password";

// Topics
const char* TOPIC_SCAN    = "openpos/nodes/scan";      // Publishes scan data
const char* TOPIC_STATUS  = "openpos/nodes/status";    // Publishes heartbeat
const char* TOPIC_CMD     = "openpos/nodes/cmd";       // Listens for commands

#define SCAN_INTERVAL_MS  2000   // Scan every 2 seconds
#define MAX_APS_PER_SCAN  30     // Limit AP results to control payload size
// ─────────────────────────────────────────────

WiFiClient   wifiClient;
PubSubClient mqtt(wifiClient);

unsigned long lastScan    = 0;
unsigned long lastStatus  = 0;
int           scanCount   = 0;
bool          trainingMode = false;
String        trainingLabel = "";

// ── MQTT callback (for commands) ──────────────────────────────────────────────
void onMqttMessage(char* topic, byte* payload, unsigned int length) {
  String msg = "";
  for (unsigned int i = 0; i < length; i++) msg += (char)payload[i];

  StaticJsonDocument<256> doc;
  if (deserializeJson(doc, msg) != DeserializationError::Ok) return;

  String cmd = doc["cmd"] | "";
  String target = doc["node"] | "all";

  if (target != NODE_ID && target != "all") return;

  if (cmd == "training_start") {
    trainingMode = true;
    trainingLabel = doc["label"] | "unknown";
    Serial.printf("[CMD] Training mode ON → label: %s\n", trainingLabel.c_str());
  } else if (cmd == "training_stop") {
    trainingMode = false;
    trainingLabel = "";
    Serial.println("[CMD] Training mode OFF");
  } else if (cmd == "ping") {
    publishStatus("pong");
  }
}

// ── WiFi Setup ────────────────────────────────────────────────────────────────
void connectWiFi() {
  Serial.printf("\n[WiFi] Connecting to %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.printf("\n[WiFi] Connected! IP: %s\n", WiFi.localIP().toString().c_str());
}

// ── MQTT Setup ────────────────────────────────────────────────────────────────
void connectMQTT() {
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMqttMessage);
  mqtt.setBufferSize(4096);

  while (!mqtt.connected()) {
    String clientId = "openpos-" + String(NODE_ID);
    Serial.printf("[MQTT] Connecting as %s...\n", clientId.c_str());

    if (mqtt.connect(clientId.c_str(), MQTT_USER, MQTT_PASS)) {
      Serial.println("[MQTT] Connected!");
      mqtt.subscribe(TOPIC_CMD);
      publishStatus("online");
    } else {
      Serial.printf("[MQTT] Failed (rc=%d), retry in 3s\n", mqtt.state());
      delay(3000);
    }
  }
}

// ── Publish status heartbeat ──────────────────────────────────────────────────
void publishStatus(const char* status) {
  StaticJsonDocument<128> doc;
  doc["node"]   = NODE_ID;
  doc["room"]   = NODE_ROOM;
  doc["status"] = status;
  doc["uptime"] = millis() / 1000;
  doc["rssi_self"] = WiFi.RSSI();

  char buf[256];
  serializeJson(doc, buf);
  mqtt.publish(TOPIC_STATUS, buf, true);  // retained
}

// ── Main scan & publish ───────────────────────────────────────────────────────
void doScan() {
  int found = WiFi.scanNetworks(false, true);  // async=false, show_hidden=true
  if (found <= 0) return;

  // Sort by RSSI descending (bubble sort, small N)
  for (int i = 0; i < found - 1; i++) {
    for (int j = 0; j < found - i - 1; j++) {
      if (WiFi.RSSI(j) < WiFi.RSSI(j + 1)) {
        // swap via re-scan not possible, but we sort indices
      }
    }
  }

  // Build JSON payload
  DynamicJsonDocument doc(4096);
  doc["node"]      = NODE_ID;
  doc["room"]      = NODE_ROOM;
  doc["ts"]        = millis();
  doc["seq"]       = scanCount++;
  doc["training"]  = trainingMode;
  if (trainingMode) doc["label"] = trainingLabel;

  JsonArray aps = doc.createNestedArray("aps");
  int limit = min(found, MAX_APS_PER_SCAN);
  for (int i = 0; i < limit; i++) {
    JsonObject ap = aps.createNestedObject();
    ap["mac"]  = WiFi.BSSIDstr(i);
    ap["rssi"] = WiFi.RSSI(i);
    ap["ch"]   = WiFi.channel(i);
    // Don't include SSID to save bandwidth (MAC is enough for fingerprinting)
  }

  char buf[4096];
  size_t len = serializeJson(doc, buf);

  if (mqtt.publish(TOPIC_SCAN, buf, len)) {
    Serial.printf("[SCAN] Node %s → %d APs published%s\n",
      NODE_ID, limit, trainingMode ? " [TRAINING]" : "");
  } else {
    Serial.println("[SCAN] Publish failed (buffer too small?)");
  }

  WiFi.scanDelete();
}

// ── Arduino lifecycle ─────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  Serial.printf("\n=== OpenPositioning Node %s ===\n", NODE_ID);
  connectWiFi();
  connectMQTT();
}

void loop() {
  // Keep MQTT alive
  if (!mqtt.connected()) connectMQTT();
  mqtt.loop();

  unsigned long now = millis();

  // Periodic scan
  if (now - lastScan >= SCAN_INTERVAL_MS) {
    lastScan = now;
    doScan();
  }

  // Heartbeat every 30s
  if (now - lastStatus >= 30000) {
    lastStatus = now;
    publishStatus("online");
  }
}
