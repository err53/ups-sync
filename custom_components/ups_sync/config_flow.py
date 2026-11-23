"""Config and options flow."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

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
    MAX_OPTION,
    MIN_OPTION,
    OPT_GRID,
    OPT_TIMINGS,
)
from .machine import Params
from .servers import Server, ServerConfig
from .snmp import ApcClient, UpsError, make_engine


class _WholeNumber(NumberSelector):
    """A typed number box that only accepts whole numbers and returns an int.

    A bare range renders as a slider with no value shown. The selector's
    step is only a hint to the browser, so 2.5 is refused here rather than
    silently becoming group 2.
    """

    def __init__(self, low: int, high: int) -> None:
        super().__init__(
            NumberSelectorConfig(min=low, max=high, step=1, mode=NumberSelectorMode.BOX)
        )

    def __call__(self, data) -> int:
        value = super().__call__(data)
        if value != int(value):
            raise vol.Invalid("must be a whole number")
        return int(value)


UPS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_READ_COMMUNITY, default="public"): str,
        vol.Required(CONF_WRITE_COMMUNITY, default="private"): str,
        vol.Required(CONF_GROUP, default=2): _WholeNumber(1, 16),
    }
)

SERVER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): str,
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_REACH_PORT, default=22): _WholeNumber(1, 65535),
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
        self._group_name = ""

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id(
                f"{user_input[CONF_HOST]}:{user_input[CONF_GROUP]}"
            )
            self._abort_if_unique_id_configured()
            engine = await self.hass.async_add_executor_job(make_engine)
            ups = ApcClient(
                engine,
                user_input[CONF_HOST],
                user_input[CONF_READ_COMMUNITY],
                user_input[CONF_WRITE_COMMUNITY],
                user_input[CONF_GROUP],
            )
            try:
                await ups.read()
                self._group_name = await ups.group_name()
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
                    title=f"UPS {self._data[CONF_HOST]} group {self._group_label()}",
                    data=self._data,
                )
        added = [s[CONF_NAME] for s in self._data[CONF_SERVERS]]
        return self.async_show_form(
            step_id="server",
            data_schema=SERVER_SCHEMA,
            errors=errors,
            description_placeholders={
                "host": self._data[CONF_HOST],
                "group": self._group_label(),
                "added": ", ".join(added) if added else "none yet",
            },
        )

    def _group_label(self) -> str:
        """For example: 2 "Servers"."""
        number = str(self._data[CONF_GROUP])
        return f'{number} "{self._group_name}"' if self._group_name else number

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
            vol.Optional(OPT_GRID, default=[]): EntitySelector(
                EntitySelectorConfig(multiple=True)
            ),
        }
        for key in OPT_TIMINGS:
            # A zero would act on a single reading; a low charge is unsafe.
            schema[vol.Required(key, default=getattr(defaults, key))] = vol.All(
                vol.Coerce(float),
                vol.Range(min=MIN_OPTION.get(key, 10), max=MAX_OPTION.get(key)),
            )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(schema), self.config_entry.options
            ),
        )
