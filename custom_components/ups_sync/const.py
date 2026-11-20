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
OPT_GRID = "grid_entities"
# Option name -> Params field. Seconds, except e_restore (minutes) and
# c_restore (% battery charge).
OPT_TIMINGS = (
    "t_ob", "t_grid", "t_grace", "t_dead", "t_wake", "t_hold", "t_wait",
    "e_restore", "c_restore",
)
# Lowest value the options form accepts. Default 10 s.
# e_restore: formal/RESULTS.md needs shed + boot + shutdown = 17 min at least.
# c_restore: the same 17 min out of a full battery's 41 or so with the
#   servers running, with room for the battery to age.
# t_grid: a grid sensor is slower and less sure than the UPS itself.
MIN_OPTION = {"e_restore": 20, "c_restore": 50, "t_grid": 120}
MAX_OPTION = {"c_restore": 100}

# Shown as the device's "Visit" link: what Armed and Maintenance do.
CONTROLS_URL = "https://github.com/err53/ups-sync#armed-and-maintenance"

POLL_SECONDS = 15
STALE_SECONDS = 120
