#!/bin/bash
# Run TLC safety check over parameter variants of MC_Baseline_safety.cfg. Prints PASS/FAIL per variant.
cd "$(dirname "$0")/.."
J=/opt/homebrew/opt/openjdk/bin/java; OUT=${1:-/tmp}; mkdir -p "$OUT"
run() { name=$1; shift; cfg="$OUT/MC_$name.cfg"; cp MC_Baseline_safety.cfg "$cfg"
  for kv in "$@"; do k=${kv%%=*}; v=${kv#*=}; sed -i '' "s/^  $k = .*/  $k = $v/" "$cfg"; done
  $J -XX:+UseParallelGC -Xmx6g -cp tools/tla2tools.jar tlc2.TLC -workers auto -config "$cfg" UpsSync.tla > "$OUT/$name.log" 2>&1
  if grep -q 'No error has been found' "$OUT/$name.log"; then r=PASS; else r=FAIL; fi
  n=$(grep -oE '[0-9,]+ distinct states found' "$OUT/$name.log" | tail -1)
  printf '%-28s %-5s %s  [%s]\n' "$name" "$r" "$n" "$*"; }
run baseline
run current_config        E_SHED=57 T_SRV_OB=1 DIRECT_MON=FALSE   # shed ~immediately on battery; TrueNAS 60s OB timer via HA upsd; no HA orchestration beyond today's
run via_ha_upsd           DIRECT_MON=FALSE                          # servers get LB only through HA's NUT server
run shed_too_close        E_LB=10 E_SHED=6                         # 10 min LB, 6 min shed: gap < one full shutdown
run no_srv_ob_timer       T_SRV_OB=0                                # servers react to LB only
run slow_shutdown_6       SMAX=6                                    # 12 min shutdowns
run slow_shutdown_8       SMAX=8                                    # 16 min shutdowns
run tdead_eq_bmax         T_DEAD=3                                  # dead timer not longer than boot time
run no_grace              T_GRACE=0                                 # cut as soon as ping stops
run ha_reliable           HA_CAN_FAIL=FALSE
run long_net_tail         NET_TAIL=2                                # ping dies 4 min before power-off
run low_restore           C_RESTORE=20                              # restore at 20 % charge: 12 min in the bank
run restore_at_bound      C_RESTORE=34                              # 34 % of 57 = 19.4, just over shed + boot + shutdown
run restore_below_bound   C_RESTORE=31                              # 31 % of 57 = 17.7, just under
run aged_battery          EMAX=24                                   # battery holds 24 full-load min: 80 % = 19.2
run aged_battery_too_far  EMAX=22                                   # 80 % of 22 = 17.6
run old_shed_6            E_SHED=6                                  # previous shed threshold, same card return
run card_return_too_low   E_RETURN=14                               # card returns the group with too little reserve
