"""Constants for ups_sync."""

DOMAIN = "ups_sync"

CONF_READ_COMMUNITY = "read_community"
CONF_WRITE_COMMUNITY = "write_community"
CONF_GROUP = "group"
CONF_SERVERS = "servers"
CONF_REACH_PORT = "reach_port"
CONF_IDRAC_HOST = "idrac_host"
CONF_IDRAC_USER = "idrac_user"
CONF_IDRAC_PASSWORD = "idrac_password"
CONF_PVE_NODE = "pve_node"
CONF_PVE_TOKEN = "pve_token"
CONF_ADD_ANOTHER = "add_another"

OPT_LOAD_IDLE_MAX = "load_idle_max"
OPT_NOTIFY = "notify_services"
# Option name -> Params field. Seconds, except e_restore (minutes).
OPT_TIMINGS = (
    "t_ob", "t_grace", "t_dead", "t_wake", "t_hold", "t_off", "t_wait", "e_restore"
)

POLL_SECONDS = 15
STALE_SECONDS = 120
