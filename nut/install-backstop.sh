#!/bin/sh
# Server-side backstop for ups_sync on a Debian or Proxmox host.
#
# The host watches the APC card itself over SNMP (not through Home
# Assistant) and shuts itself down when the UPS reports low battery, or
# after ON_BATTERY_SECONDS on battery. It never tells the UPS to power off.
#
#   ssh root@host 'sh -s -- 192.168.1.4 public' < nut/install-backstop.sh
set -eu

UPS_HOST=${1:?usage: install-backstop.sh <ups-host> [snmp-read-community] [on-battery-seconds]}
COMMUNITY=${2:-public}
ON_BATTERY_SECONDS=${3:-600}

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nut-server nut-client nut-snmp

PASS=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)
cd /etc/nut

cat > nut.conf <<CONF
MODE=standalone
CONF

cat > ups.conf <<CONF
[apc]
    driver = snmp-ups
    port = $UPS_HOST
    community = $COMMUNITY
    snmp_version = v2c
    mibs = apcc
    pollfreq = 15
CONF

cat > upsd.conf <<CONF
LISTEN 127.0.0.1 3493
CONF

cat > upsd.users <<CONF
[upsmon]
    password = $PASS
    upsmon secondary
CONF

# "secondary": this host only shuts ITSELF down. It never sets the UPS
# kill-power flag, so the outlet groups stay under the card's control.
cat > upsmon.conf <<CONF
MONITOR apc@localhost 1 upsmon $PASS secondary
MINSUPPLIES 1
SHUTDOWNCMD "/sbin/shutdown -h +0"
NOTIFYCMD /usr/sbin/upssched
POLLFREQ 5
POLLFREQALERT 5
HOSTSYNC 15
DEADTIME 60
NOTIFYFLAG ONBATT SYSLOG+EXEC
NOTIFYFLAG ONLINE SYSLOG+EXEC
NOTIFYFLAG LOWBATT SYSLOG
NOTIFYFLAG COMMBAD SYSLOG
NOTIFYFLAG NOCOMM SYSLOG
CONF

cat > upssched.conf <<CONF
CMDSCRIPT /etc/nut/upssched-cmd
PIPEFN /run/nut/upssched.pipe
LOCKFN /run/nut/upssched.lock
AT ONBATT * START-TIMER onbatt $ON_BATTERY_SECONDS
AT ONLINE * CANCEL-TIMER onbatt
CONF

# upssched runs this as the unprivileged "nut" user, which may not call
# shutdown. It asks upsmon instead: "-c fsd" makes the running monitor run
# its SHUTDOWNCMD as root. As a secondary it sets no flag on the UPS.
cat > upssched-cmd <<'CONF'
#!/bin/sh
case "$1" in
    onbatt)
        logger -t ups-sync "on battery too long, shutting down"
        /usr/sbin/upsmon -c fsd
        ;;
esac
CONF

chown root:nut nut.conf ups.conf upsd.conf upsd.users upsmon.conf upssched.conf upssched-cmd
chmod 640 nut.conf ups.conf upsd.conf upsd.users upsmon.conf upssched.conf
chmod 750 upssched-cmd

systemctl enable -q nut-driver-enumerator.service nut-server.service nut-monitor.service 2>/dev/null || true
systemctl restart nut-driver-enumerator.service 2>/dev/null || upsdrvctl start
sleep 5
systemctl restart nut-server.service
sleep 3

# Do not start the monitor unless the driver really sees the UPS online.
STATUS=$(upsc apc@localhost ups.status 2>/dev/null || true)
echo "ups.status: ${STATUS:-<none>}"
case "$STATUS" in
    OL*) systemctl restart nut-monitor.service ;;
    *) systemctl stop nut-monitor.service 2>/dev/null || true
       echo "UPS is not reporting online; nut-monitor left stopped" >&2
       exit 1 ;;
esac
sleep 2
echo "battery.runtime: $(upsc apc@localhost battery.runtime 2>/dev/null)"
echo "battery.runtime.low: $(upsc apc@localhost battery.runtime.low 2>/dev/null)"
echo "nut-monitor: $(systemctl is-active nut-monitor.service)"
# The timer path only works if the nut user may signal the monitor.
if su -s /bin/sh nut -c 'kill -0 "$(cat /run/nut/upsmon.pid)"'; then
    echo "on-battery timer: nut user can signal upsmon"
else
    echo "on-battery timer BROKEN: nut user cannot signal upsmon" >&2
    exit 1
fi
