"""
OpenPositioning KI-Steuerung
=============================
Empfängt Positionsdaten per MQTT, kombiniert sie mit Kontext
(Tageszeit, Wetter, Gewohnheiten) und lässt Ollama entscheiden
was Home Assistant tun soll.

Steuerbare Geräte: Licht, Heizung, Musik
"""

import json
import time
import logging
import os
import requests
from datetime import datetime
from collections import deque
from dotenv import load_dotenv
import paho.mqtt.client as mqtt

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("openpos-ki")

# ─────────────────────────────────────────────
#  KONFIGURATION
# ─────────────────────────────────────────────
MQTT_HOST       = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT       = int(os.getenv("MQTT_PORT", 1883))
MQTT_USER       = os.getenv("MQTT_USER", "")
MQTT_PASS       = os.getenv("MQTT_PASS", "")

OLLAMA_HOST     = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "llama3.2")

HA_URL          = os.getenv("HA_URL", "http://localhost:8123")
HA_TOKEN        = os.getenv("HA_TOKEN", "")          # Long-Lived Access Token aus HA

TOPIC_POSITION  = "openpos/position/result"
TOPIC_KI_ACTION = "openpos/ki/action"                # KI publiziert ihre Entscheidungen hier

# Wie oft darf die KI maximal eine Aktion auslösen (Sekunden)
# Verhindert dass bei jedem Positions-Update alles neu geschaltet wird
KI_COOLDOWN_S   = 30

# ─────────────────────────────────────────────
#  HOME ASSISTANT GERÄTE
#  Hier deine echten HA Entity-IDs eintragen!
# ─────────────────────────────────────────────
DEVICES = {
    "licht": {
        "schlafzimmer": "light.schlafzimmer",       # ← anpassen
        "wohnzimmer":   "light.wohnzimmer",          # ← anpassen
    },
    "heizung": {
        "schlafzimmer": "climate.schlafzimmer",      # ← anpassen
        "wohnzimmer":   "climate.wohnzimmer",        # ← anpassen
    },
    "musik": {
        "schlafzimmer": "media_player.schlafzimmer", # ← anpassen
        "wohnzimmer":   "media_player.wohnzimmer",   # ← anpassen
    }
}

# Gewohnheiten / Präferenzen (wird dem KI-Prompt mitgegeben)
USER_PREFERENCES = """
- Moritz schläft meistens zwischen 23 Uhr und 7 Uhr
- Im Schlafzimmer soll das Licht abends (ab 20 Uhr) gedimmt sein (max 30%)
- Die Heizung im Schlafzimmer soll nachts auf 18°C, tagsüber auf 21°C
- Im Wohnzimmer tagsüber 22°C, abends 20°C
- Musik im Schlafzimmer nur wenn es nach 8 Uhr und vor 22 Uhr ist
- Beim Betreten des Wohnzimmers tagsüber: Licht nur bei Dunkelheit einschalten
- Beim Verlassen eines Raumes: Licht nach 2 Minuten ausschalten
"""
# ─────────────────────────────────────────────


