"""Home Assistant level tests: config flow, entities, dry run, armed actions."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ups_sync.const import DOMAIN
from custom_components.ups_sync.snmp import UpsError, UpsReading, parse_reading

UPS = {"host": "10.0.0.4", "read_community": "public", "write_community": "private", "group": 2}
SERVERS = [
    {"name": "f", "host": "10.0.0.13", "reach_port": 443, "idrac_host": "10.0.0.12",
     "idrac_user": "u", "idrac_password": "p"},
    {"name": "s", "host": "10.0.0.11", "reach_port": 8006, "pve_node": "s",
     "pve_token": "u@pve!t=x", "idrac_host": "10.0.0.10", "idrac_user": "u",
     "idrac_password": "p"},
]


def _engine():
    return SimpleNamespace(close_dispatcher=lambda: None)


class Rig:
    """Fake UPS and servers, plus a clock the test can move."""

    def __init__(self):
        self.now = 1000.0
        self.reading = UpsReading(True, False, False, 57.0, 400.0, True)
        self.read_error = None
        self.reach = {"10.0.0.13": True, "10.0.0.11": True}
        self.chassis = {"10.0.0.12": True, "10.0.0.10": True}
        self.calls = []
        rig = self

        class FakeApc:
            def __init__(self, *a):
                pass

            async def read(self):
                if rig.read_error:
                    raise UpsError(rig.read_error)
                return rig.reading

            async def set_group(self, on):
                rig.calls.append(("group", on))

        async def reachable(server):
            return rig.reach[server.cfg.host]

        async def chassis_on(server):
            return rig.chassis.get(server.cfg.idrac_host)

        async def shutdown(server):
            rig.calls.append(("shutdown", server.cfg.name))
            return "test"

        async def power_on(server):
            rig.calls.append(("wake", server.cfg.name))

        server = "custom_components.ups_sync.servers.Server"
        clock = SimpleNamespace(monotonic=lambda: rig.now, time=lambda: rig.now)
        self.patches = [
            patch("custom_components.ups_sync.ApcClient", FakeApc),
            patch("custom_components.ups_sync.config_flow.ApcClient", FakeApc),
            patch("custom_components.ups_sync.SnmpEngine", _engine),
            patch("custom_components.ups_sync.config_flow.SnmpEngine", _engine),
            patch(f"{server}.reachable", reachable),
            patch(f"{server}.chassis_on", chassis_on),
            patch(f"{server}.shutdown", shutdown),
            patch(f"{server}.power_on", power_on),
            patch("custom_components.ups_sync.controller.time", clock),
        ]

    def set(self, **changes):
        self.reading = UpsReading(**{**self.reading.__dict__, **changes})


@pytest.fixture
def rig():
    r = Rig()
    for p in r.patches:
        p.start()
    yield r
    for p in r.patches:
        p.stop()


async def setup(hass, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN, data={**UPS, "servers": SERVERS}, options=options or {}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


async def test_config_flow_creates_entry(hass, rig):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], UPS)
    assert result["step_id"] == "server"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SERVERS[0], "add_another": True}
    )
    assert result["step_id"] == "server"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SERVERS[1], "add_another": False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {**UPS, "servers": SERVERS}
    await hass.async_block_till_done()
    assert hass.states.get("sensor.ups_sync_state").state == "idle"
    assert hass.states.get("switch.ups_sync_armed").state == "off"


async def test_config_flow_errors(hass, rig):
    rig.read_error = "timeout"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], UPS)
    assert result["errors"] == {"base": "cannot_connect"}
    rig.read_error = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], UPS)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "x", "host": "h", "reach_port": 22, "add_another": False}
    )
    assert result["errors"] == {"base": "no_channel"}
    rig.chassis["10.0.0.12"] = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SERVERS[0], "add_another": False}
    )
    assert result["errors"] == {"base": "idrac_failed"}


async def test_dry_run_reports_but_never_acts(hass, rig):
    ctl = await setup(hass, {"load_idle_max": 210})
    rig.set(input_present=False, on_battery=True)
    await ctl.async_step()
    rig.now += 130
    await ctl.async_step()
    assert rig.calls == []
    assert ctl.state.ctl == "idle"
    assert [w.split(":")[0] for w in ctl.would_act] == ["shutdown f", "shutdown s"]
    assert hass.states.get("sensor.ups_sync_state").attributes["would_act"]


async def test_armed_full_cycle(hass, rig):
    ctl = await setup(hass, {"load_idle_max": 210})
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.ups_sync_armed"}, blocking=True)
    assert ctl.armed

    rig.set(input_present=False, on_battery=True)
    await ctl.async_step()
    rig.now += 130
    await ctl.async_step()
    assert rig.calls == [("shutdown", "f"), ("shutdown", "s")]
    assert hass.states.get("sensor.ups_sync_state").state == "shutdown"

    # Servers go quiet. Load still high: no cut.
    rig.reach = dict.fromkeys(rig.reach, False)
    rig.chassis = dict.fromkeys(rig.chassis, False)
    await ctl.async_step()
    rig.now += 130
    await ctl.async_step()
    assert ("group", False) not in rig.calls

    rig.set(load_w=170.0)
    await ctl.async_step()
    assert rig.calls[-1] == ("group", False)

    rig.set(group_on=False, load_w=150.0)
    await ctl.async_step()
    assert hass.states.get("sensor.ups_sync_state").state == "cut"

    rig.set(input_present=True, on_battery=False)
    await ctl.async_step()
    rig.now += 370
    await ctl.async_step()
    assert rig.calls[-1] == ("group", True)
    rig.set(group_on=True)
    await ctl.async_step()
    assert hass.states.get("sensor.ups_sync_state").state == "idle"


async def test_cut_is_rechecked_just_before_it_happens(hass, rig):
    ctl = await setup(hass, {"load_idle_max": 210})
    await ctl.async_set_flag("armed", True)
    rig.reach = dict.fromkeys(rig.reach, False)
    rig.chassis = dict.fromkeys(rig.chassis, False)
    rig.set(load_w=170.0)
    await ctl.async_step()
    rig.now += 610

    readings = iter([rig.reading, UpsReading(True, False, False, 57.0, 290.0, True)])
    with patch.object(ctl.ups, "read", lambda: _next(readings)):
        await ctl.async_step()
    assert ("group", False) not in rig.calls
    assert "re-check" in ctl.last_error


async def _next(it):
    return next(it)


async def test_uncalibrated_and_stale_raise_problem(hass, rig):
    ctl = await setup(hass)
    assert hass.states.get("binary_sensor.ups_sync_problem").state == "on"  # uncalibrated
    ctl.params = type(ctl.params)(load_idle_max=210)
    await ctl.async_step()
    assert not ctl.problem
    rig.read_error = "timeout"
    rig.now += 200
    await ctl.async_step()
    assert ctl.problem and ctl.state.ctl == "idle"


async def test_flags_and_dead_timer_survive_restart(hass, rig):
    ctl = await setup(hass, {"load_idle_max": 210})
    await ctl.async_set_flag("armed", True)
    await ctl.async_set_flag("maintenance", True)
    rig.reach = dict.fromkeys(rig.reach, False)
    await ctl.async_step()
    dead = ctl.state.dead_since
    assert dead is not None
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    again = entry.runtime_data
    assert again is not ctl
    assert again.armed and again.maintenance and again.state.dead_since == dead


def test_parse_reading():
    r = parse_reading(2, 376400, 412, 96000, 1)
    assert r.input_present and not r.low_battery and r.group_on
    assert round(r.runtime_min, 1) == 62.7 and r.load_w == 412
    r = parse_reading(3, 90000, 412, 96000, 2)
    assert r.on_battery and r.low_battery and not r.group_on
    for status in (1, 7, 9, 15):  # unknown, off, bypass, self-test
        with pytest.raises(UpsError):
            parse_reading(status, 1, 1, 1, 1)
    with pytest.raises(UpsError):
        parse_reading(2, 1, 1, 1, 3)
