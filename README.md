<img src="assets/icon.svg" width="64" height="64" alt="">

# ups-sync

A Home Assistant integration that keeps servers on an APC UPS in step with
a whole-home battery backup, such as an EcoFlow Delta Pro Ultra.

In a grid outage the home battery carries the house, and the UPS behind it
never notices: it still has input, so the servers keep running and drain
the battery that everything else depends on. This integration watches for
the grid going down, shuts the servers down cleanly, cuts their outlet
group once they are off, and brings them back when the grid has returned.
The network gear and anything else on the UPS's other outlets keeps
running throughout.

```
grid ──► home battery ──► APC UPS ──┬─► outlet group "Critical": network, Home Assistant   (stays on)
                                    └─► outlet group "Servers":  NAS, hypervisor           (shut down, cut, restored)
```

It also covers the case where the home battery runs out, or where there is
none: when the UPS itself goes on battery, the servers are shut down the
same way.

It talks to the APC Network Management Card over SNMP, to each server's
iDRAC over Redfish, and optionally to Proxmox over its API. The controller
is a three-state machine that was model-checked in TLA+ before it was
written; see [docs/STATE_MACHINE.md](docs/STATE_MACHINE.md) and
[formal/RESULTS.md](formal/RESULTS.md).

## What it does

| State | Meaning | Leaves when |
| --- | --- | --- |
| Idle | group on | grid down for 5 min, UPS on battery for 2 min, or low battery: shut the servers down |
| Shutting down | servers told to power off | all servers are off and the UPS load confirms it: cut the group |
| Power cut | group off | grid back and UPS input stable for 6 min, with enough charge to run every server for 26 min: turn the group on |

Two extra rules run while idle. If every server is off but the outlets are
live, the group is power-cycled so the servers boot. If one server is off
while another is up, that server is powered on through its iDRAC.

Power is only ever cut when no server answers, no iDRAC reports power on,
and the UPS output is at or below the calibrated idle load.

### Following the home battery

The UPS cannot tell grid power from home-battery power, and the integration
does not talk to the home battery. It learns that the grid is down from one
or more **grid entities** you choose in its options: things in Home
Assistant that only work while the grid is up, such as the voltage sensor
of a wall charger wired ahead of the home battery. Without any, only the
UPS going on battery starts a shutdown. When none of them shows life for five minutes, the servers are shut
down and their group is cut even though the UPS still has input, and the
group is not restored until one shows life again.

Pick entities that are reliably available. If all of them drop out for some
other reason, the servers are shut down cleanly and come back when one
returns; nothing is ever hard-cut on this signal. This trigger only works
once the controller is calibrated. An entity that no integration is
providing (renamed, deleted, or not loaded yet after a restart) says
nothing: it cannot start a shutdown and cannot allow a restore, and it
raises the problem sensor. So does a group that has been held off for six
hours without any sign of the grid. Turning on Maintenance
disables this trigger, for when you want the servers to keep running on the
home battery.

## Install

1. In HACS, add `https://github.com/err53/ups-sync` as a custom repository
   of type Integration, install **UPS sync**, and restart Home Assistant.
2. Add the integration under Settings, Devices & services. Give it the card
   address, the SNMP communities, the outlet group, and each server.
3. Follow [docs/runbook.md](docs/runbook.md) to calibrate, rehearse, and arm.

It installs **disarmed**. Until you turn on the Armed switch it only watches
and reports what it would do.

## Entities

| Entity | Purpose |
| --- | --- |
| `sensor.ups_sync_state` | idle, shutdown, or cut. Attributes hold every reading and the dry-run plan. |
| `binary_sensor.ups_sync_problem` | on when the UPS cannot be read, a shutdown is overdue, a cut or restore command failed, the servers are off and cannot be power-cycled, or calibration is incomplete |
| `switch.ups_sync_armed` | off: observe only. on: act. |
| `switch.ups_sync_maintenance` | on: never power-cycle, wake, or restore. Outage shutdown still works. |
| UPS load, UPS runtime at present load, Runtime with servers on, Peak load | the numbers the controller works from |
| UPS input, On battery, Low battery, Outlet group, Grid | what the UPS and the grid entities report. Grid exists only when grid entities are set, and is unknown when none of them says anything. |
| `<server>` reachable, `<server>` power | per server: answers on the network, chassis powered (from the iDRAC) |
| Calibrated | on once the peak load is learned and the idle load is set |
| Dry run would do, Last action, Last error | what it would do while disarmed, and what it last did |

Sensors that come from a UPS reading turn unavailable as soon as a poll of
the UPS fails, and come back with the next good one.

Every action also raises a persistent notification, fires a
`ups_sync_action` event, and calls the notify services set in the options.

## Requirements on the UPS and servers

The controller is one of four layers. The others work with Home Assistant
down and are part of what was verified:

- Each server runs its own NUT client pointed at the card
  ([nut/install-backstop.sh](nut/install-backstop.sh)).
- The card sheds the group below 3 min of runtime, never on a timer, and
  reports low battery at 16 min ([scripts/cutover.sh](scripts/cutover.sh)).
- The card's minimum return runtime for the group is 60 min, its maximum.
- Each server's BIOS powers on when AC returns.

## Development

```
uv venv --python 3.14 && uv pip install -r requirements-test.txt
.venv/bin/python -m pytest
```

`tests/test_sim.py` drives the real controller logic through thousands of
random outages and fails if a server ever loses power while running.
`tests/test_live_group.py` runs against real hardware on an empty outlet
group when `UPS_SYNC_LIVE_HOST` is set.
