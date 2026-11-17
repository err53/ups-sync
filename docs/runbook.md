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
   disarmed. `sensor.ups_sync_state` should read `idle` and its attributes
   should show both servers reachable with `chassis_on: true`. After the
   first reading with both servers up and the battery above 90 %,
   `full_load_min_per_pct` is filled in. That is the learned link between
   charge and runtime with the servers running; nothing to enter by hand.
4. **Calibrate the idle load.** Shut both servers down from their own UIs
   and leave them plugged in. Wait two minutes, read `load_w` from the state
   sensor's attributes, add 40, and enter that as the idle load in the
   integration's options. Power the servers back on. The `calibrated`
   attribute should now be true and the problem sensor off.
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
   minutes. The state sensor's `would_act` attribute should list a shutdown
   for each server after two minutes. Nothing should happen. Restore input.
7. **Cut over.** `UPS_HOST=... TRUENAS_HOST=... TRUENAS_ADMIN=user:pass
   scripts/cutover.sh apply`. This moves the card to runtime-based load shed
   and points TrueNAS at the card. Do this and step 8 together: the old
   card settings cut power 150 s into an outage, which is mid-shutdown.
8. **Arm.** Turn on `switch.ups_sync_armed`.
9. **Real test.** Pull the UPS input. Expect: shutdown commands at 2 min,
   both servers off within 8 more, group cut about 2 min after the last one
   goes quiet. Restore input. Expect the group back on 6 min later, provided
   the battery is above about half, and both servers booting on their own.
   This is the first time either shutdown channel is used for real.
10. **Home Assistant down test.** Stop Home Assistant, pull the UPS input,
    and confirm both servers shut themselves down after 10 minutes on
    battery. Restore input and start Home Assistant. Within about 10 minutes
    it power-cycles the group and the servers boot.

## During an outage

- Do nothing. Do not press power buttons while the UPS is on battery.
- Do not power a server on while the battery is under half charge. Let the
  controller do it. The runtime the UPS shows with the servers off is
  several times what it will be once they are running.
- With the transfer switch on grid, moving it to the battery within two
  minutes of the outage avoids a shutdown. Later than that, the shutdown
  runs to the end and the servers come back about 20 minutes after input
  returns.

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
