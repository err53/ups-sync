"""Glue between Home Assistant, the UPS, the servers and the state machine."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections.abc import Callable
from dataclasses import asdict

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STALE_SECONDS
from .machine import (
    CUT,
    IDLE,
    SHUTDOWN,
    Action,
    Obs,
    Params,
    ServerObs,
    State,
    cut_guard,
    decide,
    observe,
)
from .servers import Server, ServerError
from .snmp import ApcClient, UpsError

_LOGGER = logging.getLogger(__name__)


class Controller:
    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        ups: ApcClient,
        servers: list[Server],
        params: Params,
        notify_services: list[str],
    ) -> None:
        self.hass = hass
        self.ups = ups
        self.servers = {s.cfg.name: s for s in servers}
        self.params = params
        self.notify_services = notify_services
        self.state = State()
        self.obs: Obs | None = None
        self.armed = False
        self.maintenance = False
        self.would_act: list[str] = []
        self.last_action = ""
        self.last_ok: float | None = None
        self.last_error = ""
        self._store: Store = Store(hass, 1, f"{DOMAIN}.{entry_id}")
        self._saved: dict = {}
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[], None]] = []
        self._failed: set[str] = set()

    # ---- persistence -----------------------------------------------------

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.armed = bool(data.get("armed", False))
        self.maintenance = bool(data.get("maintenance", False))
        self.state.dead_since = data.get("dead_since")
        self._saved = self._snapshot()

    def _snapshot(self) -> dict:
        return {
            "armed": self.armed,
            "maintenance": self.maintenance,
            "dead_since": self.state.dead_since,
        }

    async def _save_if_changed(self) -> None:
        snap = self._snapshot()
        if snap != self._saved:
            self._saved = snap
            await self._store.async_save(snap)

    async def async_set_flag(self, name: str, value: bool) -> None:
        async with self._lock:
            setattr(self, name, value)
            await self._save_if_changed()
        self._notify_listeners()

    # ---- entities --------------------------------------------------------

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    def _notify_listeners(self) -> None:
        for cb in list(self._listeners):
            cb()

    @property
    def stale(self) -> bool:
        return self.last_ok is None or time.monotonic() - self.last_ok > STALE_SECONDS

    @property
    def problem(self) -> bool:
        return self.stale or self.state.alarm or self.params.load_idle_max is None

    def attributes(self) -> dict:
        attrs = {
            "armed": self.armed,
            "maintenance": self.maintenance,
            "alarm": self.state.alarm,
            "ups_stale": self.stale,
            "calibrated": self.params.load_idle_max is not None,
            "acked": sorted(self.state.acked),
            "would_act": self.would_act,
            "last_action": self.last_action,
            "last_error": self.last_error,
        }
        if self.obs:
            attrs.update(asdict(self.obs))
        return attrs

    # ---- main loop -------------------------------------------------------

    async def _gather(self) -> Obs:
        ups, *probes = await asyncio.gather(
            self.ups.read(),
            *(s.reachable() for s in self.servers.values()),
            *(s.chassis_on() for s in self.servers.values()),
        )
        n = len(self.servers)
        return Obs(
            **asdict(ups),
            servers={
                name: ServerObs(probes[i], probes[n + i])
                for i, name in enumerate(self.servers)
            },
        )

    async def async_step(self, _now=None) -> None:
        if self._lock.locked():
            return  # previous step still running
        async with self._lock:
            try:
                obs = await self._gather()
            except UpsError as exc:
                # No reading, no decision. Timers hold; the alarm sensor
                # turns on once the reading is STALE_SECONDS old.
                self.last_error = f"UPS: {exc}"
                _LOGGER.warning("UPS read failed: %s", exc)
                self._notify_listeners()
                return
            self.obs = obs
            self.last_ok = time.monotonic()
            await self._decide_and_act(obs)
            await self._save_if_changed()
        self._notify_listeners()

    async def _decide_and_act(self, obs: Obs) -> None:
        mono, wall = time.monotonic(), time.time()
        st = self.state
        observe(st, obs, mono, wall)
        if not self.armed:
            # Dry run: never leave the observable states, never act.
            st.ctl = IDLE if obs.group_on else CUT
            st.acked.clear()
            st.last_cmd.clear()
            acts = decide(copy.deepcopy(st), obs, mono, wall, self.params, self.maintenance)
            would = [self._describe(a) for a in acts]
            if would and would != self.would_act:
                _LOGGER.info("Dry run, would do: %s", would)
            self.would_act = would
            return
        self.would_act = []
        if st.ctl != SHUTDOWN:
            self._failed.clear()
        for act in decide(st, obs, mono, wall, self.params, self.maintenance):
            await self._execute(act)

    @staticmethod
    def _describe(act: Action) -> str:
        return f"{act.kind} {act.server or 'group'}: {act.reason}"

    async def _execute(self, act: Action) -> None:
        text = self._describe(act)
        try:
            if act.kind == "shutdown":
                channel = await self.servers[act.server].shutdown()
                self.state.acked.add(act.server)
                text += f" (accepted via {channel})"
            elif act.kind == "wake":
                await self.servers[act.server].power_on()
            elif act.kind == "cut":
                # Look again immediately before cutting.
                fresh = await self._gather()
                if not cut_guard(fresh, self.params):
                    self.last_error = "cut skipped: guard failed on re-check"
                    _LOGGER.warning(self.last_error)
                    return
                await self.ups.set_group(False)
            elif act.kind == "restore":
                await self.ups.set_group(True)
        except (ServerError, UpsError) as exc:
            self.last_error = f"{text} FAILED: {exc}"
            _LOGGER.error(self.last_error)
            # A failed shutdown command repeats every t_resend; say it once.
            if act.kind == "shutdown":
                if act.server in self._failed:
                    return
                self._failed.add(act.server)
            await self._notify(self.last_error)
            return
        self.last_action = text
        _LOGGER.warning("ups_sync: %s", text)
        await self._notify(text)

    async def _notify(self, message: str) -> None:
        self.hass.bus.async_fire(f"{DOMAIN}_action", {"message": message})
        persistent_notification.async_create(self.hass, message, title="UPS sync")
        for target in self.notify_services:
            domain, _, service = target.partition(".")
            try:
                await self.hass.services.async_call(
                    domain, service, {"title": "UPS sync", "message": message}
                )
            except Exception:  # a broken notifier must not stop the controller
                _LOGGER.exception("notify via %s failed", target)
