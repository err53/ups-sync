"""Config and options flow."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ADD_ANOTHER,
    CONF_GROUP,
    CONF_IDRAC_HOST,
    CONF_IDRAC_PASSWORD,
    CONF_IDRAC_USER,
    CONF_PVE_NODE,
    CONF_PVE_TOKEN,
    CONF_REACH_PORT,
    CONF_READ_COMMUNITY,
    CONF_SERVERS,
    CONF_WRITE_COMMUNITY,
    DOMAIN,
    OPT_LOAD_IDLE_MAX,
    OPT_NOTIFY,
    MIN_OPTION,
    OPT_TIMINGS,
)
from .machine import Params
from .servers import Server, ServerConfig
from .snmp import ApcClient, UpsError, make_engine

UPS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_READ_COMMUNITY, default="public"): str,
        vol.Required(CONF_WRITE_COMMUNITY, default="private"): str,
        vol.Required(CONF_GROUP, default=2): vol.All(int, vol.Range(min=1, max=16)),
    }
)

SERVER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): str,
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_REACH_PORT, default=22): vol.All(
            int, vol.Range(min=1, max=65535)
        ),
        vol.Optional(CONF_IDRAC_HOST): str,
        vol.Optional(CONF_IDRAC_USER): str,
        vol.Optional(CONF_IDRAC_PASSWORD): str,
        vol.Optional(CONF_PVE_NODE): str,
        vol.Optional(CONF_PVE_TOKEN): str,
        vol.Required(CONF_ADD_ANOTHER, default=False): bool,
    }
)


class UpsSyncConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict = {}

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id(
                f"{user_input[CONF_HOST]}:{user_input[CONF_GROUP]}"
            )
            self._abort_if_unique_id_configured()
            engine = await self.hass.async_add_executor_job(make_engine)
            try:
                await ApcClient(
                    engine,
                    user_input[CONF_HOST],
                    user_input[CONF_READ_COMMUNITY],
                    user_input[CONF_WRITE_COMMUNITY],
                    user_input[CONF_GROUP],
                ).read()
            except UpsError:
                errors["base"] = "cannot_connect"
            finally:
                engine.close_dispatcher()
            if not errors:
                self._data = {**user_input, CONF_SERVERS: []}
                return await self.async_step_server()
        return self.async_show_form(
            step_id="user", data_schema=UPS_SCHEMA, errors=errors
        )

    async def async_step_server(self, user_input=None):
        errors = {}
        if user_input is not None:
            server = dict(user_input)
            more = server.pop(CONF_ADD_ANOTHER)
            has_pve = server.get(CONF_PVE_NODE) and server.get(CONF_PVE_TOKEN)
            if not server.get(CONF_IDRAC_HOST) and not has_pve:
                errors["base"] = "no_channel"
            elif server[CONF_NAME] in {s[CONF_NAME] for s in self._data[CONF_SERVERS]}:
                errors[CONF_NAME] = "duplicate_name"
            elif server.get(CONF_IDRAC_HOST):
                cfg = ServerConfig(
                    **{k: v for k, v in server.items() if k != CONF_NAME},
                    name=server[CONF_NAME],
                )
                session = async_get_clientsession(self.hass, verify_ssl=False)
                if await Server(cfg, session).chassis_on() is None:
                    errors["base"] = "idrac_failed"
            if not errors:
                self._data[CONF_SERVERS].append(server)
                if more:
                    return await self.async_step_server()
                return self.async_create_entry(
                    title=f"UPS {self._data[CONF_HOST]} group {self._data[CONF_GROUP]}",
                    data=self._data,
                )
        return self.async_show_form(
            step_id="server", data_schema=SERVER_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return UpsSyncOptionsFlow()


class UpsSyncOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        defaults = Params()
        schema = {
            vol.Optional(OPT_LOAD_IDLE_MAX): vol.All(
                vol.Coerce(float), vol.Range(min=0)
            ),
            vol.Optional(OPT_NOTIFY, default=""): str,
        }
        for key in OPT_TIMINGS:
            # A zero would act on a single reading; a small reserve is unsafe.
            schema[vol.Required(key, default=getattr(defaults, key))] = vol.All(
                vol.Coerce(float), vol.Range(min=MIN_OPTION.get(key, 10))
            )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(schema), self.config_entry.options
            ),
        )
