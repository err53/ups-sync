"""Live test on real hardware. Skipped unless UPS_SYNC_LIVE_HOST is set.

Runs the integration inside Home Assistant against a real APC card and an
EMPTY outlet group, with pretend servers that are always off. The controller
must restore the group, then cut it again as "dead outlets", through real
SNMP. The group is put back the way it was found.

    UPS_SYNC_LIVE_HOST=192.168.1.4 UPS_SYNC_LIVE_GROUP=3 pytest tests/test_live_group.py -s
"""

import asyncio
import os
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ups_sync.const import DOMAIN

HOST = os.environ.get("UPS_SYNC_LIVE_HOST")
GROUP = int(os.environ.get("UPS_SYNC_LIVE_GROUP", "0"))
pytestmark = pytest.mark.skipif(not (HOST and GROUP), reason="live hardware test")


async def _off(_server):
    return False


async def wait_for(ctl, want_group_on: bool, seconds: int = 40):
    for _ in range(seconds):
        await ctl.async_step()
        if ctl.obs.group_on == want_group_on and ctl.state.ctl == (
            "idle" if want_group_on else "cut"
        ):
            return
        await asyncio.sleep(1)
    raise AssertionError(f"group never became {'on' if want_group_on else 'off'}")


async def test_restore_then_cut_on_real_group(hass, socket_enabled):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "host": HOST,
            "read_community": os.environ.get("UPS_SYNC_LIVE_READ", "public"),
            "write_community": os.environ.get("UPS_SYNC_LIVE_WRITE", "private"),
            "group": GROUP,
            "servers": [
                {
                    "name": "pretend",
                    "host": "127.0.0.1",
                    "reach_port": 9,
                    "idrac_host": "127.0.0.1",
                    "idrac_user": "x",
                    "idrac_password": "x",
                }
            ],
        },
        # The group is empty, so the whole UPS load counts as "idle" here.
        options={"load_idle_max": 100000, "t_hold": 4, "t_dead": 4, "t_grace": 2},
    )
    entry.add_to_hass(hass)
    server = "custom_components.ups_sync.servers.Server"
    with patch(f"{server}.reachable", _off), patch(f"{server}.chassis_on", _off):
        assert await hass.config_entries.async_setup(entry.entry_id)
        ctl = entry.runtime_data
        found_on = ctl.obs.group_on
        print(
            f"\nfound group {GROUP} {'on' if found_on else 'off'}, load {ctl.obs.load_w} W, "
            f"runtime {ctl.obs.runtime_min:.0f} min, charge {ctl.obs.charge_pct:.0f} %"
        )
        if ctl.obs.charge_pct < ctl.params.c_restore:
            await hass.config_entries.async_unload(entry.entry_id)
            pytest.skip(
                f"charge {ctl.obs.charge_pct:.0f} % is under the restore charge"
            )

        await ctl.async_step()
        assert ctl.last_action == "", "dry run must not act"
        await ctl.async_set_flag("armed", True)
        try:
            for want in [True, False] if not found_on else [False, True, False]:
                await wait_for(ctl, want)
                print(
                    f"group {GROUP} is now {'on' if want else 'off'}, controller {ctl.state.ctl}"
                )
        finally:
            await ctl.async_set_flag("armed", False)
            # A restore may have been issued in the same step that saw the cut.
            await asyncio.sleep(3)
            if (await ctl.ups.read()).group_on != found_on:
                await ctl.ups.set_group(found_on)
                await asyncio.sleep(3)
            assert (await ctl.ups.read()).group_on == found_on
        await hass.config_entries.async_unload(entry.entry_id)
