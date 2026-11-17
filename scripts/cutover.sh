#!/bin/sh
# Switch the APC card and TrueNAS from the old "shed after 60 s on battery"
# setup to the one ups_sync was verified against. Run it when you are there
# to watch, then arm the controller in Home Assistant.
#
#   scripts/cutover.sh show       print the current values
#   scripts/cutover.sh apply      apply the new values
#   scripts/cutover.sh rollback   put the old values back
#
# Environment:
#   UPS_HOST          card address                      (required)
#   UPS_WRITE         SNMP write community              (default private)
#   UPS_READ          SNMP read community               (default public)
#   UPS_GROUP         outlet group with the servers     (default 2)
#   TRUENAS_HOST      TrueNAS address                   (optional)
#   TRUENAS_ADMIN     user:password of a TrueNAS admin  (with TRUENAS_HOST)
set -eu

: "${UPS_HOST:?set UPS_HOST}"
W=${UPS_WRITE:-private} R=${UPS_READ:-public} G=${UPS_GROUP:-2}
APC=1.3.6.1.4.1.318.1.1.1
LOW_BATT=$APC.5.2.8.0          # low-battery duration, 1/100 s
T=$APC.12.2.2.1                # outlet group config table
SHED_ON_BATT=$T.10.$G          # shed after time on battery: 1 off, 2 on
SHED_RUNTIME=$T.11.$G          # shed below runtime remaining: 1 off, 2 on
SHED_RUNTIME_S=$T.14.$G        # that runtime, seconds
MIN_RETURN_S=$T.6.$G           # min return runtime, seconds

get() { snmpget -v2c -c "$R" -Oqv "$UPS_HOST" "$1"; }
put() {
    snmpset -v2c -c "$W" -Oqv "$UPS_HOST" "$1" "$2" "$3" >/dev/null
    sleep 1
    echo "  $4 = $(get "$1")"
}

truenas() { # method path [json]
    curl -sk --fail-with-body -m 60 -u "$TRUENAS_ADMIN" -X "$1" \
        -H 'Content-Type: application/json' ${3:+-d "$3"} \
        "https://$TRUENAS_HOST/api/v2.0$2"
}

show() {
    echo "UPS $UPS_HOST, group $G"
    echo "  low-battery duration        = $(get $LOW_BATT)"
    echo "  shed after time on battery  = $(get $SHED_ON_BATT)   (1 off, 2 on)"
    echo "  shed below runtime          = $(get $SHED_RUNTIME)   (1 off, 2 on)"
    echo "  shed runtime threshold (s)  = $(get $SHED_RUNTIME_S)"
    echo "  min return runtime (s)      = $(get $MIN_RETURN_S)"
    if [ -n "${TRUENAS_HOST:-}" ]; then
        echo "TrueNAS $TRUENAS_HOST UPS service"
        truenas GET /ups | python3 -c '
import json, sys
d = json.load(sys.stdin)
for k in ("mode", "driver", "port", "remotehost", "shutdown", "shutdowntimer", "powerdown"):
    print(f"  {k} = {d.get(k)!r}")'
    fi
}

case "${1:-}" in
show)
    show
    ;;
apply)
    echo "Card:"
    put $LOW_BATT t 96000 "low-battery duration (16 min)"
    put $SHED_RUNTIME_S i 360 "shed runtime threshold (6 min)"
    put $SHED_RUNTIME i 2 "shed below runtime (on)"
    put $SHED_ON_BATT i 1 "shed after time on battery (off)"
    put $MIN_RETURN_S i 1800 "min return runtime (30 min)"
    if [ -n "${TRUENAS_HOST:-}" ]; then
        echo "TrueNAS: watch the card directly, shut down on low battery or 10 min on battery"
        # powerdown stays false: TrueNAS must never tell the UPS to power off.
        truenas PUT /ups '{
          "mode": "MASTER", "driver": "snmp-ups$(various)", "port": "'"$UPS_HOST"'",
          "options": "community = '"$R"'\nsnmp_version = v2c\nmibs = apcc\npollfreq = 15",
          "shutdown": "BATT", "shutdowntimer": 600, "powerdown": false, "rmonitor": false
        }' >/dev/null
        truenas POST /service/restart '{"service": "ups"}' >/dev/null
    fi
    show
    ;;
rollback)
    echo "Card:"
    put $SHED_ON_BATT i 2 "shed after time on battery (on)"
    put $SHED_RUNTIME i 1 "shed below runtime (off)"
    put $SHED_RUNTIME_S i 1200 "shed runtime threshold"
    put $LOW_BATT t 12000 "low-battery duration (2 min)"
    if [ -n "${TRUENAS_HOST:-}" ]; then
        echo "TrueNAS: back to following Home Assistant's NUT server"
        truenas PUT /ups '{"mode": "SLAVE", "shutdown": "BATT", "shutdowntimer": 60, "rmonitor": true}' >/dev/null
        truenas POST /service/restart '{"service": "ups"}' >/dev/null
    fi
    show
    ;;
*)
    sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
