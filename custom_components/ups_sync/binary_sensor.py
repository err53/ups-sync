"""Problem sensor, and the yes/no facts the controller acts on."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory

from .controller import Controller
from .entity import UpsSyncEntity

POWER = BinarySensorDeviceClass.POWER


def _obs(field: str) -> Callable[[Controller], bool | None]:
    return lambda ctl: None if ctl.obs is None else getattr(ctl.obs, field)


def _grid(ctl: Controller) -> bool | None:
    """On: a grid entity shows the grid up. Off: they all show it down.
    Unknown: none of them is saying anything."""
    if ctl.obs is None:
        return None
    if ctl.obs.grid_ok:
        return True
    return False if ctl.obs.grid_down else None


# key, value, device class
FACTS: tuple[
    tuple[str, Callable[[Controller], bool | None], BinarySensorDeviceClass | None], ...
] = (
    ("input", _obs("input_present"), POWER),
    ("on_battery", _obs("on_battery"), None),
    ("low_battery", _obs("low_battery"), BinarySensorDeviceClass.BATTERY),
    ("group", _obs("group_on"), POWER),
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    ctl = entry.runtime_data
    eid = entry.entry_id
    entities: list[BinarySensorEntity] = [
        ProblemSensor(ctl, eid, "problem"),
        CalibratedSensor(ctl, eid, "calibrated"),
        *(FactSensor(ctl, eid, key, value, cls) for key, value, cls in FACTS),
    ]
    if ctl.grid_entities:
        entities.append(FactSensor(ctl, eid, "grid", _grid, POWER))
    for name in ctl.servers:
        entities.append(
            FactSensor(
                ctl,
                eid,
                "server_reachable",
                lambda c, n=name: c.obs.servers[n].reachable,
                BinarySensorDeviceClass.CONNECTIVITY,
                server=name,
            )
        )
        entities.append(
            FactSensor(
                ctl,
                eid,
                "server_power",
                lambda c, n=name: c.obs.servers[n].chassis_on,
                POWER,
                server=name,
            )
        )
    async_add_entities(entities)


class ProblemSensor(UpsSyncEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def is_on(self) -> bool:
        return self.ctl.problem


class CalibratedSensor(UpsSyncEntity, BinarySensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        return self.ctl.calibrated


class FactSensor(UpsSyncEntity, BinarySensorEntity):
    """One reading from the last poll. Unavailable while the UPS is unreadable."""

    def __init__(self, ctl, entry_id, key, value, device_class, server=None) -> None:
        super().__init__(ctl, entry_id, key)
        self._value = value
        self._attr_device_class = device_class
        if server is not None:
            self._attr_unique_id = f"{entry_id}_{key}_{server}"
            self._attr_translation_placeholders = {"server": server}

    @property
    def available(self) -> bool:
        return self.ctl.fresh

    @property
    def is_on(self) -> bool | None:
        return self._value(self.ctl)
