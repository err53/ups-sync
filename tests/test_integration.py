"""Home Assistant level tests: config flow, entities, dry run, armed actions."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType, InvalidData
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
        self.group_name = "Servers"
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

            async def group_name(self):
                return rig.group_name

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
            patch("custom_components.ups_sync.make_engine", _engine),
            patch("custom_components.ups_sync.config_flow.make_engine", _engine),
            patch(f"{server}.reachable", reachable),
            patch(f"{server}.chassis_on", chassis_on),
            patch(f"{server}.shutdown", shutdown),
            patch(f"{server}.power_on", power_on),
            patch("custom_components.ups_sync.controller.time", clock),
        ]

    def set(self, **changes):
        self.reading = UpsReading(**{**self.reading.__dict__, **changes})

    async def run(self, ctl, seconds):
        """Let time pass with the controller polling every 15 s."""
        for _ in range(seconds // 15):
            self.now += 15
            await ctl.async_step()


@pytest.fixture
def rig():
    r = Rig()
    for p in r.patches:
        p.start()
    yield r
    for p in r.patches:
        p.stop()


async def setup(hass, rig, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN, data={**UPS, "servers": SERVERS}, options=options or {}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # Servers up for five minutes: the peak load is known.
    await rig.run(entry.runtime_data, 315)
    return entry.runtime_data


async def test_config_flow_creates_entry(hass, rig):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], UPS)
    assert result["step_id"] == "server"
    # The form says which UPS and group it means, by number and by name.
    assert result["description_placeholders"] == {
        "host": "10.0.0.4", "group": '2 "Servers"', "added": "none yet"
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SERVERS[0], "add_another": True}
    )
    assert result["step_id"] == "server"
    assert result["description_placeholders"]["added"] == "f"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SERVERS[1], "add_another": False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == 'UPS 10.0.0.4 group 2 "Servers"'
    assert result["data"] == {**UPS, "servers": SERVERS}
    await hass.async_block_till_done()
    assert hass.states.get("sensor.ups_sync_state").state == "idle"
    assert hass.states.get("switch.ups_sync_armed").state == "off"


async def test_config_flow_numbers_are_typed_boxes_and_stored_as_integers(hass, rig):
    """A bare range renders as a slider that shows no value."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    box = result["data_schema"].schema["group"]
    assert box.config["mode"] == "box" and (box.config["min"], box.config["max"]) == (1, 16)
    for bad in (2.5, 0, 17):  # 2.5 must not quietly become group 2
        with pytest.raises(InvalidData):
            await hass.config_entries.flow.async_configure(result["flow_id"], {**UPS, "group": bad})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**UPS, "group": 3.0})
    assert result["data_schema"].schema["reach_port"].config["mode"] == "box"
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {**SERVERS[0], "reach_port": 443.9, "add_another": False}
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SERVERS[0], "reach_port": 443.0, "add_another": False}
    )
    assert result["title"] == 'UPS 10.0.0.4 group 3 "Servers"'
    group, reach = result["data"]["group"], result["data"]["servers"][0]["reach_port"]
    assert (group, reach) == (3, 443) and type(group) is int and type(reach) is int


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
    ctl = await setup(hass, rig, {"load_idle_max": 210})
    rig.set(input_present=False, on_battery=True)
    await rig.run(ctl, 135)
    assert rig.calls == []
    assert ctl.state.ctl == "idle"
    assert [w.split(":")[0] for w in ctl.would_act] == ["shutdown f", "shutdown s"]
    assert hass.states.get("sensor.ups_sync_state").attributes["would_act"]


