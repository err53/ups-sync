"""Shared entity base."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import CONTROLS_URL, DOMAIN
from .controller import Controller


class UpsSyncEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, ctl: Controller, entry_id: str, key: str) -> None:
        self.ctl = ctl
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="UPS sync",
            # Home Assistant has no description field for a device or an
            # entity; this shows as a link in the Device info card.
            configuration_url=CONTROLS_URL,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.ctl.add_listener(self.async_write_ha_state))
