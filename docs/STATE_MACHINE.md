# ups-sync controller: state machine and formal verification

This document is the authoritative description of the outage controller. The
TLA+ models in `formal/` are the executable version of it; if they disagree,
the models win and this document is wrong.

## 1. Actors

| Actor | Where it runs | What it can observe | What it can do |
| --- | --- | --- | --- |
| **Controller** (`custom_components/ups_sync`) | Home Assistant Yellow, outlet group 1 | APC output status, runtime, output watts, low-battery threshold and group 2 state, all read from the card over SNMP; a TCP probe of each server (ping is not used: sakuya drops ICMP); iDRAC chassis power state per server; success/failure of each shutdown command | OS shutdown per server (Proxmox API for sakuya, iDRAC graceful shutdown for both); iDRAC power-on per server; SNMP immediate-off / immediate-on on group 2; notifications |
| **Server backstop** (NUT client on flandre and sakuya) | each server | APC status via its own `snmp-ups` driver pointed at the NMC (not via HA) | shut itself down |
| **NMC load shed** (APC card firmware) | the UPS | on-battery time, runtime remaining | turn group 2 off; turn it back on after min return runtime |
| **BIOS** | each server | AC present on its PSU | power on when AC returns |
| **Environment** | — | — | grid up/down at any time; transfer switch GRID/DPU at any time; DPU at any state of charge; HA crash/reboot at any time; server shutdown 2–8 min, boot 2–6 min; ping dying up to 2 min before power-off; human power-off/power-on while input is present |

The controller never looks at the Delta Pro Ultra. Its Bluetooth link goes
through a proxy that is itself on grid power, so DPU data is unavailable
exactly when it would matter. The DPU is treated purely as an upstream
buffer whose only visible effect is *when* the APC loses input.

## 2. Controller states

```
            ┌──────────────────────────────────────────────────────┐
            │                                                      │
            ▼            on battery ≥ T_OB, or LB                   │
        ┌───────┐ ─────────────────────────────────► ┌──────────┐   │
        │ IDLE  │                                    │ SHUTDOWN │   │
        │ g2=on │ ◄──── reconcile: g2 observed on ── │  g2=on   │   │
        └───────┘                                    └──────────┘   │
            │  ▲                                          │         │
            │  │ restore:                                 │ cut:    │
 recover:   │  │ input stable T_HOLD                      │ all dark, load low,
 all dark,  │  │ and runtime ≥ E_RESTORE                  │ (all acked and dark ≥ T_GRACE)
 load low,  │  │                                          │   or dark ≥ T_DEAD
 T_DEAD     ▼  │                                          ▼
          ┌────────┐ ◄────── reconcile: g2 observed off ─────────────┘
          │  CUT   │
          │ g2=off │
          └────────┘
```

**Reconciliation rule.** The controller state is anchored to the observable
group 2 state. Group 2 off while in IDLE or SHUTDOWN forces CUT. Group 2 on
while in CUT forces IDLE. On every HA start the state is derived this way;
no persisted flag is trusted. This is what makes HA crashes and the card's
own actions (load shed, auto-return) harmless.

### IDLE (group 2 on)

- Each tick: `obT` = consecutive ticks with APC on battery, else 0.
- **→ SHUTDOWN** when `obT ≥ T_OB` or NUT reports LB.
- **→ CUT (recover)** when group 2 is powered, no server answers ping, UPS
  load is at the "servers off" baseline, and this has held for `T_DEAD`.
  Input presence is deliberately **not** required: dead outlets should go
  dark whether or not the grid is back. This power-cycles servers that were
  shut down (by the backstop, by a crashed HA mid-sequence, by hand) but
  never had their power cut, so BIOS can bring them back on restore, and it
  removes live-but-unused outlets that a human could power a server from
  while the UPS is low. The load guard means a server that is merely
  booting or has its network down is never power-cycled. The `T_DEAD` timer
  is a persisted "last time any server answered" timestamp, so it survives
  HA restarts.
