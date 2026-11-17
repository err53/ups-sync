"""Problem sensor: shutdown overdue, UPS unreadable, or load guard not calibrated."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)

from .entity import UpsSyncEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([ProblemSensor(entry.runtime_data, entry.entry_id, "problem")])


class ProblemSensor(UpsSyncEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def is_on(self) -> bool:
        return self.ctl.problem
