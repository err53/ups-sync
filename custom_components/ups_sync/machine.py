"""Pure controller logic. No Home Assistant imports.

This is the production form of the TLA+ models in formal/. The operator names
in comments (DoShut, DoCutOk, DoRecov, DoRest, DoWol, Reconcile) refer to
formal/UpsSync2.tla.

Two rules keep it honest:
* The controller state moves to CUT when the card acknowledges the off
  command or the group is seen off, and back to IDLE only when the group is
  seen on. Nothing else is assumed to have worked.
* Every cut passes cut_guard().
"""

from __future__ import annotations

from dataclasses import dataclass, field

IDLE = "idle"
SHUTDOWN = "shutdown"
CUT = "cut"


@dataclass(frozen=True)
class Params:
    """Timings in seconds, runtime in minutes, load in watts."""

    t_ob: float = 120  # on battery this long -> start shutdown
    t_grid: float = 300  # grid sensor down this long -> start shutdown
    t_grace: float = 120  # all acked and dark this long -> cut
    t_dead: float = 600  # dark without ack this long -> cut
    t_wake: float = 600  # one server down, one up this long -> wake it
    t_hold: float = 360  # input stable this long -> restore
    t_wait: float = 1440  # SHUTDOWN longer than this -> alarm only
    t_resend: float = 60  # minimum gap between shutdown commands to one server
    c_restore: float = 80  # % battery charge to restore a cut group or wake a server
    load_idle_max: float | None = None  # W; None = not calibrated, never cut


@dataclass(frozen=True)
class ServerObs:
    reachable: bool
    chassis_on: bool | None  # None = iDRAC did not answer


@dataclass(frozen=True)
class Obs:
    """One consistent set of readings. Built only when the UPS answered."""

    input_present: bool
    on_battery: bool
    low_battery: bool
    runtime_min: float  # the UPS's estimate at its PRESENT load; display only
    charge_pct: float  # battery charge from the card, in %
    load_w: float
    group_on: bool
    servers: dict[str, ServerObs]
    # Optional grid sensor, for a UPS that still has input during an outage
    # because a battery upstream is carrying it. It can be wrong in either
    # direction; nothing unsafe may follow from that. Two separate facts,
    # because the sensor may also say nothing at all:
    #   grid_down  there is evidence the grid is down  -> may start a shutdown
    #   grid_ok    there is evidence the grid is up     -> may restore or wake
    # With no sensor configured: grid_down False, grid_ok True.
    grid_down: bool = False
    grid_ok: bool = True


@dataclass
class State:
    ctl: str = IDLE
    ob_since: float | None = None
    grid_since: float | None = None
    no_grid_since: float | None = None  # CUT, UPS has input, grid not confirmed
    hold_since: float | None = None
    dark_since: float | None = None  # SHUTDOWN only
    partial_since: float | None = None
    shutdown_since: float | None = None
    seen_dark_since: float | None = None  # dark, as watched by this process
    dead_since: float | None = None  # wall clock, persisted across restarts
    last_obs: float | None = None
    acked: set[str] = field(default_factory=set)
    last_cmd: dict[str, float] = field(default_factory=dict)
    alarm: bool = False


@dataclass(frozen=True)
class Action:
    kind: str  # shutdown | cut | restore | wake | alarm | abort
    server: str | None = None
    reason: str = ""


def all_dark(obs: Obs) -> bool:
    return not any(s.reachable for s in obs.servers.values())


def cut_guard(obs: Obs, p: Params) -> bool:
    """True only if nothing on the group can be running or booting."""
    return (
        obs.group_on
        and p.load_idle_max is not None
        and all_dark(obs)
        and obs.load_w <= p.load_idle_max
        and not any(s.chassis_on for s in obs.servers.values())
    )


# Readings further apart than this do not count as continuous. Equal to the
# age at which the controller reports the UPS as unreadable.
MAX_GAP = 120


