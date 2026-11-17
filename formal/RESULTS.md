# TLC verification results

> **Revised 2026-11-17 after code review.** The card settings changed: load
> shed is now "runtime < 3 min" (was 6) and the card's return threshold is
> modelled as 17 (was 30). The section "Revision: runtime is measured at the
> present load" at the end explains why and lists the re-runs. The tables in
> between record the earlier runs with `E_SHED = 6`, `E_RETURN = 30`.

Tool: TLA+ `tla2tools.jar` (TLC 2.19), exhaustive breadth-first state
exploration, 10 workers. Reproduce with:

```
cd formal
java -cp tools/tla2tools.jar tlc2.TLC -workers auto -config MC_Baseline.cfg UpsSync.tla
java -cp tools/tla2tools.jar tlc2.TLC -workers auto -config MC2_Baseline.cfg UpsSync2.tla
./tools/variants.sh /tmp/variants      # parameter sweep, single-server model
```

## Models

| Model | What it adds | States (baseline) |
| --- | --- | --- |
| `UpsSync.tla` | one lumped server, adversarial grid / switch / DPU / HA crashes / timings / NMC auto-return | 436,250 distinct |
| `UpsSync2.tla` | two independent servers (flandre, sakuya), per-server ack and ping, humans powering servers off/on while input is present | see below |

