"""Armed and maintenance switches."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity

from .entity import UpsSyncEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    ctl = entry.runtime_data
    async_add_entities(
        [FlagSwitch(ctl, entry.entry_id, "armed"), FlagSwitch(ctl, entry.entry_id, "maintenance")]
    )


class FlagSwitch(UpsSyncEntity, SwitchEntity):
    def __init__(self, ctl, entry_id: str, key: str) -> None:
        super().__init__(ctl, entry_id, key)
        self._flag = key

    @property
    def is_on(self) -> bool:
        return getattr(self.ctl, self._flag)

    async def async_turn_on(self, **kwargs) -> None:
        await self.ctl.async_set_flag(self._flag, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.ctl.async_set_flag(self._flag, False)