async def test_armed_full_cycle(hass, rig):
    ctl = await setup(hass, rig, {"load_idle_max": 210})
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.ups_sync_armed"}, blocking=True)
    assert ctl.armed

    rig.set(input_present=False, on_battery=True)
    await rig.run(ctl, 105)
    assert rig.calls == [] and ctl.calibrated and ctl.peak == 400.0
    await rig.run(ctl, 30)
    assert rig.calls == [("shutdown", "f"), ("shutdown", "s")]
    assert hass.states.get("sensor.ups_sync_state").state == "shutdown"

    # Servers go quiet. Load still high: no cut.
    rig.reach = dict.fromkeys(rig.reach, False)
    rig.chassis = dict.fromkeys(rig.chassis, False)
    await rig.run(ctl, 300)
    assert ("group", False) not in rig.calls

    rig.set(load_w=170.0)
    await rig.run(ctl, 15)
    assert rig.calls[-1] == ("group", False)
    # The card acknowledged the command: the shutdown is over, even though
    # the group has not been seen off yet.
    assert ctl.state.ctl == "cut" and ctl.obs.group_on

    # The group is off, so the UPS reports a long runtime: 90 min at 150 W.
    # At the 400 W the servers were seen to draw that is only 23 min.
    rig.set(group_on=False, load_w=150.0, runtime_min=90.0)
    await rig.run(ctl, 15)
    assert hass.states.get("sensor.ups_sync_state").state == "cut"
    assert ctl.peak == 400.0  # not dragged down by the light load
    rig.set(input_present=True, on_battery=False)
    await rig.run(ctl, 600)
    assert ("group", True) not in rig.calls
    assert round(ctl.obs.reserve_min) == 23

    rig.set(runtime_min=110.0)  # 28 min at 400 W
    await rig.run(ctl, 15)
    assert rig.calls[-1] == ("group", True)
    rig.set(group_on=True)
    await rig.run(ctl, 15)
    assert hass.states.get("sensor.ups_sync_state").state == "idle"


async def test_cut_is_rechecked_just_before_it_happens(hass, rig):
    ctl = await setup(hass, rig, {"load_idle_max": 210})
    await ctl.async_set_flag("armed", True)
    rig.reach = dict.fromkeys(rig.reach, False)
    rig.chassis = dict.fromkeys(rig.chassis, False)
    rig.set(load_w=170.0)
    await rig.run(ctl, 600)
    assert rig.calls == []

    rig.now += 15
    busy = UpsReading(True, False, False, 57.0, 290.0, True)
    readings = iter([rig.reading, busy])
    with patch.object(ctl.ups, "read", lambda: _next(readings)):
        await ctl.async_step()
    assert ("group", False) not in rig.calls
    assert "re-check" in ctl.last_error


async def test_failing_group_command_raises_problem_and_notifies_once(hass, rig):
    ctl = await setup(hass, rig, {"load_idle_max": 210})
    await ctl.async_set_flag("armed", True)
    rig.reach = dict.fromkeys(rig.reach, False)
    rig.chassis = dict.fromkeys(rig.chassis, False)
    rig.set(load_w=170.0)
    notes = []
    hass.bus.async_listen("ups_sync_action", lambda e: notes.append(e.data["message"]))

    async def refuse(on):
        raise UpsError("noAccess")

    with patch.object(ctl.ups, "set_group", refuse):
        await rig.run(ctl, 690)
        await hass.async_block_till_done()
    assert ctl.group_cmd_failed and ctl.problem
    assert len(notes) == 1 and "FAILED: noAccess" in notes[0]
    await rig.run(ctl, 15)
    assert not ctl.group_cmd_failed and rig.calls[-1] == ("group", False)

    # A failure that a person then fixes by hand also clears the flag.
    ctl.group_cmd_failed = True
    rig.set(group_on=False, load_w=150.0)
    await rig.run(ctl, 15)
    assert not ctl.group_cmd_failed


async def test_peak_load_is_not_learned_while_the_group_is_off(hass, rig):
    """Before any full-load reading, a restore must be refused, not guessed."""
    rig.set(group_on=False, load_w=150.0, runtime_min=150.0)
    rig.reach = dict.fromkeys(rig.reach, False)
    rig.chassis = dict.fromkeys(rig.chassis, None)
    ctl = await setup(hass, rig, {"load_idle_max": 210})
    await ctl.async_set_flag("armed", True)
    await rig.run(ctl, 900)
    assert ctl.peak is None and ctl.obs.reserve_min is None
    assert rig.calls == [] and not ctl.calibrated and ctl.problem