Units: 1 tick = 2 min. `e` = APC energy in minutes of full-load runtime
(full = 57 at today's 417 W load). Servers on: 2/tick. Only group 1: 1/tick
(real group-1 draw is lower, so this is conservative).

## Baseline parameters (proposed production config)

| Constant | Value | Production meaning |
| --- | --- | --- |
| SMAX | 4 | servers finish `poweroff` within 8 min |
| BMAX | 3 | servers finish booting within 6 min |
| NET_TAIL | 1 | ping may die up to 2 min before power-off |
| T_OB | 1 | start shutdown 2 min after going on battery |
| T_GRACE | 1 | after ack and last ping, wait 2 min, check UPS load, cut |
| T_DEAD | 5 | servers dark without our ack: wait 10 min, check UPS load, cut |
| T_WAIT | 12 | alarm (notification only) if SHUTDOWN exceeds 24 min |
| T_HOLD | 3 | input stable 6 min before restore |
| E_RESTORE | 26 | restore only with ≥ 26 min runtime |
| E_LB | 16 | NMC "low battery duration" = 16 min |
| E_SHED | 6 | NMC load shed group 2 when runtime < 6 min |
| E_RETURN | 30 | NMC min return runtime 30 min (already set on the card) |
| T_SRV_OB | 5 | server NUT clients shut down after 10 min on battery |
| HA_CAN_FAIL | TRUE | HA may crash at any tick |
| DIRECT_MON | TRUE | servers poll the NMC directly, not via HA's upsd |

## Single-server model: baseline

| Check | Result |
| --- | --- |
| `TypeOK`, `NoHardCut`, `NoTimeout`, `NoSpurious` | **PASS**, 278,910 distinct states, 11.0 M generated, depth 66 |
| `EventuallyRunning` (liveness, weak fairness) | **PASS** |

An earlier run with energy scaled 2x too generously (114 units) also passed;
it is superseded by the corrected scale above.

## Single-server model: parameter sweep

| Variant | Change | Result | What it tells us |
| --- | --- | --- | --- |
| current_config | shed on any power failure, TrueNAS 60 s on-battery timer, servers via HA's upsd | **FAIL** `NoHardCut` in 5 steps | Today's configuration hard-cuts TrueNAS on a 2-minute blip. |
| via_ha_upsd | DIRECT_MON = FALSE | **FAIL** `NoHardCut` | If servers only learn about LB through HA's NUT server, an HA outage at the wrong moment leaves them running into the load shed. Servers must poll the NMC themselves. |
| shed_too_close | E_LB = 10, E_SHED = 6 | **FAIL** `NoHardCut` | 4 min gap < one 8 min shutdown. Need `E_LB - E_SHED > DRAIN_HI * SMAX`. |
| no_srv_ob_timer | T_SRV_OB = 0 | PASS | The server on-battery timer is a convenience, not a safety requirement; LB alone suffices. |
| slow_shutdown_6 | SMAX = 6 (12 min shutdowns) | **FAIL** `NoHardCut` | 10 min LB-to-shed gap < 12 min shutdown. **If measured shutdown of either server exceeds 8 min, raise the NMC low-battery duration** (e.g. 22 min for a 12 min shutdown budget). |
| slow_shutdown_8 | SMAX = 8 (16 min) | **FAIL** `NoTimeout` and `NoHardCut` constraints | Also exceeds `T_WAIT > SMAX + T_DEAD`. |
| tdead_eq_bmax | T_DEAD = 3 | PASS | With the UPS-load guard, T_DEAD no longer has to exceed boot time. |
| no_grace | T_GRACE = 0 | PASS | With the UPS-load guard, the ping grace period is no longer load-bearing. Kept as margin. |
| ha_reliable | HA_CAN_FAIL = FALSE | PASS | |
| long_net_tail | NET_TAIL = 2 | PASS | Ping dying 4 min before power-off is handled by the load guard. |
| low_restore | E_RESTORE = 10 | **FAIL** `NoHardCut` | Restore with 10 min runtime, immediate second outage: boot (can't poweroff a booting box) + shutdown drains past the shed threshold. Need `E_RESTORE >= E_SHED + DRAIN_HI * (BMAX + SMAX + T_OB)`. |

## Derived constraints (all satisfied by the baseline)

```
E_LB      - E_SHED  >  DRAIN_HI * SMAX                      16 - 6 = 10   >  8    ok
E_RESTORE           >= E_SHED + DRAIN_HI*(BMAX+SMAX+T_OB)   26           >= 22   ok
E_RESTORE           >= E_LB                                 26           >= 16   ok
T_WAIT              >  SMAX + T_DEAD                        12           >  9    ok
E_RETURN            >= E_RESTORE                            30           >= 26   ok
```

## Design changes forced by counterexamples during modelling

1. **Reconcile controller state to the observable group 2 switch** on every
   tick and on HA start. Counterexample: HA cut the group, crashed, rebooted
   into IDLE, saw "on battery", re-entered SHUTDOWN against an already-off
   group, then the NMC auto-returned the group and HA cut it under a booting
   server.
2. **Count "dark" only while the group has power.** Counterexample: servers
   unpingable because the group was off counted toward the "assume off"
   timer; when power returned they booted and were cut.
3. **Guard every cut with the UPS output load.** Counterexample (two-server
   model): both servers off and acknowledged, grid back, a human powers one
   server on, HA cuts the group two minutes later under the booting server.
   Ping cannot tell OFF from BOOT; the UPS load can. This guard also made
   T_DEAD vs BMAX and the NET_TAIL grace period non-critical.
4. **Model the NMC's min return runtime after a full UPS death.** Without it
   the model booted servers at zero energy; with it, the card itself
   prevents that, so E_RETURN is part of the safety argument and must stay
   configured on the card.
5. **Widen the LB-to-shed gap** from 10 min / 6 min to 16 min / 6 min.
6. **Recovery cut no longer requires input present, and its timer is
   persisted.** Counterexample (two-server model, random simulation, depth
   ~250): hours of 2-minute grid flapping drained the UPS to ~11 min; both
   servers were off but their outlets were live because HA kept crashing
   before the recovery timer expired; a human powered one on; it booted at
   full draw into a shed-level battery. The fix makes outlets go dark as
   soon as both servers are off for `T_DEAD`, regardless of grid state or
   HA restarts. The residual case (human powers on within the `T_DEAD`
   window at low runtime) is an explicit assumption; see STATE_MACHINE.md.
7. **Targeted wake for a partially-down group.** Liveness counterexample
   (two-server model): HA crashed mid-SHUTDOWN after powering off one server
   while the other was still booting; after reboot and grid return the
   system sat forever with one server running and one off on a live
   outlet. The all-off power-cycle cannot fire (it would hard-cut the running
   server), so a targeted wake after `T_WOL` was added. In production this is
   an iDRAC `chassis power on` (both servers have iDRAC); the model operator
   is still called `DoWol`.
8. **Remove the timeout cut.** Counterexample (two-server model): a human
   kept powering one server back on while HA was in SHUTDOWN; the wait
   counter reached `T_WAIT` and the unconditional timeout cut hit that
   server mid-shutdown. `T_WAIT` is now an alarm only. Every cut in the
   design now passes through the UPS-load guard.

## Two-server model (`UpsSync2.tla`)

Symmetry reduction on `Servers = {f, s}` (safety runs only; TLC's symmetry is
unsound for liveness).

| Config | Humans | Checks | Result |
| --- | --- | --- | --- |
| `MC2_NoHuman_safety.cfg` | HUMAN = FALSE | `TypeOK`, `NoHardCut`, `NoSpurious`, `NoTimeout` | **PASS**, 1,369,800 distinct states, 89.6 M generated, 30 min 24 s |
| `MC2_NoHuman_live.cfg` | HUMAN = FALSE | `EventuallyRunning` (no symmetry) | **PASS**, 2,331,220 distinct states, 146.8 M generated, depth 66, 1 h 20 min |
| `MC2_Human_sim.cfg` | HUMAN = TRUE (power off any running server, power on any off server, while input present and runtime ≥ E_RESTORE) | `TypeOK`, `NoHardCut`, `NoSpurious`, TLC `-simulate -depth 300` (random traces of 600 min) | **No violation in 172 M states checked** over 55 min. The same method found the previous revision's bug after 1.2 M states. |
| `MC2_Baseline_safety.cfg` | HUMAN = TRUE, exhaustive | `TypeOK`, `NoHardCut`, `NoSpurious` | Previous revision: no violation in 2.8 M distinct states to depth 19 before the 2 h job limit. Not re-run exhaustively on the final revision; the state space exceeds the job limit. |

Counterexamples found and fixed in the two-server model before these runs:
the load-guard case (design change 3) and the timeout-cut case (design
change 6). Both traces are reproducible by reverting the corresponding
lines in `UpsSync2.tla`.

## Revision: runtime is measured at the present load

Code review found that the UPS, and therefore the card, measures "runtime
remaining" at the **present** load. The model's `e` is minutes with every
server running. With group 2 off the load is about 150 W instead of about
400 W, so the card's figure is two to three times `e`. Two things followed.

1. **The controller must not gate restore or wake on the UPS's raw
   runtime.** It now scales that figure to the heaviest load it has seen
   (see `docs/STATE_MACHINE.md` section 8). This is a code fix; the model
   was already right.
2. **The card's own return threshold is weaker than modelled.** The card
   accepts at most 3600 s for "minimum return runtime" (3618 and above are
   rejected; measured on group 3). Sixty minutes at about 150 W is roughly
   17 to 23 minutes with the servers running, depending on the battery's
   load curve. The old value of 30 min is only about 9 to 12. So `E_RETURN`
   is 17 in the model, not 30, and `E_RETURN >= E_RESTORE` cannot be had.

`E_RETURN` only matters after the card itself switched the group off: a
load shed, or a full UPS death. The requirement is that a second outage
starting the moment the servers are powered again can still be survived:
boot, then shutdown, without reaching the shed threshold. Lowering the shed
threshold to 3 min restores that margin, and also widens the low-battery to
shed gap from 10 to 13 min. The card is additionally set to stay off after
a load shed, leaving the restore to the controller, which waits for
`E_RESTORE`.

### TLC re-runs with `E_SHED = 3`, `E_RETURN = 17`

| Config | Checks | Result |
| --- | --- | --- |
| `MC_Baseline.cfg` (single server) | `TypeOK`, `NoHardCut`, `NoTimeout`, `NoSpurious`, `EventuallyRunning` | **PASS**, 299,970 distinct states, 12.1 M generated, depth 58 |
| `MC2_NoHuman_safety.cfg` (two servers) | `TypeOK`, `NoHardCut`, `NoSpurious`, `NoTimeout` | **PASS**, 1,503,720 distinct states, 98.4 M generated, depth 56, 17 min 22 s |
| `MC2_NoHuman_live.cfg` (two servers) | `EventuallyRunning` | **PASS**, 2,564,360 distinct states, 161.6 M generated, depth 58, 44 min 30 s |
| `MC2_Human_sim.cfg` | random simulation with humans, `-simulate -depth 300`, 15 min on 3 workers | **No violation** in 20.4 M states checked |

Single-server safety across the card's return threshold (each about 290 k
distinct states): `E_SHED = 6` passes at `E_RETURN` 22, 21, 20, 19;
`E_SHED = 3` passes at 19, 18, 17, 16. In the model's 2-minute ticks the
bound is `E_RETURN >= E_SHED + DRAIN_HI * (BMAX + SMAX - 1)`, which is 18
for a 6-minute shed and 15 for a 3-minute shed. In continuous time the
last term is `BMAX + SMAX`, so 20 and 17.

### Parameter sweep re-run on the new baseline (single server, safety)

| Variant | Change | Result |
| --- | --- | --- |
| baseline | none | PASS, 299,970 distinct states |
| current_config | shed on any power failure, 60 s on-battery timer, servers via HA's upsd | **FAIL** `NoHardCut` |
| via_ha_upsd | DIRECT_MON = FALSE | **FAIL** |
| shed_too_close | E_LB = 10, E_SHED = 6 | **FAIL** |
| no_srv_ob_timer | T_SRV_OB = 0 | PASS |
| slow_shutdown_6 | SMAX = 6 (12 min shutdowns) | **FAIL**. The 13 min low-battery to shed gap covers a 12 min shutdown only if it starts the moment low battery is reported; one 2-minute tick of delay is enough to miss. Budget about 10 min. |
| slow_shutdown_8 | SMAX = 8 (16 min) | **FAIL** |
| tdead_eq_bmax | T_DEAD = 3 | PASS |
| no_grace | T_GRACE = 0 | PASS |
| ha_reliable | HA_CAN_FAIL = FALSE | PASS |
| long_net_tail | NET_TAIL = 2 | PASS |
| low_restore | E_RESTORE = 10 | **FAIL** |
| old_shed_6 | E_SHED = 6 with the card's real return threshold | **FAIL**. This is why the shed threshold was lowered. |
| card_return_too_low | E_RETURN = 14 | **FAIL**. One below the model's bound of 15. |

### Simulator sweep (`tests/test_sim.py`)

The Python simulator runs the production controller code against a UPS
that reports runtime at the present load along a curve drawn at random per
run (exponent 1.0 to 1.3), which the TLA+ model cannot express. Each
server's draw wanders between 80 and 160 W while running and is 280 W while
booting, 75 % above its running maximum, and a booting server answers on
the network for up to the last two minutes of its boot. The card may return
the group whenever its threshold is met. "True reserve" is in minutes with
both servers at their running maximum. Rows are 1500 runs of 20 simulated
hours unless noted.

| Variant | Result |
| --- | --- |
| chosen settings: shed 3 min, card return 60 min (3000 runs) | **pass**. 10,572 restores; the lowest true reserve at any restore was 21.8 min against a need of about 17 |
| same, with humans and 30 % of commands failing (3000 runs) | **pass**. Lowest true reserve at a restore 21.5 min |
| boot draw 200 W per server | pass |
| boot draw 340 W per server (2.1 times the running maximum) | pass. Lowest true reserve at a restore 21.4 min |
| shed 6 min, card return 60 min | pass in this sample; the model check `old_shed_6` fails |
| shed 3 min, card return 45 min | **hard cut** |
| shed 3 min, card return 30 min (the card's old value) | **hard cut** |
| restore gated on the UPS's raw runtime (the round 1 bug) | fails on the first run |
| peak also counts boot and standby readings (the round 3 bug) | servers left off |
| no settle time before readings count (the round 4 bug) | servers left off |
| curve exponent 0.5 instead of 1.4 | **hard cut** |
| load and iDRAC guard removed | **hard cut** of a booting server |

The simulator also found one bug on its own. A run cut the group, the UPS
then went unreadable, and the card switched the group back on before the
controller ever saw it off. Still in SHUTDOWN, the controller shut the
freshly booted servers down again on good power. The shutdown episode now
ends when the card acknowledges the off command.

### What this leaves as an assumption

After a full UPS death the card will power the servers at 60 min of
light-load runtime no matter what the controller wants. That is safe only
while 60 light-load minutes are worth at least 17 with the servers
running, i.e. while group 1's draw is at least about 35 % of the total
with the servers on (38 % if the battery's load curve is at its steepest).
Today it is about 37 to 40 % (145 to 160 W of 390 to 410 W), which puts
the figure at 17 to 19 minutes. The margin is thin. If group 1's load drops, or a server is
added, re-check it. A full UPS death needs an outage long enough to drain
the UPS with the servers already off, about two to three hours.
