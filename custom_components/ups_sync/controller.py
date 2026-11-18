"""Glue between Home Assistant, the UPS, the servers and the state machine."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections.abc import Callable
from dataclasses import asdict

from homeassistant.components import persistent_notification
from homeassistant.const import (
    ATTR_RESTORED,
    STATE_OFF,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STALE_SECONDS
from .machine import (
    CUT,
    IDLE,
    MAX_GAP,
    SHUTDOWN,
    Action,
    Obs,
    Params,
    ServerObs,
    State,
    all_up_since,
    cut_guard,
    decide,
    enter_cut,
    peak_sample,
    observe,
    reserve_minutes,
    stuck,
    update_peak,
)
from .servers import Server, ServerError
from .snmp import ApcClient, UpsError, UpsReading

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
        grid_entities: list[str] | None = None,
    ) -> None:
        self.hass = hass
        self.ups = ups
        self.servers = {s.cfg.name: s for s in servers}
        self.params = params
        self.notify_services = notify_services
        self.grid_entities = grid_entities or []
        self.grid_missing: list[str] = []
        self.state = State()
        self.obs: Obs | None = None
        self._read_ok = False  # the latest poll got a UPS reading
        self.armed = False
        self.maintenance = False
        self.would_act: list[str] = []
        self.last_action = ""
        self.last_ok: float | None = None
        self.last_error = ""
        self.peak: float | None = None  # heaviest load seen with every server up
        self._prev_sample: float | None = None
        self._up_since: float | None = None
        self._prev_load: float | None = None
        self._wanted_group: bool | None = None
        self.group_cmd_failed = False
        self.stuck = False
        self._stopped = False
        self._last_note = ""
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
        if self.state.dead_since is not None and self.state.dead_since > time.time():
            self.state.dead_since = None  # the clock stepped backwards
        self.peak = data.get("peak") or None
        self._saved = self._snapshot()

    def _snapshot(self) -> dict:
        return {
            "armed": self.armed,
            "maintenance": self.maintenance,
            "dead_since": self.state.dead_since,
            "peak": self.peak,
        }

    async def _save_if_changed(self) -> None:
        snap = self._snapshot()
        if snap != self._saved:
            self._saved = snap
            await self._store.async_save(snap)

    async def async_set_flag(self, name: str, value: bool) -> None:
        async with self._lock:
            setattr(self, name, value)
            if name == "maintenance" and not value:
                # Whatever was done by hand must be watched afresh.
                self.state.dead_since = self.state.seen_dark_since = None
                self.state.partial_since = self.state.grid_since = None
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
    def fresh(self) -> bool:
        """The readings are current enough to show: the latest poll got
        them, and polling has not stalled. Display only; decisions use
        `stale`, which tolerates a few missed polls."""
        return self._read_ok and not self.stale

    @property
    def problem(self) -> bool:
        return (
            self.stale
            or self.state.alarm
            or self.stuck
            or self.group_cmd_failed
            or not self.calibrated
            # At startup other integrations may simply not have loaded yet.
            or (bool(self.grid_missing) and self.hass.is_running)
        )

    @property
    def calibrated(self) -> bool:
        return self.params.load_idle_max is not None and self.peak is not None

    async def async_stop(self) -> None:
        """Wait for a step in flight, then never act again (unload, reload)."""
        async with self._lock:
            self._stopped = True

    def attributes(self) -> dict:
        attrs = {
            "armed": self.armed,
            "maintenance": self.maintenance,
            "alarm": self.state.alarm,
            "ups_stale": self.stale,
            "calibrated": self.calibrated,
            "stuck_servers_off": self.stuck,
            "waiting_for_grid": bool(
                self.obs and not self.obs.grid_ok and self.state.ctl == CUT
            ),
            "grid_entities_missing": self.grid_missing,
            "group_command_failed": self.group_cmd_failed,
            "peak_load_w": self.peak,
            "acked": sorted(self.state.acked),
            "would_act": self.would_act,
            "last_action": self.last_action,
            "last_error": self.last_error,
        }
        if self.obs:
            attrs.update(asdict(self.obs))
        return attrs

    # ---- main loop -------------------------------------------------------

    def _forget_settling(self) -> None:
        """A reading was missed. A server may have rebooted unseen, so the
        "every server up and settled" stretch starts again."""
        self._up_since = self._prev_sample = self._prev_load = None

    def _grid(self) -> tuple[bool, bool]:
        """(grid_down, grid_ok) from the configured grid entities.

        Each should be something that only works on grid power, such as a
        wall charger's voltage sensor. One entity that is available, not
        "off" and not zero is evidence the grid is up. Evidence that it is
        down needs every entity an integration is actually providing to
        show no life. With none configured the feature is off.

        An entity that no integration is providing says nothing: one Home
        Assistant does not know at all (renamed, deleted), or one it only
        remembers from before a restart. For the latter it writes a
        placeholder "unavailable" state marked `restored` until the owning
        integration has loaded. Saying nothing can neither start a shutdown
        nor allow a restore. Such entities are listed in
        `grid_entities_missing` and raise the problem sensor.
        """
        if not self.grid_entities:
            self.grid_missing = []
            return False, True
        states = [self.hass.states.get(e) for e in self.grid_entities]
        live = [s is not None and not s.attributes.get(ATTR_RESTORED) for s in states]
        self.grid_missing = [
            e for e, ok in zip(self.grid_entities, live, strict=True) if not ok
        ]
        known = [s for s, ok in zip(states, live, strict=True) if ok]
        up = any(self._shows_grid(s.state) for s in known)
        return bool(known) and not up, up

    @staticmethod
    def _shows_grid(state: str) -> bool:
        if state in (STATE_UNAVAILABLE, STATE_UNKNOWN, STATE_OFF, ""):
            return False
        try:
            return float(state) > 0
        except ValueError:
            return True

    async def _gather(self) -> Obs:
        ups: UpsReading
        ups, *probes = await asyncio.gather(
            self.ups.read(),
            *(s.reachable() for s in self.servers.values()),
            *(s.chassis_on() for s in self.servers.values()),
        )
        n = len(self.servers)
        mono = time.monotonic()
        if self.last_ok is not None and mono - self.last_ok > MAX_GAP:
            self._forget_settling()  # a stalled loop counts as a missed reading
        self._up_since = all_up_since(
            self._up_since, ups.group_on, all(probes[:n]), mono
        )
        sample = peak_sample(ups.load_w, self._up_since, mono)
        self.peak = update_peak(self.peak, self._prev_sample, sample)
        self._prev_sample = sample
        # The lower of the last two loads, in case the runtime figure lags.
        load_for_reserve = min(self._prev_load or ups.load_w, ups.load_w)
        grid_down, grid_ok = self._grid()
        self._prev_load = ups.load_w
        return Obs(
            input_present=ups.input_present,
            on_battery=ups.on_battery,
            low_battery=ups.low_battery,
            runtime_min=ups.runtime_min,
            reserve_min=reserve_minutes(ups.runtime_min, load_for_reserve, self.peak),
            load_w=ups.load_w,
            group_on=ups.group_on,
            servers={
                name: ServerObs(probes[i], probes[n + i])
                for i, name in enumerate(self.servers)
            },
            grid_down=grid_down,
            grid_ok=grid_ok,
        )

    async def async_step(self, _now=None) -> None:
        if self._lock.locked():
            return  # previous step still running
        async with self._lock:
            if self._stopped:
                return
            try:
                obs = await self._gather()
            except UpsError as exc:
                # No reading, no decision. Timers hold; the alarm sensor
                # turns on once the reading is STALE_SECONDS old.
                self.last_error = f"UPS: {exc}"
                self._read_ok = False
                _LOGGER.warning("UPS read failed: %s", exc)
                self._forget_settling()
                self._notify_listeners()
                return
            self.obs = obs
            self._read_ok = True
            self.last_ok = time.monotonic()
            await self._decide_and_act(obs)
            await self._save_if_changed()
        self._notify_listeners()

    async def _decide_and_act(self, obs: Obs) -> None:
        mono, wall = time.monotonic(), time.time()
        st = self.state
        observe(st, obs, mono, wall)
        self.stuck = not self.maintenance and stuck(st, obs, mono, wall, self.params)
        if obs.group_on == self._wanted_group:
            self.group_cmd_failed = False  # it got there, by us or by hand
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
                try:
                    safe = cut_guard(await self._gather(), self.params)
                except UpsError:
                    safe = False
                if not safe:
                    self.last_error = "cut skipped: guard failed on re-check"
                    _LOGGER.warning(self.last_error)
                    return
                self._wanted_group = False
                await self.ups.set_group(False)
                self.group_cmd_failed = False
                enter_cut(self.state)
            elif act.kind == "restore":
                self._wanted_group = True
                await self.ups.set_group(True)
                self.group_cmd_failed = False
        except (ServerError, UpsError) as exc:
            self.last_error = f"{text} FAILED: {exc}"
            _LOGGER.error(self.last_error)
            if act.kind in ("cut", "restore"):
                self.group_cmd_failed = True
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
        if message == self._last_note:
            return  # a retried command says the same thing every poll
        self._last_note = message
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