def enough_charge(obs: Obs, p: Params) -> bool:
    """May the servers be powered, as far as the battery goes?

    Decided on the card's battery charge, which does not depend on the load.
    The card's runtime figure cannot be used: it is measured at the PRESENT
    load, two to three times too long with the servers off. An earlier
    version scaled it to the heaviest load seen with an exponent of 1.4.
    That holds near the peak but halves the figure from the light load of a
    cut group: on 2026-11-20, at 94 % charge and never on battery, it read
    20 to 22 with the group cut, under the 26 required, and the group was
    never restored. The peak it relied on was also raised for good by any
    extra load on the UPS while the servers ran.

    c_restore % of a full battery must stay at least 26 minutes with the
    servers running: a boot drawing up to twice the running load, then a
    shutdown, then the card's shed margin (formal/RESULTS.md). At 80 % the
    battery must hold 32.5. c_restore must also stay below the highest
    charge the card reaches (93 to 94 % on this install), or nothing is
    ever restored.
    """
    return obs.charge_pct >= p.c_restore


RESTORE_OVERDUE = 6 * 3600


def stuck(st: State, obs: Obs, mono: float, wall: float, p: Params) -> bool:
    """Servers are off and the controller cannot bring them back.

    Idle: every server silent for t_dead, yet the guard refuses the cycle.
    Cut: for hours the UPS has had input, yet no restore: the charge still
    falls short, or the grid sensor has not confirmed the grid in all that
    time. The second may be a real long outage or a sensor that has stopped
    working; either way a person should know.
    """
    if st.ctl == IDLE:
        return (
            obs.group_on
            and all_dark(obs)
            and _held(st.dead_since, wall, p.t_dead)
            and not cut_guard(obs, p)
        )
    return st.ctl == CUT and (
        _held(st.hold_since, mono, RESTORE_OVERDUE)
        or _held(st.no_grid_since, mono, RESTORE_OVERDUE)
    )


def _since(cond: bool, since: float | None, now: float) -> float | None:
    return (since if since is not None else now) if cond else None


def _held(since: float | None, now: float, need: float) -> bool:
    return since is not None and now - since >= need


def enter_cut(st: State) -> None:
    """The group is off: seen off, or the card just acknowledged our command.

    Ending the shutdown episode on the acknowledgement matters when the UPS
    then cannot be read for a while. If the group comes back on in that
    time, a controller still in SHUTDOWN would shut the servers down again
    on good power. From CUT, seeing the group on simply means IDLE.
    """
    st.ctl = CUT
    st.acked.clear()
    st.last_cmd.clear()
    st.shutdown_since = None
    st.alarm = False
    st.hold_since = None  # input must be stable for t_hold AFTER the cut


def observe(st: State, obs: Obs, mono: float, wall: float) -> None:
    """Reconcile to the observed group state and advance the timers."""
    if st.last_obs is not None and mono - st.last_obs > MAX_GAP:
        # Nobody was watching. Nothing has "held" across the gap.
        st.ob_since = st.grid_since = st.hold_since = st.dark_since = None
        st.no_grid_since = None
        st.partial_since = st.seen_dark_since = None
    st.last_obs = mono

    if st.ctl != CUT and not obs.group_on:
        enter_cut(st)
    elif st.ctl == CUT and obs.group_on:
        st.ctl = IDLE
    if st.ctl != SHUTDOWN:
        st.alarm = False

    dark = obs.group_on and all_dark(obs)
    some_down = any(not s.reachable for s in obs.servers.values())
    st.ob_since = _since(obs.on_battery, st.ob_since, mono)
    st.grid_since = _since(obs.grid_down, st.grid_since, mono)
    # "Power is back" means UPS input from the grid, not from a battery upstream.
    st.hold_since = _since(
        obs.input_present and obs.grid_ok and st.ctl != IDLE, st.hold_since, mono
    )
    st.no_grid_since = _since(
        st.ctl == CUT and obs.input_present and not obs.grid_ok,
        st.no_grid_since,
        mono,
    )
    st.seen_dark_since = _since(dark, st.seen_dark_since, mono)
    st.dark_since = _since(st.ctl == SHUTDOWN and dark, st.dark_since, mono)
    st.partial_since = _since(
        obs.group_on and not all_dark(obs) and some_down, st.partial_since, mono
    )
    st.dead_since = _since(dark, st.dead_since, wall)


