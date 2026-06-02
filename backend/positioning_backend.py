"""
OpenPositioning Backend
=======================
Collects WiFi scan data from ESP32 nodes via MQTT,
maintains a fingerprint database, and uses Ollama for position estimation.

Requirements:
    pip install paho-mqtt numpy scikit-learn requests python-dotenv

Usage:
    python positioning_backend.py
"""

import json
import time
import sqlite3
import threading
import logging
import os
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

import numpy as np
import paho.mqtt.client as mqtt
import requests
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("openpos")

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────
MQTT_HOST       = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT       = int(os.getenv("MQTT_PORT", 1883))
MQTT_USER       = os.getenv("MQTT_USER", "")
MQTT_PASS       = os.getenv("MQTT_PASS", "")

OLLAMA_HOST     = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "llama3.2")  # or mistral, phi3, etc.

DB_PATH         = os.getenv("DB_PATH", "openpos.db")
HA_WEBHOOK_URL  = os.getenv("HA_WEBHOOK_URL", "")  # optional HA webhook

NODES           = ["S1", "S2", "S3", "W1", "W2", "W3"]
ROOMS           = ["Schlafzimmer", "Wohnzimmer"]
AGGREGATION_WINDOW_S = 3    # Collect scans from all nodes for N seconds before estimating
RSSI_MISSING_VALUE   = -100  # Value used when an AP wasn't seen by a node

TOPIC_SCAN   = "openpos/nodes/scan"
TOPIC_STATUS = "openpos/nodes/status"
TOPIC_CMD    = "openpos/nodes/cmd"
TOPIC_RESULT = "openpos/position/result"
# ─────────────────────────────────────────────


@dataclass
class ScanEntry:
    node: str
    room: str
    ts: int
    aps: dict  # mac -> rssi
    training: bool = False
    label: str = ""


@dataclass
class PositionResult:
    room: str
    x: float
    y: float
    confidence: float
    method: str
    raw_scores: dict = field(default_factory=dict)


