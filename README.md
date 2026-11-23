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
written; see the state diagram in
[docs/STATE_MACHINE.md](docs/STATE_MACHINE.md#2-controller-states) and
[formal/RESULTS.md](formal/RESULTS.md).

## What it does

| State | Meaning | Leaves when |
| --- | --- | --- |
| Idle | group on | grid down for 5 min, UPS on battery for 2 min, or low battery: shut the servers down |
| Shutting down | servers told to power off | all servers are off and the UPS load confirms it: cut the group |
| Power cut | group off | grid back and UPS input stable for 6 min, with the UPS battery charged to 80 %: turn the group on |

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
of a wall charger wired ahead of the home battery. Without any, a shutdown
starts only after two minutes on UPS battery, or at once on low battery.
When none of them shows life for five minutes, the servers are shut down
and their group is cut even though the UPS still has input, and the group
is not restored until one shows life again.

Pick entities that are reliably available. If all of them drop out for some
other reason, the servers are shut down cleanly and come back when one
returns; nothing is ever hard-cut on this signal. This trigger only works
once the idle load is set. An entity that no integration is
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

## Armed and Maintenance

The two switches on the device page. Home Assistant has no description
field, so the device's "Visit" link opens this section, and each switch
carries the same explanation under Attributes when you tap it, written with
your own timings. Below are the defaults; every time here is an option
except the 6 hours for an overdue restore.

**Armed**

- **On:** it sends commands.
  - Shuts the servers down after 2 min on UPS battery, at once on low
    battery, or, with grid entities set and the idle load set, after 5 min
    of them showing the grid down.
  - Cuts their outlet group once the UPS load confirms they are off: 2 min
    after the last one goes quiet if every shutdown was accepted, or 10 min
    after regardless, whichever comes first.
  - Turns the group back on after 6 min of UPS input (with a grid entity
    showing the grid up, if any are set) once "UPS battery charge" is at
    least 80 %. The same rule restores a group you switch off by hand.
    Until a grid entity shows the grid up (the grid is still down, or the
    entities are missing, which `grid_entities_missing` lists),
    `waiting_for_grid` on the state sensor is on.
  - Powers on a server through its iDRAC if it has been off for 10 min while
    another runs, if the UPS has input (and a grid entity shows the grid
    up, if any are set) and "UPS battery charge" is at least 80 %.
  - If every server has been off 10 min on live outlets, cuts the group
    (once the idle load is set) so their BIOS powers them on when it is restored by
    the rule above. Turn on Maintenance before shutting them down by hand.
- **Off:** no commands are sent; it still reads the UPS and the iDRACs.
  "Dry run would do" lists what it would do. Turning Armed off does not
  recall a shutdown a server has already accepted.

**Maintenance**

- **On:** for working on the servers by hand.
  - Nothing is powered on: no restore, no wake, no power-cycle.
  - With grid entities set, a grid outage they see does not start a
    shutdown, so the servers can run on the home battery.
  - 2 min on UPS battery or low battery still shuts them down and cuts the
    group. The group then stays off until you switch it on at the UPS card
    or turn Maintenance off; an iDRAC power-on does nothing while the
    outlets are off.
  - **It does not stop a shutdown already under way.** Only turning Armed
    off stops further commands.
  - Servers that cannot be revived and a restore overdue for 6 hours are
    not reported as problems.
- **Off:** normal operation. Turn it off when you are done.
  - A group cut while Maintenance was on is turned back on by the restore
    rule, possibly at once.
  - A server you left off gets a fresh 10 min (the wake time) before it is
    powered on, and a fresh 10 min (the dead time) before the group is
    power-cycled.
  - With grid entities set, during a grid outage the 5 min grid timer
    starts again, then (once the idle load is set) the servers are shut down.

Maintenance changes what is done, or would be done in a dry run, and what
counts as a problem. Only Armed decides whether anything is sent.

## Entities

| Entity | Purpose |
| --- | --- |
| `sensor.ups_sync_state` | idle, shutdown, or cut. Attributes hold every reading and the dry-run plan. |
| `binary_sensor.ups_sync_problem` | on when the UPS cannot be read, a shutdown is overdue, a cut or restore command failed, the servers are off and cannot be power-cycled, or the idle load is not set |
| `switch.ups_sync_armed` | off: observe only. on: act. See [Armed and Maintenance](#armed-and-maintenance). |
| `switch.ups_sync_maintenance` | on: never power-cycle, wake, or restore. Shutdown on UPS battery still works. See [Armed and Maintenance](#armed-and-maintenance). |
| UPS load, UPS battery charge | the numbers the controller works from: the load for the cut guard, the charge for every restore and wake |
| UPS runtime at present load | the card's own estimate. On battery it sets the low-battery flag (below the card's low-battery duration), which shuts the servers down at once; restores and wakes do not use it |
| UPS input, On battery, Low battery, Outlet group, Grid | what the UPS and the grid entities report. Grid exists only when grid entities are set, and is unknown when none of them says anything. |
| `<server>` reachable, `<server>` power | per server: answers on the network, chassis powered (from the iDRAC) |
| Calibrated | on once the idle load is set |
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
- The card reports its battery charge (PowerNet `upsAdvBatteryCapacity`).
  Every restore and wake goes by it, and from version 0.6.0 a card that
  does not answer it is reported as unreadable, so nothing is shut down
  by the integration. Check the "UPS battery charge" sensor shows a figure
  before upgrading.

## Development

```
uv venv --python 3.14
uv pip install -r requirements-test.txt -r requirements-lint.txt
git config core.hooksPath .githooks   # run the checks before each commit
.venv/bin/python -m pytest
scripts/check.sh                      # ruff, ruff format, pyright; --fix to apply fixes
```

CI runs the same `scripts/check.sh`. Pyright checks the integration only; the
tests use duck-typed fakes on purpose. `.claude/settings.json` stops coding
agents from skipping the commit hook with `--no-verify`.

`tests/test_sim.py` drives the real controller logic through thousands of
random outages and fails if a server ever loses power while running.
`tests/test_live_group.py` runs against real hardware on an empty outlet
group when `UPS_SYNC_LIVE_HOST` is set.
