"""
sensor.py — OpenPositioning HA Sensor Platform

Erstellt drei Sensoren:
  sensor.openpos_room        → aktueller Raum
  sensor.openpos_x           → X-Koordinate
  sensor.openpos_y           → Y-Koordinate
  sensor.openpos_confidence  → Konfidenz (0–1)
"""

from __future__ import annotations
import json
import logging
from homeassistant.components import mqtt
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

_LOGGER = logging.getLogger(__name__)

DEFAULT_TOPIC = "openpos/position/result"


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    topic = config.get("mqtt_topic", DEFAULT_TOPIC)

    sensors = [
        OpenPosSensor("openpos_room",       "OpenPos Raum",       "room",       None,  "mdi:map-marker"),
        OpenPosSensor("openpos_x",          "OpenPos X",          "x",          "m",   "mdi:axis-x-arrow"),
        OpenPosSensor("openpos_y",          "OpenPos Y",          "y",          "m",   "mdi:axis-y-arrow"),
        OpenPosSensor("openpos_confidence", "OpenPos Konfidenz",  "confidence", None,  "mdi:percent"),
    ]
    async_add_entities(sensors, update_before_add=True)

    @callback
    def message_received(msg):
        try:
            data = json.loads(msg.payload)
            for sensor in sensors:
                sensor.update_from_payload(data)
                sensor.async_write_ha_state()
        except Exception as e:
            _LOGGER.error("OpenPos parse error: %s", e)

    await mqtt.async_subscribe(hass, topic, message_received, 0)
    _LOGGER.info("OpenPositioning: subscribed to %s", topic)


class OpenPosSensor(SensorEntity):
    def __init__(self, unique_id: str, name: str, key: str, unit: str | None, icon: str):
        self._attr_unique_id    = unique_id
        self._attr_name         = name
        self._attr_native_unit_of_measurement = unit
        self._attr_icon         = icon
        self._key               = key
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}

    def update_from_payload(self, data: dict):
        self._attr_native_value = data.get(self._key)
        if self._key == "room":
            # Attach all position data as attributes to the room sensor
            self._attr_extra_state_attributes = {
                "x":          data.get("x"),
                "y":          data.get("y"),
                "confidence": data.get("confidence"),
                "method":     data.get("method"),
                "last_update": data.get("ts"),
            }