class HAClient:
    """Home Assistant REST API Client."""

    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

    def call_service(self, domain: str, service: str, data: dict) -> bool:
        """Ruft einen HA-Service auf."""
        try:
            resp = requests.post(
                f"{self.url}/api/services/{domain}/{service}",
                headers=self.headers,
                json=data,
                timeout=5
            )
            resp.raise_for_status()
            log.info(f"[HA] {domain}.{service} → {data}")
            return True
        except Exception as e:
            log.error(f"[HA] Service-Aufruf fehlgeschlagen: {e}")
            return False

    def get_state(self, entity_id: str) -> dict | None:
        """Liest den aktuellen Zustand einer Entität."""
        try:
            resp = requests.get(
                f"{self.url}/api/states/{entity_id}",
                headers=self.headers,
                timeout=5
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error(f"[HA] State-Abfrage fehlgeschlagen: {e}")
            return None

    def execute_actions(self, actions: list[dict]):
        """Führt eine Liste von KI-Aktionen aus."""
        for action in actions:
            device_type = action.get("device")
            room        = action.get("room", "").lower()
            command     = action.get("command")
            value       = action.get("value")

            if device_type == "licht":
                entity = DEVICES["licht"].get(room)
                if not entity: continue
                if command == "an":
                    brightness = int((value or 100) / 100 * 255)
                    self.call_service("light", "turn_on", {
                        "entity_id": entity,
                        "brightness": brightness
                    })
                elif command == "aus":
                    self.call_service("light", "turn_off", {"entity_id": entity})

            elif device_type == "heizung":
                entity = DEVICES["heizung"].get(room)
                if not entity: continue
                if command == "temperatur":
                    self.call_service("climate", "set_temperature", {
                        "entity_id": entity,
                        "temperature": float(value or 20)
                    })

            elif device_type == "musik":
                entity = DEVICES["musik"].get(room)
                if not entity: continue
                if command == "an":
                    self.call_service("media_player", "media_play", {"entity_id": entity})
                elif command == "aus":
                    self.call_service("media_player", "media_pause", {"entity_id": entity})
                elif command == "lautstaerke":
                    self.call_service("media_player", "volume_set", {
                        "entity_id": entity,
                        "volume_level": float(value or 0.3)
                    })


class OllamaDecider:
    """Fragt Ollama was HA tun soll."""

    def __init__(self, host: str, model: str):
        self.host  = host
        self.model = model

    def decide(self, position: dict, history: list[dict]) -> list[dict]:
        """
        Gibt eine Liste von Aktionen zurück die HA ausführen soll.
        """
        now = datetime.now()
        history_str = "\n".join([
            f"  {h['ts']}: {h['room']} (x={h['x']}, y={h['y']})"
            for h in list(history)[-5:]  # letzte 5 Positionen
        ])

        prompt = f"""Du bist ein intelligentes Heimautomatisierungssystem für Moritz.
Deine Aufgabe: Entscheide welche Geräte jetzt geschaltet werden sollen.

AKTUELLE SITUATION:
- Uhrzeit: {now.strftime('%H:%M')} Uhr, {now.strftime('%A')} (Wochentag: {'Wochenende' if now.weekday() >= 5 else 'Werktag'})
- Aktueller Raum: {position['room']}
- Position: x={position['x']}m, y={position['y']}m
- Konfidenz: {position['confidence']}

BEWEGUNGSHISTORIE (letzte Positionen):
{history_str}

PRÄFERENZEN:
{USER_PREFERENCES}

VERFÜGBARE GERÄTE:
- licht: schlafzimmer, wohnzimmer
- heizung: schlafzimmer, wohnzimmer  
- musik: schlafzimmer, wohnzimmer

REGELN:
- Nur sinnvolle Aktionen auslösen (nicht alles auf einmal schalten)
- Bei Raumwechsel: alten Raum berücksichtigen
- Keine Aktionen wenn Konfidenz < 0.6
- Musik nur in dem Raum wo Moritz ist

Antworte NUR mit einem JSON-Array von Aktionen, kein Text davor/danach:
[
  {{"device": "licht", "room": "schlafzimmer", "command": "an", "value": 30, "reason": "kurze Begründung"}},
  {{"device": "heizung", "room": "schlafzimmer", "command": "temperatur", "value": 21, "reason": "..."}}
]

Leeres Array [] wenn keine Aktion nötig ist."""

        try:
            resp = requests.post(
                f"{self.host}/api/generate",
                json={"model": self.model, "prompt": prompt, "stream": False},
                timeout=20
            )
            resp.raise_for_status()
            raw = resp.json().get("response", "")

            # JSON aus Antwort extrahieren
            start = raw.find("[")
            end   = raw.rfind("]") + 1
            if start == -1 or end == 0:
                return []

            actions = json.loads(raw[start:end])
            for a in actions:
                log.info(f"[KI] → {a.get('device')} {a.get('room')} {a.get('command')} "
                         f"{a.get('value','')} | {a.get('reason','')}")
            return actions

        except Exception as e:
            log.warning(f"[Ollama] Fehler: {e}")
            return []


class KIController:
    """Hauptklasse: verbindet MQTT, Ollama und HA."""

    def __init__(self):
        self.ha       = HAClient(HA_URL, HA_TOKEN)
        self.ollama   = OllamaDecider(OLLAMA_HOST, OLLAMA_MODEL)
        self.client   = mqtt.Client(client_id="openpos-ki", protocol=mqtt.MQTTv5)
        self.history  = deque(maxlen=20)
        self.last_action_time = 0
        self.last_room = ""

    def start(self):
        self.client.username_pw_set(MQTT_USER, MQTT_PASS)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

        log.info(f"[MQTT] Verbinde mit {MQTT_HOST}:{MQTT_PORT}...")
        self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        self.client.loop_start()

        log.info("KI-Steuerung läuft. Warte auf Positionsdaten...")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Beende KI-Steuerung...")
            self.client.loop_stop()

    def _on_connect(self, client, userdata, flags, rc, props=None):
        if rc == 0:
            log.info("[MQTT] Verbunden!")
            client.subscribe(TOPIC_POSITION)
        else:
            log.error(f"[MQTT] Verbindung fehlgeschlagen: rc={rc}")

    def _on_message(self, client, userdata, msg):
        try:
            position = json.loads(msg.payload.decode())
            self._handle_position(position)
        except Exception as e:
            log.error(f"[MQTT] Parse-Fehler: {e}")

    def _handle_position(self, position: dict):
        # Zur Historie hinzufügen
        position["ts"] = datetime.now().strftime("%H:%M:%S")
        self.history.append(position)

        now = time.time()
        room_changed = position["room"] != self.last_room
        cooldown_ok  = (now - self.last_action_time) >= KI_COOLDOWN_S

        # KI nur aufrufen bei Raumwechsel ODER nach Cooldown
        if not room_changed and not cooldown_ok:
            return

        if room_changed:
            log.info(f"[POS] Raumwechsel: {self.last_room} → {position['room']}")
            self.last_room = position["room"]

        # Ollama entscheiden lassen
        actions = self.ollama.decide(position, list(self.history))

        if actions:
            self.last_action_time = now
            self.ha.execute_actions(actions)

            # Aktionen auch per MQTT publizieren (für HA-Logging)
            self.client.publish(TOPIC_KI_ACTION, json.dumps({
                "position": position,
                "actions":  actions,
                "ts":       position["ts"]
            }))


if __name__ == "__main__":
    if not HA_TOKEN:
        log.error("HA_TOKEN fehlt! Bitte in .env eintragen.")
        log.error("HA → Profil → Sicherheit → Langlebige Zugriffstoken → Token erstellen")
        exit(1)
    KIController().start()