async def test_peak_load_ignores_standby_and_boot_readings(hass, rig):
    """Group on but a server off or booting: neither 170 W nor 700 W counts."""
    rig.reach["10.0.0.13"] = False
    rig.set(load_w=170.0)
    ctl = await setup(hass, rig, {"load_idle_max": 210})
    await rig.run(ctl, 60)
    assert ctl.peak is None and not ctl.calibrated
    rig.set(load_w=700.0)  # booting
    await rig.run(ctl, 60)
    assert ctl.peak is None
    rig.reach["10.0.0.13"] = True  # answers, but its boot is not over
    await rig.run(ctl, 285)
    assert ctl.peak is None
    rig.set(load_w=400.0)
    await rig.run(ctl, 45)
    assert ctl.peak == 400.0 and ctl.calibrated


async def test_a_reboot_during_unreadable_ups_does_not_raise_the_peak(hass, rig):
    ctl = await setup(hass, rig, {"load_idle_max": 210})
    assert ctl.peak == 400.0
    rig.read_error = "timeout"  # servers reboot while nobody can see
    await rig.run(ctl, 60)
    rig.read_error = None
    rig.set(load_w=830.0)  # both answer again, still booting
    await rig.run(ctl, 285)
    assert ctl.peak == 400.0
    rig.set(load_w=410.0)
    await rig.run(ctl, 60)
    assert ctl.peak == 410.0


async def test_a_reboot_during_a_stalled_loop_does_not_raise_the_peak(hass, rig):
    """No read fails, but nothing polled for over two minutes."""
    ctl = await setup(hass, rig, {"load_idle_max": 210})
    assert ctl.peak == 400.0
    rig.now += 200
    rig.set(load_w=830.0)
    await rig.run(ctl, 285)
    assert ctl.peak == 400.0


async def test_grid_entities_trigger_shutdown_and_hold_back_restore(hass, rig):
    """The UPS stays online the whole time: an upstream battery is feeding it."""
    hass.states.async_set("sensor.wall_voltage", "241.5")
    hass.states.async_set("binary_sensor.grid_relay", "on")
    ctl = await setup(
        hass, rig,
        {"load_idle_max": 210, "grid_entities": ["sensor.wall_voltage", "binary_sensor.grid_relay"]},
    )
    await ctl.async_set_flag("armed", True)
    assert not ctl.obs.grid_down

    hass.states.async_set("sensor.wall_voltage", "unavailable")
    await rig.run(ctl, 600)
    assert rig.calls == []  # one entity still shows life

    hass.states.async_set("binary_sensor.grid_relay", "off")
    await rig.run(ctl, 285)
    assert rig.calls == [] and ctl.obs.grid_down
    await rig.run(ctl, 30)
    assert rig.calls == [("shutdown", "f"), ("shutdown", "s")]
    assert "grid is down" in ctl.last_action

    rig.reach = dict.fromkeys(rig.reach, False)
    rig.chassis = dict.fromkeys(rig.chassis, False)
    rig.set(load_w=170.0)
    await rig.run(ctl, 150)
    assert rig.calls[-1] == ("group", False)
    rig.set(group_on=False, load_w=150.0, runtime_min=150.0)
    await rig.run(ctl, 1800)
    assert ("group", True) not in rig.calls  # UPS has input, grid is still down

    hass.states.async_set("sensor.wall_voltage", "239.0")
    await rig.run(ctl, 345)
    assert ("group", True) not in rig.calls
    await rig.run(ctl, 30)
    assert rig.calls[-1] == ("group", True)


