"""Controller state, and the readings behind it as sensors of their own."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfPower, UnitOfTime

from .controller import Controller
from .entity import UpsSyncEntity
from .machine import CUT, IDLE, SHUTDOWN

MAX_STATE = 255  # Home Assistant's limit on a state's length


def _text(value: str) -> str:
    if not value:
        return "Nothing"  # an empty state shows as a blank row
    return value if len(value) <= MAX_STATE else value[: MAX_STATE - 1] + "…"


def _obs(field: str) -> Callable[[Controller], Any]:
    return lambda ctl: getattr(ctl.obs, field)


# key, value, needs a fresh UPS reading, device class, unit, diagnostic
SENSORS: tuple[tuple[str, Callable[[Controller], Any], bool, Any, Any, bool], ...] = (
    ("load", _obs("load_w"), True, SensorDeviceClass.POWER, UnitOfPower.WATT, False),
    ("runtime", _obs("runtime_min"), True, SensorDeviceClass.DURATION, UnitOfTime.MINUTES, False),
    ("reserve", _obs("reserve_min"), True, SensorDeviceClass.DURATION, UnitOfTime.MINUTES, False),
    ("peak_load", lambda ctl: ctl.peak, False, SensorDeviceClass.POWER, UnitOfPower.WATT, True),
    ("would_act", lambda ctl: _text("; ".join(ctl.would_act)), False, None, None, False),
    ("last_action", lambda ctl: _text(ctl.last_action), False, None, None, True),
    ("last_error", lambda ctl: _text(ctl.last_error), False, None, None, True),
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    ctl = entry.runtime_data
    async_add_entities(
        [
            StateSensor(ctl, entry.entry_id, "state"),
            *(ReadingSensor(ctl, entry.entry_id, *spec) for spec in SENSORS),
        ]
    )


class StateSensor(UpsSyncEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [IDLE, SHUTDOWN, CUT]

    @property
    def native_value(self) -> str:
        return self.ctl.state.ctl

    @property
    def extra_state_attributes(self) -> dict:
        return self.ctl.attributes()


class ReadingSensor(UpsSyncEntity, SensorEntity):
    def __init__(self, ctl, entry_id, key, value, needs_ups, device_class, unit, diagnostic):
        super().__init__(ctl, entry_id, key)
        self._value = value
        self._needs_ups = needs_ups
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        if unit is not None:
            self._attr_state_class = SensorStateClass.MEASUREMENT
            # Minutes to one decimal: the restore threshold compares the
            # exact value, so 25.6 must not show as 26.
            self._attr_suggested_display_precision = (
                1 if unit == UnitOfTime.MINUTES else 0
            )
        if diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        return not self._needs_ups or self.ctl.fresh

    @property
    def native_value(self):
        return self._value(self.ctl)
