"""The readings are entities of their own, not only attributes of the state sensor."""

from tests.test_integration import rig, setup  # noqa: F401  (rig is a fixture)


def state(hass, entity_id):
    s = hass.states.get(entity_id)
    assert s is not None, entity_id
    return s.state


async def test_readings_are_sensors(hass, rig):
    await setup(hass, rig, {"load_idle_max": 210})
    assert state(hass, "sensor.ups_sync_ups_load") == "400.0"
    assert state(hass, "sensor.ups_sync_ups_runtime_at_present_load") == "57.0"
    assert hass.states.get("sensor.ups_sync_runtime_with_servers_on") is None
    assert hass.states.get("sensor.ups_sync_peak_load") is None
    assert state(hass, "sensor.ups_sync_ups_battery_charge") == "100.0"
    assert state(hass, "sensor.ups_sync_dry_run_would_do") == "Nothing"
    assert state(hass, "binary_sensor.ups_sync_calibrated") == "on"
    assert state(hass, "binary_sensor.ups_sync_ups_input") == "on"
    assert state(hass, "binary_sensor.ups_sync_on_battery") == "off"
    assert state(hass, "binary_sensor.ups_sync_low_battery") == "off"
    assert state(hass, "binary_sensor.ups_sync_outlet_group") == "on"
    for server in ("f", "s"):
        assert state(hass, f"binary_sensor.ups_sync_{server}_reachable") == "on"
        assert state(hass, f"binary_sensor.ups_sync_{server}_power") == "on"


async def test_sensors_follow_the_readings(hass, rig):
    ctl = await setup(hass, rig)
    assert state(hass, "binary_sensor.ups_sync_calibrated") == "off"
    rig.set(input_present=False, on_battery=True, load_w=380.0)
    rig.reach["10.0.0.13"] = False
    rig.chassis["10.0.0.12"] = None  # iDRAC did not answer
    await rig.run(ctl, 15)
    assert state(hass, "sensor.ups_sync_ups_load") == "380.0"
    assert state(hass, "binary_sensor.ups_sync_ups_input") == "off"
    assert state(hass, "binary_sensor.ups_sync_on_battery") == "on"
    assert state(hass, "binary_sensor.ups_sync_f_reachable") == "off"
    assert state(hass, "binary_sensor.ups_sync_f_power") == "unknown"
    assert state(hass, "binary_sensor.ups_sync_s_power") == "on"


async def test_dry_run_sensor_lists_what_would_be_done(hass, rig):
    ctl = await setup(hass, rig, {"load_idle_max": 210})
    rig.set(input_present=False, on_battery=True)
    await rig.run(ctl, 135)
    would = state(hass, "sensor.ups_sync_dry_run_would_do")
    assert "shutdown f" in would and "shutdown s" in would
    assert rig.calls == []


async def test_readings_go_unavailable_when_the_ups_stops_answering(hass, rig):
    ctl = await setup(hass, rig, {"load_idle_max": 210})
    rig.read_error = "timeout"
    await rig.run(ctl, 15)  # the first failed poll is enough
    assert state(hass, "sensor.ups_sync_ups_load") == "unavailable"
    assert state(hass, "binary_sensor.ups_sync_problem") == "off"  # not stale yet
    rig.read_error = None
    await rig.run(ctl, 15)
    assert state(hass, "sensor.ups_sync_ups_load") == "400.0"
    rig.read_error = "timeout"
    await rig.run(ctl, 195)
    for entity_id in (
        "sensor.ups_sync_ups_load",
        "sensor.ups_sync_ups_battery_charge",
        "binary_sensor.ups_sync_ups_input",
        "binary_sensor.ups_sync_outlet_group",
        "binary_sensor.ups_sync_s_power",
    ):
        assert state(hass, entity_id) == "unavailable"
    # What the controller itself knows stays readable.
    assert state(hass, "binary_sensor.ups_sync_calibrated") == "on"
    assert state(hass, "binary_sensor.ups_sync_problem") == "on"
    assert state(hass, "sensor.ups_sync_last_error").startswith("UPS:")


async def test_grid_sensor_exists_only_with_grid_entities(hass, rig):
    await setup(hass, rig)
    assert hass.states.get("binary_sensor.ups_sync_grid") is None