def decide(
    st: State, obs: Obs, mono: float, wall: float, p: Params, maintenance: bool
) -> list[Action]:
    """Return the actions for this step. Call observe() first."""
    acts: list[Action] = []

    if st.ctl == IDLE:
        # DoShut. The grid-sensor trigger saves the upstream battery for the
        # loads that must stay up. It is an economy, not a safety rule, so
        # maintenance turns it off; the on-battery triggers always apply.
        # It also waits until the idle load is set, so that the cut that
        # follows can be approved: an economy must not leave the servers
        # shut down on live outlets.
        on_battery = obs.on_battery and _held(st.ob_since, mono, p.t_ob)
        on_grid_loss = (
            not maintenance
            and p.load_idle_max is not None
            and _held(st.grid_since, mono, p.t_grid)
        )
        if obs.group_on and (obs.low_battery or on_battery or on_grid_loss):
            st.ctl = SHUTDOWN
            st.shutdown_since = mono
            st.acked.clear()
            st.last_cmd.clear()
            st.dark_since = _since(all_dark(obs), None, mono)
            why = (
                "low battery"
                if obs.low_battery
                else "on battery"
                if on_battery
                else "grid is down, saving the upstream battery"
            )
            return _shutdown_cmds(st, obs, mono, p, why)
        if maintenance:
            return acts
        # DoRecov. dead_since survives restarts and may be stale, so this
        # process must also have watched the darkness itself for t_grace.
        if (
            cut_guard(obs, p)
            and _held(st.dead_since, wall, p.t_dead)
            and _held(st.seen_dark_since, mono, p.t_grace)
        ):
            return [Action("cut", reason="all servers off on live outlets")]
        # DoWol
        if (
            _held(st.partial_since, mono, p.t_wake)
            and obs.input_present
            and obs.grid_ok
            and enough_charge(obs, p)
        ):
            for name, s in obs.servers.items():
                if not s.reachable and s.chassis_on is False:
                    acts.append(Action("wake", name, "off while another server is up"))
            if acts:
                st.partial_since = mono
        return acts

    if st.ctl == SHUTDOWN:
        # DoCutOk
        if cut_guard(obs, p) and (
            (st.acked >= set(obs.servers) and _held(st.dark_since, mono, p.t_grace))
            or _held(st.dark_since, mono, p.t_dead)
        ):
            return [Action("cut", reason="servers shut down")]
        # Abort. Not in the TLA+ model, where commands never fail. Dropping to
        # IDLE is exactly what an HA restart does, and the model lets HA
        # restart at any step, so every verified property still holds.
        if _held(st.hold_since, mono, p.t_hold) and (not st.acked or st.alarm):
            st.ctl = IDLE
            st.alarm = False
            return [Action("abort", reason="power is back and shutdown did not finish")]
        acts = _shutdown_cmds(st, obs, mono, p, "shutdown in progress")
        # DoCutTO is an alarm, never a cut.
        if not st.alarm and _held(st.shutdown_since, mono, p.t_wait):
            st.alarm = True
            acts.append(Action("alarm", reason="shutdown is taking too long"))
        return acts

    # CUT: DoRest
    if (
        not maintenance
        and not obs.group_on
        and obs.input_present
        and _held(st.hold_since, mono, p.t_hold)  # counts only with the grid up
        and enough_charge(obs, p)
    ):
        return [Action("restore", reason="input stable")]
    return acts


def _shutdown_cmds(
    st: State, obs: Obs, mono: float, p: Params, why: str
) -> list[Action]:
    acts = []
    for name, s in obs.servers.items():
        last = st.last_cmd.get(name)
        if s.reachable and (last is None or mono - last >= p.t_resend):
            st.last_cmd[name] = mono
            acts.append(Action("shutdown", name, why))
    return acts
