"""Pure controller logic. No Home Assistant imports.

This is the production form of the TLA+ models in formal/. The operator names
in comments (DoShut, DoCutOk, DoRecov, DoRest, DoWol, Reconcile) refer to
formal/UpsSync2.tla.

Two rules keep it honest:
* The controller state is only ever moved to CUT or back to IDLE by observing
  the outlet group (Reconcile). Decisions emit actions; they never assume the
  action worked.
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
    t_grace: float = 120  # all acked and dark this long -> cut
    t_dead: float = 600  # dark without ack this long -> cut
    t_wake: float = 600  # one server down, one up this long -> wake it
    t_hold: float = 360  # input stable this long -> restore
    t_wait: float = 1440  # SHUTDOWN longer than this -> alarm only
    t_resend: float = 60  # minimum gap between shutdown commands to one server
    e_restore: float = 26  # full-load minutes of reserve to restore or wake
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
    reserve_min: float | None  # runtime with every server running; None = unknown
    load_w: float
    group_on: bool
    servers: dict[str, ServerObs]


@dataclass
class State:
    ctl: str = IDLE
    ob_since: float | None = None
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

# The UPS reports runtime at its PRESENT load. With the servers off that is
# several times what it will be once they run, so it cannot gate a restore.
# reserve = runtime x (load now / peak) ^ CURVE is the runtime at the peak:
# the heaviest load seen while every server was up and running. That is the
# model's unit, "minutes with every server running". Lead-acid runtime falls
# faster than load rises; APC's own tables for this UPS give exponents of
# 1.2 to 1.36, so 1.4 never overstates. The peak only ever goes up.
#
# Readings taken while a server is off or booting are left out on purpose.
# Standby draw would understate the peak (restore too early). A boot surge
# would overstate it for good (never restore). A server answers on the
# network well before its boot is over, so a reading only counts once every
# server has been reachable without a break for PEAK_SETTLE.
# e_restore leaves room for a boot that draws up to about twice as much.
CURVE = 1.4
PEAK_SETTLE = 300


def all_up_since(
    since: float | None, group_on: bool, all_up: bool, mono: float
) -> float | None:
    """When the current unbroken stretch of "group on, every server up" began."""
    return _since(group_on and all_up, since, mono)


def peak_sample(load_w: float, up_since: float | None, mono: float) -> float | None:
    """This reading, if it may count towards the peak."""
    settled = up_since is not None and mono - up_since >= PEAK_SETTLE
    return load_w if settled and load_w > 0 else None


def update_peak(
    peak: float | None, prev_sample: float | None, sample: float | None
) -> float | None:
    """Two eligible readings in a row must agree, so one odd sample cannot
    raise the peak."""
    if prev_sample is None or sample is None:
        return peak
    return max(peak or 0.0, min(prev_sample, sample))


def reserve_minutes(
    runtime_min: float, load_w: float, peak: float | None
) -> float | None:
    """Minutes the UPS would last at the peak load. None = unknown."""
    if not peak:
        return None
    return runtime_min * min(1.0, load_w / peak) ** CURVE


def _enough_reserve(obs: Obs, p: Params) -> bool:
    return obs.reserve_min is not None and obs.reserve_min >= p.e_restore


RESTORE_OVERDUE = 6 * 3600


def stuck(st: State, obs: Obs, mono: float, wall: float, p: Params) -> bool:
    """Servers are off and the controller cannot bring them back.

    Idle: every server silent for t_dead, yet the guard refuses the cycle.
    Cut: input has been stable for hours and the reserve still falls short.
    """
    if st.ctl == IDLE:
        return (
            obs.group_on
            and all_dark(obs)
            and _held(st.dead_since, wall, p.t_dead)
            and not cut_guard(obs, p)
        )
    return st.ctl == CUT and _held(st.hold_since, mono, RESTORE_OVERDUE)


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
        st.ob_since = st.hold_since = st.dark_since = None
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
    st.hold_since = _since(obs.input_present and st.ctl != IDLE, st.hold_since, mono)
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
        # DoShut
        if obs.group_on and (
            obs.low_battery or (obs.on_battery and _held(st.ob_since, mono, p.t_ob))
        ):
            st.ctl = SHUTDOWN
            st.shutdown_since = mono
            st.acked.clear()
            st.last_cmd.clear()
            st.dark_since = _since(all_dark(obs), None, mono)
            why = "low battery" if obs.low_battery else "on battery"
            return _shutdown_cmds(st, obs, mono, p, why)
        if maintenance:
            return acts
        # DoRecov. dead_since survives restarts and may be stale, so this
        # process must also have watched the darkness itself for t_grace.
        # No peak load yet means a restore could never be approved: leave
        # the outlets live so a person can simply power the servers on.
        if (
            cut_guard(obs, p)
            and obs.reserve_min is not None
            and _held(st.dead_since, wall, p.t_dead)
            and _held(st.seen_dark_since, mono, p.t_grace)
        ):
            return [Action("cut", reason="all servers off on live outlets")]
        # DoWol
        if (
            _held(st.partial_since, mono, p.t_wake)
            and obs.input_present
            and _enough_reserve(obs, p)
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
        and _held(st.hold_since, mono, p.t_hold)
        and _enough_reserve(obs, p)
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