@pytest.mark.parametrize(
    ("state", "shows_grid"),
    [("241.5", True), ("0", False), ("0.0", False), ("on", True), ("home", True),
     ("off", False), ("unavailable", False), ("unknown", False)],
)
async def test_what_counts_as_the_grid_being_up(hass, rig, state, shows_grid):
    hass.states.async_set("sensor.g", state)
    ctl = await setup(hass, rig, {"grid_entities": ["sensor.g"]})
    assert ctl.obs.grid_down is not shows_grid
    assert ctl.grid_missing == []


async def test_an_entity_home_assistant_does_not_know_is_not_a_grid_loss(hass, rig):
    """Not loaded yet, renamed or deleted: no evidence, but worth a warning."""
    hass.states.async_set("sensor.real", "unavailable")
    ctl = await setup(
        hass, rig, {"load_idle_max": 210, "grid_entities": ["sensor.typo", "sensor.late"]}
    )
    await ctl.async_set_flag("armed", True)
    await rig.run(ctl, 900)
    assert not ctl.obs.grid_down and rig.calls == []
    assert ctl.grid_missing == ["sensor.typo", "sensor.late"]
    attrs = hass.states.get("sensor.ups_sync_state").attributes
    assert attrs["grid_entities_missing"] == ["sensor.typo", "sensor.late"]
    assert ctl.problem  # Home Assistant is running: they should exist by now

    # After a restart Home Assistant writes a placeholder "unavailable" state
    # for an entity whose integration has not loaded yet. Still no evidence.
    hass.states.async_set("sensor.late", "unavailable", {"restored": True})
    await rig.run(ctl, 900)
    assert not ctl.obs.grid_down and rig.calls == []
    assert ctl.grid_missing == ["sensor.typo", "sensor.late"] and ctl.problem

    # One loads late and shows the grid up: fine. The typo still warns.
    hass.states.async_set("sensor.late", "240")
    await rig.run(ctl, 15)
    assert not ctl.obs.grid_down and ctl.grid_missing == ["sensor.typo"] and ctl.problem
    # The known one going unavailable IS a grid loss, typo or not.
    hass.states.async_set("sensor.late", "unavailable")
    await rig.run(ctl, 315)
    assert ctl.obs.grid_down and rig.calls == [("shutdown", "f"), ("shutdown", "s")]


async def test_grid_shutdown_waits_for_calibration(hass, rig):
    """No idle load set: a cut could never be approved, so do not start."""
    hass.states.async_set("sensor.g", "unavailable")
    ctl = await setup(hass, rig, {"grid_entities": ["sensor.g"]})
    await ctl.async_set_flag("armed", True)
    assert ctl.peak == 400.0 and not ctl.calibrated
    await rig.run(ctl, 1800)
    assert rig.calls == [] and ctl.state.ctl == "idle"


async def test_grid_sensor_holding_the_group_off_is_reported(hass, rig):
    hass.states.async_set("sensor.g", "240")
    ctl = await setup(hass, rig, {"load_idle_max": 210, "grid_entities": ["sensor.g"]})
    await ctl.async_set_flag("armed", True)
    hass.states.async_set("sensor.g", "unavailable")
    rig.reach = dict.fromkeys(rig.reach, False)
    rig.chassis = dict.fromkeys(rig.chassis, False)
    rig.set(group_on=False, load_w=150.0, runtime_min=150.0)
    await rig.run(ctl, 60)
    attrs = hass.states.get("sensor.ups_sync_state").attributes
    assert attrs["waiting_for_grid"] is True and not ctl.stuck
    await rig.run(ctl, 6 * 3600)
    assert ctl.stuck and ctl.problem and ("group", True) not in rig.calls


