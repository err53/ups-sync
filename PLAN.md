# ups-sync: EcoFlow Delta Pro Ultra + APC Smart-UPS X 1500 outage plan

> **Status 2026-11-17.** This file is the project history. The design is
> `docs/STATE_MACHINE.md`, the implementation is the Home Assistant
> integration in `custom_components/ups_sync`, and the steps left to do are
> in `docs/runbook.md`. Where this file disagrees with those, they win. In
> particular the "HA package" of Phase 2 became a HACS integration, SSH
> poweroff became the Proxmox API plus iDRAC, and the controller reads the
> card over SNMP itself instead of going through the NUT add-on.
>
> Done so far: iDRAC `ups-sync` Operator users on both servers; Proxmox
> role `UpsSync` (power management on the node only) with an API token; NUT
> backstop installed on sakuya. Not done: everything in the runbook from
> step 1, including the card and TrueNAS cutover.

Goal: on a power outage, gracefully shut down the storage and virtualization
servers (APC outlet group 2 "Servers") and then cut their power, while the
network / Raspberry Pi stack (APC outlet group 1 "Critical") keeps running for
as long as possible. Bring the servers back automatically once power is stable.

## Phase 0 results (2026-11-08, complete except two user checks)

### Power topology

```
grid -> Delta Pro Ultra (DPU) -> transfer switch -> APC Smart-UPS X 1500
                                   (grid | DPU)        |- group 1 Critical: network, Pi stack, HA Yellow (to confirm)
                                                       |- group 2 Servers:  flandre (TrueNAS), sakuya (Proxmox)
                                                       |- group 3 Lab:      empty, used for tests
```

- The transfer switch is manual. Position is unknown to software. The plan
  must work in both positions.
- DPU is in `time_of_use` mode, charge limit 90 %, discharge limit 10 %,
  backup reserve 50 %. It has been charging from grid continuously over the
  last 9 days (C20 AC-in power never dropped to 0), so "AC-in power = 0" is
  not usable as a grid-present signal.

### Devices and how they reach Home Assistant

| Device | HA integration | Notes |
| --- | --- | --- |
| APC NMC2 (AP9630, AOS 7.0.4) at 192.168.1.4 | **NUT add-on on HA** (`snmp-ups`, apcc MIB, poll 2 s) -> NUT integration `a0d7b954-nut:3493` | Already working. Entities `sensor.ups_status_data` (OL/OB...), `sensor.ups_battery_runtime` (min), `sensor.ups_battery_charge`, `sensor.ups_load`. A stale second NUT entry (192.168.1.63:3493) is in `setup_retry` and should be removed. |
| DPU | `ef_ble` (Bluetooth, gnox/ha-ef-ble fork) | Entities: `sensor.ef_<serial>_battery_level`, `_input_power`, `_output_power`, `_ac_c20_in_power`, `_ac_l14_out_power`, etc. No grid/AC-in-connected entity exists for this model. History shows ~200 `unavailable` blips on 2026-10-31 alone. **Treat as advisory only.** |
| Tesla Wall Connector (garage) | `tesla_wall_connector` | `sensor.tesla_wall_connector_grid_voltage` 232-247 V over 9 days, no gaps. **Best independent grid-present signal** (assumes the charger is on the main panel, not the DPU). |
| HA host | HAOS 2026.11.0 on Home Assistant Yellow, 192.168.1.71 (its LAN hostname) | Supervisor API returns 401 for the provided token; add-on config must be edited in the UI. |

### NUT server (HA add-on) capabilities

`upsd` on 192.168.1.71:3493 exposes the full APC status set. Instant
commands available: `load.off`, `load.on`, `shutdown.return`,
`shutdown.stayoff`, bypass/calibrate/test. **No outlet-group commands**, as
expected from NUT's `apc-mib.c`. Outlet-group control stays on SNMP.

### Outlet-group control (verified on hardware, group 3)

- Command OID `1.3.6.1.4.1.318.1.1.1.12.3.2.1.3.<g>`: `1` = immediate on,
  `2` = immediate off, (PowerNet also defines delayed off / reboot values).
- State OID `1.3.6.1.4.1.318.1.1.1.12.1.2.1.3.<g>`: `1` = on, `2` = off.
- SNMPv2c, read community `public`, write `private`, from the LAN.

### Current NMC2 outlet-group settings (from the web UI)

| Group | Power off delay | Power on delay | Min return runtime | Load shedding |
| --- | --- | --- | --- | --- |
| 1 Critical | 90 s | 0 | 600 s | none |
| 2 Servers | 90 s | 0 | 1800 s | **on battery > 60 s -> off** (then 90 s delay = ~150 s total) |
| 3 Lab | 90 s | 0 | 1800 s | on battery > 60 s, runtime < 1800 s, overload; skip off delay |

