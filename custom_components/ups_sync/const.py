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
OPT_GRID = "grid_entities"
OPT_TIMINGS = (
    "t_ob", "t_grid", "t_grace", "t_dead", "t_wake", "t_hold", "t_wait", "e_restore"
)
# Lowest value the options form accepts. Default 10 s.
# e_restore: formal/RESULTS.md needs shed + boot + shutdown = 17 min at least.
# t_grid: a grid sensor is slower and less sure than the UPS itself.
MIN_OPTION = {"e_restore": 20, "t_grid": 120}

POLL_SECONDS = 15
STALE_SECONDS = 120
