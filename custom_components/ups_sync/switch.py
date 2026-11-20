"""Armed and maintenance switches."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity

from .controller import Controller
from .entity import UpsSyncEntity
from .machine import RESTORE_OVERDUE


def _min(seconds: float) -> str:
    return f"{seconds / 60:g} min"


def describe(ctl: Controller, key: str) -> str:
    """What the switch does, with this install's own timings.

    Shown under Attributes when the switch is tapped: Home Assistant has no
    other place for a description, and the README may be out of reach during
    an outage, so this must stand on its own. Options reload the entry, so
    the text always matches the timings in use.
    """
    p = ctl.params
    grid = bool(ctl.grid_entities)
    # The power-cycle needs the servers dark for t_dead and seen dark by
    # this process for t_grace (decide(), DoRecov): the longer one counts.
    cycle = max(p.t_dead, p.t_grace)
    restore = (
        f"after {_min(p.t_hold)} of UPS input"
        + (" with a grid entity showing the grid up" if grid else "")
        + f", once \"UPS battery charge\" is at least {p.c_restore:g} % "
        + f"(on a card that does not report charge: once \"Runtime with "
        + f"servers on\" is at least {p.e_restore:g})"
    )
    if key == "armed":
        return (
            "On: sends commands. Shuts the servers down after "
            f"{_min(p.t_ob)} on UPS battery or at once on low battery"
            + (
                f", or after {_min(p.t_grid)} of the grid entities showing the "
                "grid down (once calibrated)"
                if grid
                else ""
            )
            + ". Cuts their outlet group once the UPS load confirms they are "
            f"off: {_min(p.t_grace)} after the last one goes quiet if every "
            f"shutdown was accepted, or {_min(p.t_dead)} after regardless, "
            "whichever comes first. Turns the "
            f"group back on {restore}. The same rule restores a group "
            "switched off by hand. Powers on a server that has been off "
            f"{_min(p.t_wake)} while another runs, if the UPS has input"
            + (" and a grid entity shows the grid up" if grid else "")
            + f" and \"Runtime with servers on\" is at least {p.e_restore:g}. "
            f"If every server has been off {_min(cycle)} on live outlets, "
            "cuts the group (once calibrated) so they boot when it is "
            "restored: turn on "
            "Maintenance before shutting them down by hand. "
            "Off: sends no commands, only watches; \"Dry run would do\" "
            "shows what it would do. Turning it off does not recall a "
            "shutdown a server has already accepted."
        )
    return (
        "On: for working on the servers by hand. Nothing is powered on: no "
        "restore, no wake, no power-cycle"
        + (", and a grid outage does not start a shutdown" if grid else "")
        + f". {_min(p.t_ob)} on UPS battery or low battery still shuts them "
        "down and cuts the group; the group then stays off until you switch "
        "it on at the UPS card or turn Maintenance off. Turning Maintenance "
        "on does not stop a shutdown already under way; only turning Armed "
        "off stops further commands. Servers that cannot be revived and a "
        f"restore overdue for {RESTORE_OVERDUE / 3600:g} h are not reported "
        "as problems. "
        "Off: a group cut while it was on is turned back on "
        f"{restore}, possibly at once. "
        + (
            f"A server left off gets a fresh {_min(p.t_wake)} before it is "
            "powered on"
            + (
                f", and {_min(cycle)} before the group is power-cycled."
                if cycle != p.t_wake
                else " or the group is power-cycled."
            )
        )
        + (
            f" During a grid outage the {_min(p.t_grid)} grid timer starts "
            "again, then (once calibrated) the servers are shut down."
            if grid
            else ""
        )
    )


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    ctl = entry.runtime_data
    async_add_entities(
        [FlagSwitch(ctl, entry.entry_id, "armed"), FlagSwitch(ctl, entry.entry_id, "maintenance")]
    )


class FlagSwitch(UpsSyncEntity, SwitchEntity):
    # Static text: no need to keep a copy of it in the history.
    _unrecorded_attributes = frozenset({"description"})

    def __init__(self, ctl, entry_id: str, key: str) -> None:
        super().__init__(ctl, entry_id, key)
        self._flag = key
        self._attr_extra_state_attributes = {"description": describe(ctl, key)}

    @property
    def is_on(self) -> bool:
        return getattr(self.ctl, self._flag)

    async def async_turn_on(self, **kwargs) -> None:
        await self.ctl.async_set_flag(self._flag, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.ctl.async_set_flag(self._flag, False)
