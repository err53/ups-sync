"""Armed and maintenance switches."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity

from .entity import UpsSyncEntity

# Shown under Attributes when the switch is tapped. Home Assistant has no
# other place for a description. The README section "Armed and Maintenance"
# says the same at more length.
DESCRIPTIONS = {
    "armed": (
        "On: acts. Shuts the servers down after 2 min on UPS battery, on low "
        "battery, or after 5 min of grid outage if grid entities are set; "
        "cuts their outlet group once they are off; turns it back on when "
        "power is back and the battery can carry them; powers on a server "
        "that is off while another runs. Off: only watches, and \"Dry run "
        "would do\" says what it would have done."
    ),
    "maintenance": (
        "On: for working on the servers by hand. Nothing is powered on: no "
        "restore, no wake, no power-cycle, and a grid outage alone does not "
        "shut them down. 2 min on UPS battery or low battery still shuts "
        "them down and cuts the group; they then stay off until you turn "
        "them on or turn Maintenance off. Turn it off when done."
    ),
}


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    ctl = entry.runtime_data
    async_add_entities(
        [FlagSwitch(ctl, entry.entry_id, "armed"), FlagSwitch(ctl, entry.entry_id, "maintenance")]
    )


class FlagSwitch(UpsSyncEntity, SwitchEntity):
    def __init__(self, ctl, entry_id: str, key: str) -> None:
        super().__init__(ctl, entry_id, key)
        self._flag = key
        self._attr_extra_state_attributes = {"description": DESCRIPTIONS[key]}

    @property
    def is_on(self) -> bool:
        return getattr(self.ctl, self._flag)

    async def async_turn_on(self, **kwargs) -> None:
        await self.ctl.async_set_flag(self._flag, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.ctl.async_set_flag(self._flag, False)