- **Wake (iDRAC power-on)** when some server is unreachable while another is
  up, group 2 is powered, input is present, runtime ≥ `E_RESTORE`, and this
  has held for `T_WOL`. This covers the leftover of an interrupted shutdown
  (HA crashed after powering off one server). Cycling the group would
  hard-cut the running server, so the down one is powered on individually
  through its iDRAC (Redfish `Reset` with `On`). Both servers have iDRAC, so
  Wake-on-LAN is not used. The iDRAC must report chassis power **off**
  before the command is sent; a server that is merely booting is left alone.
  In the TLA+ models this action is still named `DoWol`; the model only
  cares that it powers on exactly the unreachable server.

- Notification only, no action: grid sensor (Tesla Wall Connector voltage)
  says grid is down while APC is still online. That means the switch is on
  DPU pass-through and the DPU is carrying the load.

### SHUTDOWN (group 2 on)

- Every 60 s: a shutdown command to every server that still answers; record
  per server whether a channel accepted it (`acked`). Channels are tried in
  order: the Proxmox API where a token is configured, then iDRAC graceful
  shutdown (ACPI power button). Home Assistant holds no shell keys.
- `darkT` = consecutive ticks with group 2 powered and no server pingable.
- **→ CUT** when no server pings, UPS load is at baseline, no iDRAC reports
  chassis power on, and either every server acked and `darkT ≥ T_GRACE`, or
  `darkT ≥ T_DEAD` (servers went away without our command, e.g. backstop or
  manual shutdown). The iDRAC check is an extra guard on top of the load
  guard, not a replacement: it can only delay a cut, never permit one, so
  it cannot weaken `NoHardCut`. If an iDRAC is unreachable it is treated as
  "unknown" and the load guard alone decides, as in the model.
- **Alarm** when `waitT ≥ T_WAIT`: notify loudly, keep waiting. There is
  deliberately no timeout cut. Every cut goes through the load guard; a cut
  that ignores it is a hard cut by construction. The model proves the alarm
  never fires unless a human keeps powering servers back on mid-sequence.
- Input returning during SHUTDOWN does **not** abort. A half-finished
  shutdown is worse than a finished one followed by a restore.

### CUT (group 2 off by us)

- `holdT` = consecutive ticks with APC input present.
- **→ IDLE (restore)** when `holdT ≥ T_HOLD` and runtime ≥ `E_RESTORE`:
  SNMP immediate-on on group 2. Servers boot via BIOS.

## 3. Backstops (work with HA dead)

1. **Server NUT clients** monitor the NMC directly over SNMP. Shut down on
   LB, or after `T_SRV_OB` on battery.
2. **NMC load shed** on group 2: "UPS runtime remaining < E_SHED". Never
   "power failure longer than N seconds".
3. **NMC min return runtime** on group 2 keeps servers off after a full UPS
   death until runtime ≥ `E_RETURN`.
4. **BIOS restore on AC** on both servers.

## 4. Properties checked

| Property | Kind | Meaning |
| --- | --- | --- |
| `NoHardCut` | safety | no server ever loses power while RUN, SHUT or BOOT |
| `NoTimeout` | safety | the `T_WAIT` alarm never fires (checked without human interference) |
| `NoSpurious` | safety | SHUTDOWN is entered only on battery or LB |
| `TypeOK` | safety | all counters stay in range |
| `EventuallyRunning` | liveness | if grid and HA eventually stay up (and, in the two-server model, humans stop acting), every server not deliberately powered off by a human is eventually running and stays running |

## 5. Verification results

See `formal/RESULTS.md` (generated from the TLC runs) for the parameter
table, pass/fail per variant, and counterexample traces.

## 6. Assumptions the proof depends on

- Shutdown completes within `SMAX` and boot within `BMAX` once started.
- The UPS runtime estimate is roughly honest (we take 57 min at today's
  load and use it as the full-charge budget).
- Nobody presses power buttons or runs `poweroff` by hand while the UPS is on
  battery, and nobody powers a server **on** while the UPS has less than
  `E_RESTORE` (26 min) runtime. The model found that a human powering a
  server on from a live outlet with ~11 min of runtime left, during a
  flapping grid, ends in a hard cut that no controller can prevent. Let HA
  bring the servers back; it waits for the runtime margin.