class FingerprintDB:
    """SQLite-backed fingerprint database."""

    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self._init_schema()

    def _init_schema(self):
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS fingerprints (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    label     TEXT NOT NULL,
                    room      TEXT NOT NULL,
                    x         REAL DEFAULT 0,
                    y         REAL DEFAULT 0,
                    node      TEXT NOT NULL,
                    mac       TEXT NOT NULL,
                    rssi      INTEGER NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_label ON fingerprints(label);
                CREATE INDEX IF NOT EXISTS idx_mac   ON fingerprints(mac);

                CREATE TABLE IF NOT EXISTS positions (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    room      TEXT,
                    x         REAL,
                    y         REAL,
                    confidence REAL,
                    method    TEXT,
                    estimated_at TEXT
                );
            """)

    def save_fingerprint(self, label: str, room: str, x: float, y: float, scans: list[ScanEntry]):
        """Store a training fingerprint (multi-node snapshot)."""
        now = datetime.utcnow().isoformat()
        rows = []
        for scan in scans:
            for mac, rssi in scan.aps.items():
                rows.append((label, room, x, y, scan.node, mac, rssi, now))
        with self.lock:
            with self.conn:
                self.conn.executemany(
                    "INSERT INTO fingerprints(label,room,x,y,node,mac,rssi,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    rows
                )
        log.info(f"[DB] Saved fingerprint '{label}' ({len(rows)} AP readings)")

    def get_training_data(self) -> tuple[np.ndarray, list[str], list[tuple], list[str]]:
        """
        Returns (X, rooms, coords, macs) where X is shape (n_samples, n_features).
        Features = RSSI per (mac, node) pair.
        """
        with self.lock:
            cur = self.conn.execute(
                "SELECT DISTINCT mac FROM fingerprints ORDER BY mac"
            )
            macs = [r[0] for r in cur.fetchall()]

            cur = self.conn.execute(
                "SELECT DISTINCT label, room, x, y FROM fingerprints ORDER BY label"
            )
            samples_meta = cur.fetchall()  # [(label, room, x, y), ...]

        if not macs or not samples_meta:
            return np.array([]), [], [], []

        feature_keys = [(mac, node) for mac in macs for node in NODES]
        n_features = len(feature_keys)
        key_index = {k: i for i, k in enumerate(feature_keys)}

        X = np.full((len(samples_meta), n_features), RSSI_MISSING_VALUE, dtype=float)
        rooms = []
        coords = []

        for s_idx, (label, room, x, y) in enumerate(samples_meta):
            rooms.append(room)
            coords.append((x, y))
            with self.lock:
                cur = self.conn.execute(
                    "SELECT node, mac, rssi FROM fingerprints WHERE label=?", (label,)
                )
                for node, mac, rssi in cur.fetchall():
                    key = (mac, node)
                    if key in key_index:
                        X[s_idx, key_index[key]] = rssi

        return X, rooms, coords, feature_keys

    def save_position(self, result: PositionResult):
        with self.lock:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO positions(room,x,y,confidence,method,estimated_at) VALUES(?,?,?,?,?,?)",
                    (result.room, result.x, result.y, result.confidence,
                     result.method, datetime.utcnow().isoformat())
                )


class KNNPositioner:
    """Weighted k-NN over fingerprint database."""

    def __init__(self, db: FingerprintDB, k: int = 3):
        self.db = db
        self.k = k
        self._X = None
        self._rooms = None
        self._coords = None
        self._feature_keys = None
        self._trained = False

    def train(self):
        X, rooms, coords, keys = self.db.get_training_data()
        if len(X) == 0:
            log.warning("[KNN] No training data available yet.")
            return False
        self._X = X
        self._rooms = rooms
        self._coords = coords
        self._feature_keys = keys
        self._trained = True
        log.info(f"[KNN] Trained on {len(X)} samples, {len(keys)} features")
        return True

    def predict(self, live_scans: list[ScanEntry]) -> Optional[PositionResult]:
        if not self._trained:
            if not self.train():
                return None

        # Build feature vector from live scans
        vec = np.full(len(self._feature_keys), RSSI_MISSING_VALUE, dtype=float)
        key_index = {k: i for i, k in enumerate(self._feature_keys)}

        for scan in live_scans:
            for mac, rssi in scan.aps.items():
                key = (mac, scan.node)
                if key in key_index:
                    vec[key_index[key]] = rssi

        # Euclidean distances to all training samples
        diffs = self._X - vec
        distances = np.sqrt(np.sum(diffs ** 2, axis=1))

        # k-NN weighted by inverse distance
        k = min(self.k, len(distances))
        nn_idx = np.argsort(distances)[:k]
        nn_dist = distances[nn_idx]

        # Avoid division by zero (exact match)
        weights = 1.0 / np.maximum(nn_dist, 0.01)
        weights /= weights.sum()

        # Weighted room voting
        room_scores = defaultdict(float)
        for i, idx in enumerate(nn_idx):
            room_scores[self._rooms[idx]] += weights[i]

        # Weighted X/Y interpolation
        x_est = sum(weights[i] * self._coords[nn_idx[i]][0] for i in range(k))
        y_est = sum(weights[i] * self._coords[nn_idx[i]][1] for i in range(k))

        best_room = max(room_scores, key=room_scores.get)
        confidence = float(room_scores[best_room])

        return PositionResult(
            room=best_room,
            x=round(x_est, 2),
            y=round(y_est, 2),
            confidence=round(confidence, 3),
            method="knn",
            raw_scores=dict(room_scores)
        )


class OllamaPositioner:
    """
    Uses local Ollama LLM to reason about position from RSSI data.
    Best used for ambiguous cases where k-NN confidence is low.
    """

    def __init__(self, host: str, model: str):
        self.host = host
        self.model = model

    def predict(self, live_scans: list[ScanEntry], knn_result: Optional[PositionResult]) -> Optional[PositionResult]:
        """
        Ask the LLM to verify/refine a position estimate.
        Returns None if Ollama is unavailable.
        """
        # Format scan data for the prompt
        scan_summary = []
        for scan in live_scans:
            top_aps = sorted(scan.aps.items(), key=lambda x: x[1], reverse=True)[:5]
            ap_str = ", ".join(f"{mac[-5:]}:{rssi}dBm" for mac, rssi in top_aps)
            scan_summary.append(f"  Node {scan.node}: {ap_str}")

        knn_hint = ""
        if knn_result:
            knn_hint = f"\nk-NN preliminary estimate: room={knn_result.room}, x={knn_result.x}, y={knn_result.y}, confidence={knn_result.confidence}"

        prompt = f"""You are an indoor positioning system. Analyze these WiFi RSSI readings from 6 ESP32 nodes
(S1-S3 in Schlafzimmer bedroom, W1-W3 in Wohnzimmer living room) and determine position.

Live RSSI readings (top 5 APs per node, format MAC_suffix:rssi):
{chr(10).join(scan_summary)}
{knn_hint}

Rules:
- Higher RSSI (closer to 0) means stronger signal = device is physically closer to that node
- Nodes S1-S3 are in Schlafzimmer (bedroom), nodes W1-W3 are in Wohnzimmer (living room)
- X axis: 0=left wall, max=right wall (estimate room width ~5m)
- Y axis: 0=bottom wall, max=top wall (estimate room height ~4m)

Respond ONLY with valid JSON, no explanation:
{{"room": "Schlafzimmer or Wohnzimmer", "x": 0.0, "y": 0.0, "confidence": 0.0-1.0, "reasoning": "one sentence"}}"""

        try:
            resp = requests.post(
                f"{self.host}/api/generate",
                json={"model": self.model, "prompt": prompt, "stream": False},
                timeout=10
            )
            resp.raise_for_status()
            raw = resp.json().get("response", "")

            # Extract JSON from response
            start = raw.find("{")
            end   = raw.rfind("}") + 1
            if start == -1 or end == 0:
                raise ValueError("No JSON in response")

            data = json.loads(raw[start:end])
            log.info(f"[Ollama] {data.get('room')} ({data.get('confidence')}) — {data.get('reasoning','')}")

            return PositionResult(
                room=data["room"],
                x=float(data["x"]),
                y=float(data["y"]),
                confidence=float(data["confidence"]),
                method="ollama",
                raw_scores={"reasoning": data.get("reasoning", "")}
            )
        except Exception as e:
            log.warning(f"[Ollama] Unavailable or parse error: {e}")
            return None


class PositioningBackend:
    """Main orchestrator: MQTT → aggregation → estimation → publish."""

    def __init__(self):
        self.db         = FingerprintDB(DB_PATH)
        self.knn        = KNNPositioner(self.db)
        self.ollama     = OllamaPositioner(OLLAMA_HOST, OLLAMA_MODEL)
        self.client     = mqtt.Client(client_id="openpos-backend", protocol=mqtt.MQTTv5)
        # Window of recent scans per node
        self.scan_window: dict[str, deque] = {n: deque(maxlen=5) for n in NODES}
        self.lock        = threading.Lock()
        self.last_estimate = 0
        self.current_position: Optional[PositionResult] = None

    def start(self):
        self.client.username_pw_set(MQTT_USER, MQTT_PASS)
        self.client.on_connect    = self._on_connect
        self.client.on_message    = self._on_message
        self.client.on_disconnect = self._on_disconnect

        log.info(f"[MQTT] Connecting to {MQTT_HOST}:{MQTT_PORT}...")
        self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        self.client.loop_start()

        # Retrain k-NN periodically
        threading.Thread(target=self._retrain_loop, daemon=True).start()

        log.info("OpenPositioning Backend running. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Shutting down...")
            self.client.loop_stop()
            self.client.disconnect()

    def _on_connect(self, client, userdata, flags, rc, props=None):
        if rc == 0:
            log.info("[MQTT] Connected!")
            client.subscribe(TOPIC_SCAN)
            client.subscribe(TOPIC_STATUS)
        else:
            log.error(f"[MQTT] Connection failed: rc={rc}")

    def _on_disconnect(self, client, userdata, rc, props=None):
        log.warning(f"[MQTT] Disconnected (rc={rc}), reconnecting...")

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode())
            if msg.topic == TOPIC_SCAN:
                self._handle_scan(data)
        except Exception as e:
            log.error(f"[MQTT] Message parse error: {e}")

    def _handle_scan(self, data: dict):
        node = data.get("node", "")
        if node not in NODES:
            return

        aps = {ap["mac"]: ap["rssi"] for ap in data.get("aps", [])}
        entry = ScanEntry(
            node=node,
            room=data.get("room", ""),
            ts=data.get("ts", 0),
            aps=aps,
            training=data.get("training", False),
            label=data.get("label", "")
        )

        with self.lock:
            self.scan_window[node].append(entry)

        # Training mode: accumulate and save fingerprint
        if entry.training and entry.label:
            self._maybe_save_training(entry.label)
            return

        # Live mode: estimate position every AGGREGATION_WINDOW_S
        now = time.time()
        if now - self.last_estimate >= AGGREGATION_WINDOW_S:
            self.last_estimate = now
            threading.Thread(target=self._estimate_and_publish, daemon=True).start()

    def _maybe_save_training(self, label: str):
        """If we have recent scans from all nodes, save as fingerprint."""
        with self.lock:
            recent = []
            for node, window in self.scan_window.items():
                if window and window[-1].training and window[-1].label == label:
                    recent.append(window[-1])

        if len(recent) >= len(NODES) * 0.5:  # at least half the nodes
            # Parse position from label: "Schlafzimmer_x2.5_y1.0" or just "Schlafzimmer"
            room = label.split("_")[0] if "_" in label else label
            x, y = 0.0, 0.0
            for part in label.split("_"):
                if part.startswith("x"): x = float(part[1:])
                if part.startswith("y"): y = float(part[1:])
            self.db.save_fingerprint(label, room, x, y, recent)
            self.knn.train()  # Retrain immediately

    def _estimate_and_publish(self):
        """Gather latest scans, estimate position, publish to MQTT and HA."""
        with self.lock:
            live_scans = [w[-1] for w in self.scan_window.values() if w]

        if len(live_scans) < 2:
            return  # Not enough data

        # Step 1: k-NN estimate (fast, local)
        knn_result = self.knn.predict(live_scans)

        # Step 2: Use Ollama if k-NN confidence is low OR to refine
        final_result = knn_result
        if knn_result is None or knn_result.confidence < 0.7:
            ollama_result = self.ollama.predict(live_scans, knn_result)
            if ollama_result:
                final_result = ollama_result

        if final_result is None:
            return

        self.current_position = final_result
        self.db.save_position(final_result)

        # Publish to MQTT
        payload = {
            "room":       final_result.room,
            "x":          final_result.x,
            "y":          final_result.y,
            "confidence": final_result.confidence,
            "method":     final_result.method,
            "ts":         datetime.utcnow().isoformat()
        }
        self.client.publish(TOPIC_RESULT, json.dumps(payload), retain=True)
        log.info(f"[POS] {final_result.room} ({final_result.x}, {final_result.y}) "
                 f"conf={final_result.confidence:.2f} via {final_result.method}")

        # Notify Home Assistant via webhook (optional)
        if HA_WEBHOOK_URL:
            try:
                requests.post(HA_WEBHOOK_URL, json=payload, timeout=3)
            except Exception:
                pass

    def _retrain_loop(self):
        """Retrain k-NN model every 5 minutes."""
        while True:
            time.sleep(300)
            log.info("[KNN] Periodic retrain...")
            self.knn.train()


# ── Training helper CLI ────────────────────────────────────────────────────────

def training_cli():
    """
    Interactive CLI to record training fingerprints.
    Run: python positioning_backend.py train
    """
    import sys
    broker_client = mqtt.Client(client_id="openpos-trainer", protocol=mqtt.MQTTv5)
    broker_client.username_pw_set(MQTT_USER, MQTT_PASS)
    broker_client.connect(MQTT_HOST, MQTT_PORT)
    broker_client.loop_start()

    print("\n=== OpenPositioning Training CLI ===")
    print("Format: <room>_x<X>_y<Y>  e.g. Schlafzimmer_x2.5_y1.0")
    print("Type 'stop' to end training, 'quit' to exit.\n")

    while True:
        label = input("Label (or 'quit'): ").strip()
        if label == "quit":
            break
        if not label:
            continue

        # Start training on all nodes
        cmd = json.dumps({"cmd": "training_start", "node": "all", "label": label})
        broker_client.publish(TOPIC_CMD, cmd)
        print(f"  → Recording '{label}' for 5 seconds...")
        time.sleep(5)

        cmd = json.dumps({"cmd": "training_stop", "node": "all"})
        broker_client.publish(TOPIC_CMD, cmd)
        print(f"  ✓ Done! Move to next position.\n")

    broker_client.loop_stop()
    broker_client.disconnect()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "train":
        training_cli()
    else:
        PositioningBackend().start()
