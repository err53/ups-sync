# Runbook

## Before arming

Do these once, in order, when you can watch the servers.

1. **Check the BIOS.** On each server, "AC power recovery" must be **On**,
   not "Last". With "Last", a server that was shut down stays off when the
   outlet comes back, and the controller power-cycles the group every
   16 minutes trying to revive it.
2. **Check anything else that reacts to the UPS.** Nothing may tell the UPS
   to switch its output off, and Home Assistant must not shut its own host
   down on low battery. On this install the NUT add-on has
   `shutdown_host: true`. With that, Home Assistant halts when the UPS
   reports low battery and stays halted when power returns, because its
   outlet never lost power. Then nothing restores the servers. Set it to
   `false` before cutting over.
3. **Install and configure** the integration (see the README). Leave it
   disarmed. The State sensor should read Idle, and each server's
   "reachable" and "power" sensors should be on, and "UPS battery charge"
   should show a figure. The card must report its charge: every restore
   and wake goes by it, and a card that does not is reported as unreadable.
4. **Calibrate the idle load.** Shut both servers down from their own UIs
   and leave them plugged in. Wait two minutes, read the UPS load sensor,
   add 40, and enter that as the idle load in the integration's options.
   Power the servers back on. The Calibrated sensor should now be on and
   the problem sensor off.
   - Estimate on 2026-11-17, before measuring: UPS 370 to 410 W total, the
     two servers 155 W and 112 W by their iDRACs, so roughly 160 W with both
     off and a setting near 200 W. One running server adds at least 110 W.
5. **Time a shutdown.** While doing step 4, time each server from the
   shutdown command to power off. The design assumes 8 minutes or less and
   the card settings leave room for about 10. If either takes longer, raise
   the card's low-battery duration before going further. The controller
   reads that value from the card, so there is nothing to change in its
   options.
6. **Dry run.** With the servers back up, pull the UPS input for three
   minutes. The "Dry run would do" sensor should list a shutdown
   for each server after two minutes. Nothing should happen. Restore input.
7. **Cut over.** `UPS_HOST=... TRUENAS_HOST=... TRUENAS_ADMIN=user:pass
   scripts/cutover.sh apply`. This moves the card to runtime-based load shed
   and points TrueNAS at the card. Do this and step 9 together: the old
   card settings cut power 150 s into an outage, which is mid-shutdown.
8. **Choose the grid entities** (only if a home battery feeds the UPS and
   the servers should not run it down). In the integration's options, pick
   one or more entities that only work on grid power. Here that is
   `sensor.tesla_wall_connector_grid_voltage`, which had no gaps in two
   weeks of history. Check that the Grid sensor is on
   afterwards. With the transfer switch on the home battery and
   the controller still disarmed, switch the charger's breaker off for six
   minutes: "Dry run would do" should list a shutdown for each server.
9. **Arm.** Turn on `switch.ups_sync_armed`.
10. **Real test.** Pull the UPS input. Expect: shutdown commands at 2 min,
   both servers off within 8 more, group cut about 2 min after the last one
   goes quiet. Restore input. Expect the group back on once input has been
   stable for 6 min **and** the "UPS battery charge" sensor reads 80 % or
   more (the default of the restore-charge option), then both servers
   booting on their own. If the test leaves the charge under 80 %, the
   restore waits for the UPS to recharge that far.
   This is the first time either shutdown channel is used for real.
11. **Home Assistant down test.** Stop Home Assistant, pull the UPS input,
    and confirm both servers shut themselves down after 10 minutes on
    battery. Restore input and start Home Assistant. Within about 10 minutes
    it power-cycles the group and the servers boot.

## During an outage

- Do nothing. Do not press power buttons while the UPS is on battery.
- Do not power a server on by hand while "UPS battery charge" is under
  80 % (or your restore-charge setting). Let the controller do it. The
  runtime the UPS shows with the servers off is several times what it
  will be once they are running.
