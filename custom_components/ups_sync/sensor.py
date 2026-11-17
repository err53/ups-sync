"""Controller state sensor."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity

from .entity import UpsSyncEntity
from .machine import CUT, IDLE, SHUTDOWN


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([StateSensor(entry.runtime_data, entry.entry_id, "state")])


class StateSensor(UpsSyncEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [IDLE, SHUTDOWN, CUT]

    @property
    def native_value(self) -> str:
        return self.ctl.state.ctl

    @property
    def extra_state_attributes(self) -> dict:
        return self.ctl.attributes()
