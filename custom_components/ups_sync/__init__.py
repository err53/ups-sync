"""UPS sync: graceful server shutdown and outlet-group control for APC UPSes."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_GROUP,
    CONF_READ_COMMUNITY,
    CONF_SERVERS,
    CONF_WRITE_COMMUNITY,
    OPT_GRID,
    OPT_LOAD_IDLE_MAX,
    OPT_NOTIFY,
    OPT_TIMINGS,
    POLL_SECONDS,
)
from .controller import Controller
from .machine import Params
from .servers import Server, ServerConfig
from .snmp import ApcClient, make_engine

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]

type UpsSyncEntry = ConfigEntry[Controller]


def params_from_options(options: Mapping[str, Any]) -> Params:
    kwargs = {k: float(options[k]) for k in OPT_TIMINGS if k in options}
    return Params(load_idle_max=options.get(OPT_LOAD_IDLE_MAX), **kwargs)


async def async_setup_entry(hass: HomeAssistant, entry: UpsSyncEntry) -> bool:
    data = entry.data
    engine = await hass.async_add_executor_job(make_engine)
    ups = ApcClient(
        engine,
        data[CONF_HOST],
        data[CONF_READ_COMMUNITY],
        data[CONF_WRITE_COMMUNITY],
        int(data[CONF_GROUP]),
    )
    session = async_get_clientsession(hass, verify_ssl=False)
    servers = [
        Server(
            ServerConfig(
                name=s[CONF_NAME],
                **{
                    k: v for k, v in s.items() if k != CONF_NAME and v not in ("", None)
                },
            ),
            session,
        )
        for s in data[CONF_SERVERS]
    ]
    notify = [
        s.strip() for s in entry.options.get(OPT_NOTIFY, "").split(",") if "." in s
    ]
    ctl = Controller(
        hass,
        entry.entry_id,
        ups,
        servers,
        params_from_options(entry.options),
        notify,
        list(entry.options.get(OPT_GRID, [])),
    )
    await ctl.async_load()
    entry.runtime_data = ctl

    async def _stop() -> None:
        await ctl.async_stop()  # waits for a step in flight
        engine.close_dispatcher()

    entry.async_on_unload(_stop)
    await ctl.async_step()
    entry.async_on_unload(
        async_track_time_interval(hass, ctl.async_step, timedelta(seconds=POLL_SECONDS))
    )
    entry.async_on_unload(entry.add_update_listener(_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _reload(hass: HomeAssistant, entry: UpsSyncEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: UpsSyncEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