async def test_grid_sensor_is_on_off_or_unknown(hass, rig):
    hass.states.async_set("sensor.wall", "240")
    ctl = await setup(hass, rig, {"grid_entities": ["sensor.wall"]})
    assert state(hass, "binary_sensor.ups_sync_grid") == "on"
    hass.states.async_set("sensor.wall", "unavailable")
    await rig.run(ctl, 15)
    assert state(hass, "binary_sensor.ups_sync_grid") == "off"
    hass.states.async_remove("sensor.wall")
    await rig.run(ctl, 15)
    assert state(hass, "binary_sensor.ups_sync_grid") == "unknown"


async def test_battery_charge_follows_the_card(hass, rig):
    ctl = await setup(hass, rig)
    rig.set(charge_pct=74.0)
    await rig.run(ctl, 15)
    assert state(hass, "sensor.ups_sync_ups_battery_charge") == "74.0"
    rig.read_error = "timeout"
    await rig.run(ctl, 15)
    assert state(hass, "sensor.ups_sync_ups_battery_charge") == "unavailable"


async def test_the_controls_explain_themselves(hass, rig):
    from homeassistant.helpers import device_registry as dr

    from custom_components.ups_sync.const import CONTROLS_URL, DOMAIN

    await setup(hass, rig)
    for switch in ("armed", "maintenance"):
        attrs = hass.states.get(f"switch.ups_sync_{switch}").attributes
        assert len(attrs["description"]) > 100
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert device.configuration_url == CONTROLS_URL
    assert CONTROLS_URL.endswith("#armed-and-maintenance")


def test_the_descriptions_use_this_installs_timings():
    from types import SimpleNamespace

    from custom_components.ups_sync.machine import Params
    from custom_components.ups_sync.switch import describe

    ctl = SimpleNamespace(
        params=Params(
            t_ob=180, t_grid=600, c_restore=85, t_wake=60, t_dead=1800,
            t_grace=1200,
        ),
        grid_entities=[],
    )
    armed, maint = describe(ctl, "armed"), describe(ctl, "maintenance")
    assert "after 3 min on UPS battery" in armed
    assert armed.count('"UPS battery charge" is at least 85 %') == 2  # restore, wake
    assert "Runtime with servers on" not in armed + describe(ctl, "maintenance")
    assert '"UPS battery charge" is at least 85 %' in maint
    # Reviewer cases: the cut is at the earlier deadline, and waking uses
    # the wake time, not the dead time.
    assert "20 min after the last one goes quiet" in armed
    assert "30 min after regardless, whichever comes first" in armed
    assert "fresh 1 min before it is powered on, and 30 min before" in maint
    assert "overdue for 6 h" in maint
    assert "grid" not in armed and "grid" not in maint
    assert "does not stop a shutdown already under way" in maint
    ctl.grid_entities = ["sensor.wall"]
    armed, maint = describe(ctl, "armed"), describe(ctl, "maintenance")
    assert "after 10 min of the grid entities showing the grid down" in armed
    assert "grid timer starts again" in maint
    # The power-cycle waits the longer of the dead and grace times.
    ctl.params = Params(t_dead=1800, t_grace=2400)
    assert "has been off 40 min on live outlets" in describe(ctl, "armed")
    assert "and 40 min before the group is power-cycled" in describe(ctl, "maintenance")
    # Equal times read as one sentence.
    ctl.params = Params(t_wake=600, t_dead=600, t_grace=120)
    assert "10 min before it is powered on or the group is power-cycled." in describe(
        ctl, "maintenance"
    )


async def test_the_description_is_not_kept_in_history(hass, rig):
    from custom_components.ups_sync.switch import FlagSwitch

    assert "description" in FlagSwitch._unrecorded_attributes


async def test_sensors_of_earlier_versions_are_removed(hass, rig):
    """ "Runtime with servers on" and "Peak load" no longer decide anything."""
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.ups_sync.const import DOMAIN
    from tests.test_integration import SERVERS, UPS

    entry = MockConfigEntry(
        domain=DOMAIN, data={**UPS, "servers": SERVERS}, options={"load_idle_max": 210}
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for key in ("reserve", "peak_load"):
        registry.async_get_or_create(
            "sensor", DOMAIN, f"{entry.entry_id}_{key}", config_entry=entry
        )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for key in ("reserve", "peak_load"):
        assert registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}") is None
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_battery_charge")
