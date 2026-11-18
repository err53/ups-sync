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
    assert state(hass, "sensor.ups_sync_runtime_with_servers_on") == "57.0"
    assert state(hass, "sensor.ups_sync_peak_load") == "400.0"
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
        "sensor.ups_sync_runtime_with_servers_on",
        "binary_sensor.ups_sync_ups_input",
        "binary_sensor.ups_sync_outlet_group",
        "binary_sensor.ups_sync_s_power",
    ):
        assert state(hass, entity_id) == "unavailable"
    # What the controller itself knows stays readable.
    assert state(hass, "sensor.ups_sync_peak_load") == "400.0"
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