async def test_reloading_options_while_cut_and_grid_down_keeps_the_group_off(hass, rig):
    hass.states.async_set("sensor.g", "unavailable")
    ctl = await setup(hass, rig, {"load_idle_max": 210, "grid_entities": ["sensor.g"]})
    await ctl.async_set_flag("armed", True)
    rig.reach = dict.fromkeys(rig.reach, False)
    rig.chassis = dict.fromkeys(rig.chassis, False)
    rig.set(group_on=False, load_w=150.0, runtime_min=150.0)
    await rig.run(ctl, 600)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "t_grid": 600}
    )
    await hass.async_block_till_done()
    again = entry.runtime_data
    assert again is not ctl and again.armed and again.state.ctl == "cut"
    await rig.run(again, 1800)
    assert rig.calls == [] and again.obs.grid_down
    hass.states.async_set("sensor.g", "240")
    await rig.run(again, 345)
    assert rig.calls == []  # six minutes of grid first
    await rig.run(again, 30)
    assert rig.calls[0] == ("group", True) and set(rig.calls) == {("group", True)}


async def test_turning_maintenance_off_restarts_the_grid_timer(hass, rig):
    hass.states.async_set("sensor.g", "unavailable")
    ctl = await setup(hass, rig, {"load_idle_max": 210, "grid_entities": ["sensor.g"]})
    await ctl.async_set_flag("armed", True)
    await ctl.async_set_flag("maintenance", True)
    await rig.run(ctl, 1800)
    assert rig.calls == []
    await ctl.async_set_flag("maintenance", False)
    await rig.run(ctl, 285)
    assert rig.calls == []
    await rig.run(ctl, 30)
    assert rig.calls == [("shutdown", "f"), ("shutdown", "s")]


async def test_no_grid_entities_means_the_feature_is_off(hass, rig):
    ctl = await setup(hass, rig, {"load_idle_max": 210})
    assert ctl.obs.grid_down is False


async def test_options_form_offers_grid_entities(hass, rig):
    await setup(hass, rig, {"load_idle_max": 210})
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    form = await hass.config_entries.options.async_init(entry.entry_id)
    keys = {str(k) for k in form["data_schema"].schema}
    assert {"grid_entities", "t_grid", "load_idle_max"} <= keys
    hass.states.async_set("sensor.wall_voltage", "241")
    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {"load_idle_max": 210, "notify_services": "", "grid_entities": ["sensor.wall_voltage"],
         "t_ob": 120, "t_grid": 300, "t_grace": 120, "t_dead": 600, "t_wake": 600,
         "t_hold": 360, "t_wait": 1440, "e_restore": 26},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.runtime_data.grid_entities == ["sensor.wall_voltage"]
    assert entry.runtime_data.params.t_grid == 300


async def test_stuck_servers_raise_problem(hass, rig):
    ctl = await setup(hass, rig, {"load_idle_max": 210})
    rig.reach = dict.fromkeys(rig.reach, False)
    rig.chassis = dict.fromkeys(rig.chassis, False)
    await rig.run(ctl, 615)  # load stays 400 W: the guard refuses the cycle
    assert ctl.stuck and ctl.problem
    assert hass.states.get("sensor.ups_sync_state").attributes["stuck_servers_off"]


async def test_unload_stops_the_controller(hass, rig):
    ctl = await setup(hass, rig, {"load_idle_max": 210})
    await ctl.async_set_flag("armed", True)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert await hass.config_entries.async_unload(entry.entry_id)
    rig.set(input_present=False, on_battery=True)
    await rig.run(ctl, 300)
    assert rig.calls == []


async def _next(it):
    return next(it)


async def test_uncalibrated_and_stale_raise_problem(hass, rig):
    ctl = await setup(hass, rig)
    assert hass.states.get("binary_sensor.ups_sync_problem").state == "on"  # uncalibrated
    ctl.params = type(ctl.params)(load_idle_max=210)
    await ctl.async_step()
    assert not ctl.problem
    rig.read_error = "timeout"
    await rig.run(ctl, 195)
    assert ctl.problem and ctl.state.ctl == "idle"


async def test_flags_and_dead_timer_survive_restart(hass, rig):
    ctl = await setup(hass, rig, {"load_idle_max": 210})
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
    assert again.peak == 400.0


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
