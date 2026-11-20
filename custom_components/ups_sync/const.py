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
# Option name -> Params field. Seconds, except c_restore (% battery charge).
OPT_TIMINGS = (
    "t_ob", "t_grid", "t_grace", "t_dead", "t_wake", "t_hold", "t_wait", "c_restore",
)
# Lowest value the options form accepts. Default 10 s.
# c_restore: c_restore % of a full battery must be at least 26 minutes with
#   the servers running (formal/RESULTS.md); at 80 % that needs 32.5.
# t_grid: a grid sensor is slower and less sure than the UPS itself.
MIN_OPTION = {"c_restore": 50, "t_grid": 120}
MAX_OPTION = {"c_restore": 100}

# Shown as the device's "Visit" link: what Armed and Maintenance do.
CONTROLS_URL = "https://github.com/err53/ups-sync#armed-and-maintenance"

POLL_SECONDS = 15
STALE_SECONDS = 120