### Current TrueNAS (flandre) UPS service

NUT **slave** to the HA host on port 3493, user `upsmon`, shutdown mode
**"UPS goes on battery"**, shutdown timer **60 s**, remote monitor on.

### Problems with the current configuration

1. **Race:** NMC2 cuts group 2 about 150 s after the APC goes on battery.
   TrueNAS only *starts* shutting down at 60 s. A ZFS export with VMs/apps
   can take longer than 90 s, so the NAS can be hard-cut mid-shutdown.
2. **Blips trigger full shutdowns.** A 61-second brownout today takes down
   both servers and leaves them off for 30 minutes (min return runtime).
3. **The DPU buffer is wasted.** If the transfer switch is on DPU
   pass-through, the APC stays `OL` until the DPU hits its 10 % discharge
   limit, then the servers get ~1 minute of notice. If the switch is on
   grid, the DPU's 6 kWh is never used by the servers at all.
4. **sakuya (Proxmox)** has no known NUT client; it is shut down only by
   losing power.

### Findings 2026-11-17 (read-only probes, nothing changed)

- iDRAC: sakuya 192.168.1.10, flandre 192.168.1.12. Both answer Redfish
  (`/redfish/v1`, ServiceRoot 1.3.0) over HTTPS, so HA can use built-in
  `rest` / `rest_command` instead of `ipmitool`. Needs a dedicated
  Operator-only iDRAC user on each.
- sakuya (192.168.1.11) drops ICMP but answers TCP 22 and 8006. Its
  reachability check must be a TCP probe, not ping. flandre answers ping.
- NMC load-shed settings are readable over SNMP in table
  `1.3.6.1.4.1.318.1.1.1.12.2.2.1`: col 6 min return runtime, col 10
  time-on-battery enable (1 off, 2 on), col 11 runtime-remaining enable,
  col 13 / 14 the two thresholds in seconds. Group 2 today: col 10 = 2
  (60 s), col 11 = 1, col 14 = 1200. Low-battery duration is
  `1.3.6.1.4.1.318.1.1.1.5.2.8.0`, currently 120 s.
- Cutover ordering: the NMC shed change and the HA controller must go live
  together. Shed changed alone leaves TrueNAS off on a live outlet after
  any outage over 60 s; controller deployed alone gets hard-cut by the
  150 s shed.
- The stored HA API token file is gone; a new one is needed to continue.

### Still needs Jason

- Confirm **HA Yellow and the Bluetooth proxy are on group 1** (Critical).
  If HA is on group 2 the whole orchestration dies with the servers.
- Confirm **BIOS "power on after AC loss" = On** on flandre and sakuya.
- Confirm the Tesla Wall Connector is fed from the main panel (grid), not
  from the DPU.

## Stack (all off-the-shelf)

| Role | Tool | Status |
| --- | --- | --- |
| UPS telemetry | NUT add-on (`snmp-ups`) + NUT integration | done |
| Graceful OS shutdown | NUT clients on flandre (built-in) and sakuya (`nut-client`) | flandre done, sakuya to do |
| Outlet group on/off | HA built-in `snmp` switch platform | to do |
| Grid-present signal | Tesla Wall Connector grid voltage + APC OB status | to do (template binary sensor) |
| DPU state | `ef_ble` battery level, advisory | exists |
| Orchestration | HA automations + helpers | to do |
| Hardware backstop | NMC2 load-shed + BIOS restore-on-AC | retune |

No custom CLI needed. Fallback if the HA `snmp` switch misbehaves: a
`shell_command` wrapping `snmpset`.

## Target behaviour

**Superseded by `docs/STATE_MACHINE.md`** (the verified controller) and
`formal/RESULTS.md` (TLC results). The summary below is kept for history;
where it differs from the state machine document, that document wins.
Key differences: cut decisions are guarded by UPS output load; the
recovery power-cycle does not require input present and its timer is
persisted; a partially-down server is powered on via its iDRAC; the shutdown
timeout is an alarm, never a cut; NMC low-battery duration is 16 min and
load shed is "runtime < 6 min"; restore needs ≥ 26 min runtime.

Definitions (HA template binary sensors):

- `grid_present` = Tesla grid voltage > 180 V **and** APC status does not
  contain `OB`. Debounced 60 s both ways.
- `on_battery` = APC status contains `OB` (APC input lost: either grid is
  out with the switch on grid, or the DPU has quit).
- `dpu_low` = DPU battery level < 25 % **and** entity available.

