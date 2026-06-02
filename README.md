# OpenPositioning

**Freie, lokale WiFi-basierte Positionserkennung für Home Assistant**  
Kein Cloud-Zwang. Keine Lizenzprobleme. Vollständig open-source.

---

## Wie es funktioniert

```
[6× ESP32]  →  MQTT  →  [Python Backend]  →  MQTT  →  [Home Assistant]
   Raum-          WiFi-Scans        kNN + Ollama         3 Sensoren
   Nodes          alle 2s           lokal                Room / X / Y
```

1. **ESP32-Nodes** scannen alle 2 Sekunden alle sichtbaren WiFi-AccessPoints und melden MAC + RSSI per MQTT
2. **Python-Backend** aggregiert die Daten aller 6 Nodes zu einem "Fingerprint-Vektor"
3. **k-NN-Algorithmus** vergleicht den Live-Fingerprint mit der Trainingsdatenbank
4. **Ollama (lokal)** verfeinert die Schätzung bei niedriger Konfidenz
5. **Home Assistant** empfängt Raum + X/Y-Koordinaten als Sensoren

---

## Hardware

| Komponente | Anzahl | Kosten ca. |
|---|---|---|
| ESP32 (z.B. ESP32-WROOM-32) | 6 | ~3–5€/Stück |
| USB-Netzteil oder Powerbank | 6 | vorhanden |

### Node-Verteilung

```
Schlafzimmer          Wohnzimmer
┌─────────────┐       ┌─────────────┐
│ S1    S2    │       │ W1    W2    │
│             │       │             │
│      S3     │       │      W3     │
└─────────────┘       └─────────────┘
```
Platziere die Nodes möglichst verteilt (Ecken + Mitte).

---

## Installation

### 1. ESP32 Firmware flashen

**Benötigt:** Arduino IDE mit ESP32-Support

1. Bibliotheken installieren:
   - `PubSubClient` (by Nick O'Leary)
   - `ArduinoJson` (by Benoit Blanchon)

2. `firmware/wifi_scanner.ino` öffnen

3. Oben im Code anpassen:
   ```cpp
   #define NODE_ID    "S1"            // S1/S2/S3/W1/W2/W3
   #define NODE_ROOM  "Schlafzimmer"  // oder "Wohnzimmer"
   const char* WIFI_SSID = "DeinWLAN";
   const char* MQTT_HOST = "192.168.1.100";
   ```

4. Auf ESP32 flashen, Vorgang für alle 6 Nodes wiederholen

### 2. Python Backend

```bash
cd backend/
pip install paho-mqtt numpy scikit-learn requests python-dotenv
cp .env.example .env
# .env nach Bedarf anpassen
```

### 3. Ollama installieren

```bash
# Linux/Mac
curl -fsSL https://ollama.ai/install.sh | sh
ollama pull llama3.2   # ~2GB, läuft auf jedem normalen PC
```

### 4. Home Assistant Integration

```bash
cp -r ha-integration/ <HA-Config>/custom_components/openpos/
```

In `configuration.yaml`:
```yaml
sensor:
  - platform: openpos
    mqtt_topic: "openpos/position/result"
```

HA neustarten.

---

## Training (einmalig!)

Das System muss lernen, wie sich WiFi-Signale an verschiedenen Positionen anfühlen.

```bash
cd backend/
python positioning_backend.py train
```

**Ablauf:**
1. Stelle dich an Position X (z.B. Schlafzimmer links)
2. Gib Label ein: `Schlafzimmer_x0.5_y2.0`
3. Warte 5 Sekunden (Fingerprint wird aufgenommen)
4. Nächste Position → Repeat

**Empfehlung:** mind. 6–10 Positionen pro Raum, ~1m Abstand

### Label-Format
```
<Raumname>_x<X-Meter>_y<Y-Meter>

Beispiele:
  Schlafzimmer_x0.5_y0.5   → linke untere Ecke
  Schlafzimmer_x2.5_y2.0   → Mitte des Raumes
  Wohnzimmer_x0.5_y3.5     → linke obere Ecke
```

---

## Live-Betrieb starten

```bash
cd backend/
python positioning_backend.py
```

### Home Assistant Sensoren

Nach dem Start stehen zur Verfügung:
- `sensor.openpos_room` → `Schlafzimmer` oder `Wohnzimmer`
- `sensor.openpos_x` → X-Koordinate in Metern
- `sensor.openpos_y` → Y-Koordinate in Metern
- `sensor.openpos_confidence` → Konfidenz (0.0 – 1.0)

### Automation Beispiel

```yaml
automation:
  - alias: "Licht Schlafzimmer bei Betreten"
    trigger:
      platform: state
      entity_id: sensor.openpos_room
      to: "Schlafzimmer"
    action:
      service: light.turn_on
      entity_id: light.schlafzimmer
```

---

## Wie der Algorithmus funktioniert

### Phase 1: k-NN (schnell, offline)

Jeder Messpunkt erzeugt einen Vektor mit ~180 Features:
```
[rssi_mac1_node_S1, rssi_mac1_node_S2, ..., rssi_macN_node_W3]
```

Bei fehlender MAC: `-100 dBm` (Defaultwert)

Der k-NN-Algorithmus (k=3) sucht die ähnlichsten Trainingspunkte
und interpoliert gewichtet nach Distanz.

### Phase 2: Ollama (bei Unsicherheit)

Wenn k-NN-Konfidenz < 70%, fragt das Backend das lokale LLM.
Das Modell bekommt die Top-5 APs pro Node und die k-NN-Schätzung
als Kontext und gibt eine JSON-Antwort zurück.

Empfohlene Modelle:
- `llama3.2` (Standard, gute Balance)
- `phi3` (schnell, wenig RAM)
- `mistral` (präzise, etwas langsamer)

---

## Genauigkeit

| Bedingung | Erwartete Genauigkeit |
|---|---|
| Viele Trainingspunkte (>15 pro Raum) | ~0.5–1m |
| Wenige Trainingspunkte (6–9) | ~1–2m |
| Nur Raumzuordnung | ~95% korrekt |

**Verbesserungen:**
- Mehr ESP32-Nodes = bessere Triangulation
- Mehr Trainingsdaten = bessere X/Y-Genauigkeit
- Stabiles WLAN-Umfeld (keine sich bewegenden Accesspoints)

---

## MQTT Topics

| Topic | Richtung | Beschreibung |
|---|---|---|
| `openpos/nodes/scan` | ESP32 → Backend | WiFi-Scan-Daten |
| `openpos/nodes/status` | ESP32 → Backend | Heartbeat |
| `openpos/nodes/cmd` | Backend → ESP32 | Kommandos (training_start, etc.) |
| `openpos/position/result` | Backend → HA | Positionsergebnis |

---

## Lizenz

MIT License — vollständig frei nutzbar, veränderbar, weiterzugeben.

---

## Roadmap

- [ ] Web-UI zur Trainings-Visualisierung
- [ ] Automatische Raumkarten-Erstellung
- [ ] Mehrpersonen-Tracking (via BLE-Tags)
- [ ] Docker-Compose für einfaches Deployment
- [ ] HACS-Integration für HA