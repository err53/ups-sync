<img src="assets/icon.svg" width="64" height="64" alt="">

# ups-sync

A Home Assistant integration that shuts servers down cleanly when an APC UPS
loses input, cuts their outlet group once they are off, and brings them back
when power is stable. Everything else on the UPS keeps running.

It talks to the APC Network Management Card over SNMP, to each server's
iDRAC over Redfish, and optionally to Proxmox over its API. The controller
is a three-state machine that was model-checked in TLA+ before it was
written; see [docs/STATE_MACHINE.md](docs/STATE_MACHINE.md) and
[formal/RESULTS.md](formal/RESULTS.md).

## What it does

| State | Meaning | Leaves when |
| --- | --- | --- |
| Idle | group on | on battery for 2 min, or low battery: shut the servers down |
| Shutting down | servers told to power off | all servers are off and the UPS load confirms it: cut the group |
| Power cut | group off | input stable for 6 min and enough charge to run every server for 26 min: turn the group on |

Two extra rules run while idle. If every server is off but the outlets are
live, the group is power-cycled so the servers boot. If one server is off
while another is up, that server is powered on through its iDRAC.

Power is only ever cut when no server answers, no iDRAC reports power on,
and the UPS output is at or below the calibrated idle load.

### With a battery upstream of the UPS

If the UPS is fed through a home battery, an outage does not put the UPS on
battery until the home battery is empty, so by default the servers run it
down. To shut them down early and keep that battery for everything else,
give the integration one or more **grid entities** in its options: things
that only work while the grid is up, such as a wall charger's voltage
sensor. When none of them shows life for five minutes, the servers are shut
down and their group is cut even though the UPS still has input, and the
group is not restored until one shows life again.

Pick entities that are reliably available. If all of them drop out for some
other reason, the servers are shut down cleanly and come back when one
returns; nothing is ever hard-cut on this signal. This trigger only works
once the controller is calibrated. An entity that Home Assistant no longer
knows is ignored and raises the problem sensor, and so does a group that
has been held off by the grid entities for six hours. Turning on Maintenance
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