Shutdown is triggered when **any** of these holds for 60 s:

| Condition | Why |
| --- | --- |
| `on_battery` and APC runtime < 20 min | hand-off nearly over |
| `on_battery` for > 10 min | switch on grid, no DPU buffer, don't drain for nothing |
| `grid_present` off and `dpu_low` | switch on DPU, buffer nearly gone, APC still OL |
| APC status contains `LB` | hard floor |

Not a trigger: a short blip, or grid out while the DPU still has > 25 %.
In that case the servers keep running on the DPU, and HA sends a
notification only.

Shutdown sequence (HA script, `mode: single`):

1. Notify; set `input_boolean.ups_sync_shutdown_in_progress`.
2. Call the per-server `shell_command` (SSH `poweroff` on sakuya and
   flandre). This is independent of NUT so it also works when the APC is
   still `OL` (DPU-side trigger).
3. Wait until both hosts stop answering ping, max 8 min.
4. `switch.turn_off` on the group 2 SNMP switch.

Restore sequence:

1. `grid_present` on for 5 min (timer) and APC runtime > 15 min.
2. `switch.turn_on` group 2. Servers boot via BIOS.
3. Clear flag, notify.

Backstops that work with HA dead:

- NMC2 group 2 load shed: change to **"UPS runtime remaining < 600 s"**
  (not "power failure > 60 s"), keep 90 s off delay, keep 1800 s min return
  runtime. This gives NUT clients 10 min of notice and never fires on blips.
- TrueNAS UPS service: shutdown mode **"UPS reaches low battery"**; NUT's
  `battery.runtime.low` on this UPS is 120 s, so raise the NMC's low-battery
  threshold (UPS -> Configuration -> Shutdown -> Low battery duration) to
  **8-10 min** so `LB` fires before the load shed.
- sakuya: `nut-client` secondary to `ups@192.168.1.71`, shuts down on `LB`.
- HA add-on itself: keep `SHUTDOWNCMD` disabled so HA rides group 1 down.

## Phases

### Phase 1: Hardware and NUT (no HA changes yet)

- NMC2: set group 2 load shed to runtime < 600 s; group 1 unchanged; group 3
  left as is for tests. Raise low-battery duration to 8-10 min.
- TrueNAS: shutdown mode -> low battery.
- sakuya: install `nut-client`, `upsmon.conf` MONITOR line, `upssched` not
  needed. Verify with `upsc ups@192.168.1.71`.
- Remove the stale NUT integration entry (192.168.1.63).

### Phase 2: HA package `ups_sync`

`/config/packages/ups_sync.yaml` (enable `packages:` in `configuration.yaml`
if not already):

- `switch: platform: snmp` for groups 2 and 3 (group 3 for testing).
- `template: binary_sensor` for `grid_present`, `on_battery`, `dpu_low`.
- `shell_command` x2 for SSH poweroff, with a dedicated key restricted via
  `command=` in each server's `authorized_keys` and a non-root sudoers rule
  for `poweroff` only.
- iDRAC per server (`ipmitool -I lanplus -H <idrac> ... chassis power
  status|on|soft`): `command_line` sensor for chassis power state, and
  `shell_command`s for power on (wake rule) and soft off (second shutdown
  channel). Use a dedicated iDRAC user with Operator privilege only.
- `input_boolean`, `timer`, `input_number` helpers.
- `script.ups_sync_shutdown`, `script.ups_sync_restore`.
- `automation` x3: trigger shutdown, trigger restore, notify-only on grid
  loss while DPU still healthy.

### Phase 3: Tests

1. HA switch on group 3: on/off round trip from the UI.
2. Script dry run: temporarily point the switch at group 3 and the shell
   commands at `true`; force the trigger via Developer Tools.
3. Real test, servers idle: pull APC input with switch on grid. Expect:
   notify at 60 s, shutdown at 10 min on battery, group 2 off after hosts
   go dark, restore 5 min after input returns.
4. HA-dead test: stop HA, pull APC input, confirm NUT `LB` and NMC load shed
   still shut down and cut the servers.

## Security notes

- SNMPv2c communities are plaintext. Restrict the NMC's SNMP access control
  to 192.168.1.71 (HA) and the admin workstation, or move to SNMPv3 after
  the flow is proven.
- Communities and the SSH key live in HA `secrets.yaml` / `/config/.ssh`,
  not in this repo.

## Repo layout (to be filled in)

```
ha/packages/ups_sync.yaml   switches, sensors, helpers, scripts, automations
nut/sakuya/                 upsmon.conf template for Proxmox
docs/runbook.md             what to do during an outage, how to test
```