- **After an outage the restore waits for the battery charge** to reach
  80 %, so that the servers can get through another outage. From near
  empty that took 42 minutes on 2026-11-20; a grid outage that the home
  battery carried leaves the charge where it was, so the servers come
  back 6 minutes after the grid.
  No restore or wake goes by the UPS's runtime figure. It is measured at
  the present load, two to three times too long with the servers off, and
  an earlier version that scaled it to the servers' load left a group cut
  on 2026-11-20 at 94 % charge for good. On battery the runtime still sets
  the low-battery flag (below the card's low-battery duration), which
  shuts the servers down at once.
- The card's charge has topped out at 93 to 94 % on this battery. Keep
  the restore-charge setting well under that, or the group is never
  restored; the problem sensor turns on after six hours of waiting.
- The restore charge must still mean at least 26 minutes with the servers
  running, which covers a boot, a shutdown and the card's shed margin if
  the power fails again at once. Check it after a completed runtime
  calibration, with both servers running and busy: "UPS runtime at present
  load" x 100 / "UPS battery charge" is roughly what a full battery holds.
  At 80 % it must be 32.5 or more (the old battery: about 41). Below that,
  replace the battery or raise the restore charge.
- **With grid entities configured:** five minutes after the grid goes, the
  servers shut down and their group is cut, whichever way the transfer
  switch is set. They stay off until the grid is back and has been stable
  for six minutes. Moving the switch to the home battery does not bring
  them back. To run them on the home battery anyway, turn on Maintenance
  and switch the group on yourself. Turning Maintenance off again while
  the grid is still out starts the five minutes afresh, then shuts them
  down.
  If the servers stay off after the grid is back, look at the state
  sensor: `waiting_for_grid: true` means no grid entity is showing the
  grid up yet, and `grid_entities_missing` lists any that no integration
  is providing (renamed, deleted, or not loaded yet).
- If `grid_entities_missing` is ever non-empty for more than a few minutes
  after Home Assistant starts, fix or remove that entity before the next
  outage. While no grid entity can show the grid up, the group is not
  restored after an outage.
- **Without grid entities:** with the transfer switch on grid, moving it to
  the battery within two minutes of the outage avoids a shutdown. Later
  than that, the shutdown runs to the end and the servers come back once
  the UPS has had input for six minutes, even if that input is the home
  battery, and "UPS battery charge" reads 80 % or more (or your
  restore-charge setting).

## The UPS battery

Replaced on 2026-11-20 (the previous one dated from 2022-07-15). Set the
replacement date on the card. Its runtime calibration is **invalid** until
one completes, so the card's runtime figures are not matched to this
battery yet. Its charge reading also levelled off at 94 % after a few hours
on mains, as the old battery's did.

- A runtime calibration makes the card's figures match the battery. Plan it
  as a separate job. It runs the UPS on battery, which
  looks like an outage to everything: the armed controller shuts the
  servers down after 2 minutes (Maintenance does not stop that), TrueNAS
  shuts flandre down after 10, and the card's load shed still applies. The
  load then drops and the calibration may abort. Disarm the integration,
  shut the servers down by hand with Maintenance on, and re-arm afterwards.
  It also leaves the battery nearly empty, so pick a time when an outage
  would not matter.
- The load must be steady (APC suggests about 30 %, within 5 %; its
  minimum is 10 %). The servers swing too much: about 375 to 460 W, and up
  to 30 % within minutes. On 2026-11-20 a calibration with them running and
  a heater added aborted after two minutes. Use the network and Home
  Assistant alone (about 155 W) plus a steady resistive load, such as an
  oil-filled heater on its lowest setting with its thermostat turned up,
  in a group 2 outlet, starting from a full charge.
- With the servers off and Maintenance on, the extra load changes nothing
  the integration would do. The heavier load does shorten the runtime, so
  the card reports low battery sooner during the calibration; with the
  servers already off that only means nothing more to shut down.

## Maintenance

Turn on `switch.ups_sync_maintenance` before powering a server off by hand
or switching the group off yourself. Otherwise the controller powers it back
on or power-cycles the group within ten minutes. Outage shutdown still
works in maintenance. Turn it off when you are done.

## Notifications and what they mean

| Message | Meaning |
| --- | --- |
| `shutdown <server>: on battery (accepted via ...)` | the server took the shutdown command |
| `shutdown <server> ... FAILED` | no channel accepted it; the server's own NUT client is now the only thing that will shut it down |
| `cut group: servers shut down` | normal end of an outage shutdown |
| `cut group: all servers off on live outlets` | power-cycling so the servers boot |
| `restore group: input stable` | group back on |
| `wake <server>` | one server was off while the other ran; powered on by iDRAC |
| `alarm group: shutdown is taking too long` | 24 min in the shutdown state; something is still drawing power |
| `abort group` | power came back and the shutdown never completed; back to idle |
| `cut skipped: guard failed on re-check` | (log only) something started drawing power at the last moment |

The state sensor's attributes say which problem it is: `ups_stale`,
`alarm`, `group_command_failed`, `stuck_servers_off` (every server is off
but the UPS load is above the idle setting, so the group cannot be
power-cycled: recalibrate), or `calibrated: false`.

## Rollback

Turn off `switch.ups_sync_armed`, then `scripts/cutover.sh rollback`.