- SNMP and NUT reads from HA succeed within a tick (2 min). The network
  (group 1) stays up as long as the UPS has output.
- UPS load is a faithful "something is drawing power on group 2" signal.
  Calibrate: baseline = group 1 load with both servers soft-off but still
  plugged in, so the iDRACs' standby draw (roughly 10-20 W each) is part of
  the baseline. Guard threshold = baseline + 40 W.
- A manual power-off for maintenance must be preceded by switching the
  `ups_sync_maintenance` boolean on, which disables the recover and wake
  rules; otherwise HA will wake or power-cycle the box within 10 minutes.
- Both iDRACs are reachable from the HA host over the group-1-powered
  network whenever their outlet has power (dedicated iDRAC NIC or shared
  LOM; both work with the host off). iDRAC credentials live in HA
  `secrets.yaml`.
- The APC's own "min return runtime" and "load shed" settings are set as
  specified in the config table and not changed by hand.

## 7. Mapping model units to production

| Model | Production |
| --- | --- |
| 1 tick | 2 min |
| `e` = 1 | 1 min of full-load runtime (57 at full charge); drains 2/tick with servers on, 1/tick with only group 1 |
| `T_OB` = 1 | shutdown starts 2 min after going on battery |
| `T_GRACE` = 1 | wait 2 min after the last ping before cutting |
| `T_DEAD` = 5 | 10 min dark (persisted) before cutting a group whose servers are all off |
| `T_WOL` = 5 | 10 min with one server down and one up before iDRAC power-on of the down one |
| `T_HOLD` = 3 | 6 min stable input before restore |
| `E_RESTORE` = 26 | restore only with ≥ 26 min runtime |
| `E_LB` = 16 | UPS low-battery threshold 16 min (NMC "Low battery duration") |
| `E_SHED` = 6 | NMC load shed group 2 when runtime < 6 min |
| `E_RETURN` = 30 | NMC min return runtime 30 min (already set) |
| `T_SRV_OB` = 5 | server NUT client shuts down after 10 min on battery |

## 8. Where the code differs from the model

The code is `custom_components/ups_sync/machine.py`. Its rules carry the
model's operator names in comments. The differences below are deliberate.
Each one either only removes or delays actions, or is equivalent to a step
the model already allows.

| Difference | Why it is safe |
| --- | --- |
| Real seconds and 15 s polling instead of 2-minute ticks. A timer counts how long a condition has held without interruption. | A refinement: the model's "held for N ticks" becomes "held for N x 120 s". |
| The state only becomes CUT or returns to IDLE when the group is *observed* off or on. Cut and restore emit a command and wait. | A failed SNMP write is retried on the next poll instead of being assumed. |
| The cut guard is evaluated again on fresh readings immediately before the SNMP write. | Only removes cuts. |
| If the UPS cannot be read, or reports bypass, self-test or off, the step is skipped: no decision, timers hold. The problem sensor turns on after 2 min. | Only removes actions. The server backstops do not depend on Home Assistant. |
| Abort: in SHUTDOWN, if input has been stable for `T_HOLD` and either no server accepted a command or the alarm has fired, return to IDLE. The model has no failing commands, so it never needs this. | Returning to IDLE is exactly what an HA restart does, and the model lets HA restart at any step. |
| The group stays off for at least 60 s before a restore. | Only delays restore. Lets the power supplies drain so the BIOS sees a real AC loss. |
| Maintenance also blocks restore, not only recover and wake. | Only removes actions. |
| Disarmed (the default after install): readings and timers run, decisions are computed on a copy and published as `would_act`, nothing is sent. | No actions. |
| The load guard refuses every cut until the idle load is configured. | Only removes cuts. |
| The "grid down but APC online" notification is not part of the integration. | Notification only. A plain automation on the wall connector's voltage does it. |

`tests/test_sim.py` is a Python port of the model's environment that drives
this code through random outages, including failing commands and humans,
and checks `NoHardCut`, `NoSpurious`, the alarm, and recovery on each run.
