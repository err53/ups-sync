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
   "reachable" and "power" sensors should be on. After both have been up
   for five minutes, the Peak load sensor is filled in. That is the
   heaviest UPS load seen while both servers were up and settled. The
   controller uses it to turn the UPS's runtime figure into "runtime with
   the servers on". It only ever rises.
   If the integration is added while a server is off, Peak load stays
   unknown and the problem sensor stays on. Until both servers have been seen
   running, nothing is power-cycled or restored. An outage still shuts the
   servers down and cuts the group, and the group then stays off until you
   turn it on by hand.
   To reset it (for example after a one-off heavy load on the UPS), remove
   the integration and add it again with both servers running.
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
   stable for 6 min **and** the "Runtime with servers on" sensor reads 26
   or more, then both servers booting on their own. Start with the "UPS
   battery charge" sensor at 100 %. Otherwise the restore may wait a long
   time: on 2026-11-17 a test started at about 80 % charge, six minutes on
   battery left it at 74 %, and "Runtime with servers on" sat at 16 to 18
   for over twenty minutes after input returned.
   This is the first time either shutdown channel is used for real.
11. **Home Assistant down test.** Stop Home Assistant, pull the UPS input,
    and confirm both servers shut themselves down after 10 minutes on
    battery. Restore input and start Home Assistant. Within about 10 minutes
    it power-cycles the group and the servers boot.

## During an outage

- Do nothing. Do not press power buttons while the UPS is on battery.
- Do not power a server on by hand while "Runtime with servers on" is
  under 26. Let the controller do it. The runtime the UPS shows with the
  servers off is several times what it will be once they are running.
- **After an outage the restore can take a while.** It waits until the
  battery has recharged enough to carry the servers through another
  outage, and that wait is real, not a reading that is too cautious. On
  2026-11-17 the UPS said 48 minutes at full load on mains and 29 minutes
  two minutes into the outage. "Runtime with servers on" is scaled with an
  exponent of 1.4, which matched what the battery did on battery that
  night (about 1.44); do not try to lower it to speed restores up.
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
  battery.

## The UPS battery

As read from the card on 2026-11-17: battery last replaced 2022-07-15, the
card's recommended replacement date 2027-01-15, replace-battery warning not
raised, last self-test (2026-11-07) passed, last runtime calibration
(2025-04-20) **invalid**, so the card's runtime figures are not matched to
this battery. An aged battery holds less and sags more under load, which
fits the 48 to 29 minute drop above.

- Replace the battery by the card's date. Restores after an outage will
  then come sooner, and every runtime figure will be closer to the truth.
- Afterwards, set the replacement date on the card, let the battery reach
  100 %, and run the card's runtime calibration. That discharges the
  battery deeply under the present load, so do it with Maintenance on and
  the servers shut down, or with them on another supply.

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
