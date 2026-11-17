"""Transition tests. Each maps to a rule or a counterexample in formal/RESULTS.md."""

from custom_components.ups_sync.machine import (
    CUT,
    IDLE,
    SHUTDOWN,
    Obs,
    Params,
    ServerObs,
    State,
    decide,
    observe,
)

P = Params(load_idle_max=210)
UP = ServerObs(True, True)
OFF = ServerObs(False, False)
BOOTING = ServerObs(False, True)


def obs(battery=False, low=False, runtime=57, load=400, group=True, f=UP, s=UP):
    return Obs(
        input_present=not battery,
        on_battery=battery,
        low_battery=low,
        runtime_min=runtime,
        load_w=load,
        group_on=group,
        servers={"f": f, "s": s},
    )


def step(st, o, t, p=P, maintenance=False):
    observe(st, o, t, t)
    return decide(st, o, t, t, p, maintenance)


def kinds(acts):
    return [(a.kind, a.server) for a in acts]


def shut_down(st, t=0):
    """Drive IDLE -> SHUTDOWN with both servers acked. Returns the time."""
    step(st, obs(battery=True), t)
    assert kinds(step(st, obs(battery=True), t + 120)) == [
        ("shutdown", "f"),
        ("shutdown", "s"),
    ]
    st.acked |= {"f", "s"}
    return t + 120


def test_blip_shorter_than_t_ob_does_nothing():
    st = State()
    assert step(st, obs(battery=True), 0) == []
    assert step(st, obs(battery=True), 119) == []
    assert step(st, obs(), 130) == []
    assert step(st, obs(battery=True), 140) == []  # timer restarted
    assert st.ctl == IDLE


def test_on_battery_for_t_ob_starts_shutdown():
    st = State()
    shut_down(st)
    assert st.ctl == SHUTDOWN


def test_low_battery_starts_shutdown_at_once():
    st = State()
    acts = step(st, obs(battery=True, low=True, runtime=15), 0)
    assert kinds(acts) == [("shutdown", "f"), ("shutdown", "s")]


def test_shutdown_only_commands_reachable_servers_and_throttles():
    st = State()
    step(st, obs(battery=True, s=BOOTING), 0)
    assert kinds(step(st, obs(battery=True, s=BOOTING), 120)) == [("shutdown", "f")]
    assert step(st, obs(battery=True, s=BOOTING), 150) == []
    assert kinds(step(st, obs(battery=True), 180)) == [
        ("shutdown", "f"),
        ("shutdown", "s"),
    ]


def test_cut_after_ack_and_grace_then_reconcile_to_cut():
    st = State()
    t = shut_down(st)
    dark = obs(battery=True, load=170, f=OFF, s=OFF)
    assert step(st, dark, t + 60) == []
    assert step(st, dark, t + 170) == []
    assert kinds(step(st, dark, t + 180)) == [("cut", None)]
    assert st.ctl == SHUTDOWN  # not CUT until the group is seen off
    step(st, obs(battery=True, load=150, group=False, f=OFF, s=OFF), t + 195)
    assert st.ctl == CUT


def test_cut_blocked_by_load_chassis_or_missing_calibration():
    """Design change 3: ping cannot tell OFF from BOOT; load and iDRAC can."""
    for blocked, p in (
        (obs(battery=True, load=290, f=OFF, s=OFF), P),
        (obs(battery=True, load=170, f=OFF, s=BOOTING), P),
        (obs(battery=True, load=170, f=OFF, s=OFF), Params(load_idle_max=None)),
    ):
        st = State()
        t = shut_down(st)
        step(st, blocked, t + 60, p)
        assert ("cut", None) not in kinds(step(st, blocked, t + 2000, p))
        assert st.ctl == SHUTDOWN


def test_unknown_idrac_falls_back_to_load_guard():
    st = State()
    t = shut_down(st)
    dark = obs(battery=True, load=170, f=ServerObs(False, None), s=OFF)
    step(st, dark, t + 60)
    assert kinds(step(st, dark, t + 180)) == [("cut", None)]


def test_unacked_servers_need_t_dead():
    st = State()
    step(st, obs(battery=True, f=OFF, s=OFF, load=170), 0)
    assert step(st, obs(battery=True, f=OFF, s=OFF, load=170), 120) == []
    assert st.ctl == SHUTDOWN and not st.acked
    assert step(st, obs(battery=True, f=OFF, s=OFF, load=170), 700) == []
    assert kinds(step(st, obs(battery=True, f=OFF, s=OFF, load=170), 720)) == [
        ("cut", None)
    ]


def test_no_timeout_cut_only_an_alarm():
    """Design change 8."""
    st = State()
    t = shut_down(st)
    acts = step(st, obs(battery=True), t + 1440)
    assert ("alarm", None) in kinds(acts) and ("cut", None) not in kinds(acts)
    assert st.alarm
    assert ("alarm", None) not in kinds(step(st, obs(battery=True), t + 1500))


def test_input_return_does_not_abort_an_acked_shutdown():
    st = State()
    t = shut_down(st)
    for dt in (400, 800):
        assert kinds(step(st, obs(), t + dt)) == [("shutdown", "f"), ("shutdown", "s")]
    assert st.ctl == SHUTDOWN


def test_abort_when_no_command_was_accepted_and_power_is_back():
    st = State()
    step(st, obs(battery=True), 0)
    step(st, obs(battery=True), 120)  # commands sent, none acked
    step(st, obs(), 130)
    assert kinds(step(st, obs(), 500)) == [("abort", None)]
    assert st.ctl == IDLE


def test_abort_after_alarm_when_power_is_back():
    st = State()
    t = shut_down(st)
    step(st, obs(battery=True), t + 1440)  # alarm
    step(st, obs(), t + 1500)
    assert kinds(step(st, obs(), t + 1900)) == [("abort", None)]


def test_reconcile_after_restart():
    """Design change 1: a fresh controller trusts the group, not a flag."""
    st = State()
    assert step(st, obs(battery=True, group=False, f=OFF, s=OFF, load=150), 0) == []
    assert st.ctl == CUT
    step(st, obs(f=BOOTING, s=BOOTING), 15)  # card returned the group itself
    assert st.ctl == IDLE


def test_restore_needs_stable_input_and_runtime():
    st = State(ctl=CUT)
    off = dict(group=False, f=OFF, s=OFF, load=150)
    assert step(st, obs(**off), 0) == []
    assert step(st, obs(**off), 359) == []
    assert step(st, obs(runtime=25, **off), 360) == []
    assert step(st, obs(**off), 360, maintenance=True) == []
    assert kinds(step(st, obs(**off), 360)) == [("restore", None)]
    assert step(st, obs(battery=True, **off), 375) == []
    assert step(st, obs(**off), 390) == []  # hold timer restarted


def test_group_stays_off_for_t_off_even_if_input_has_been_stable_for_days():
    st = State()
    on = obs(load=170, f=OFF, s=OFF)
    step(st, on, 0)
    off = obs(group=False, f=OFF, s=OFF, load=150)
    assert step(st, off, 100000) == [] and st.ctl == CUT
    assert step(st, off, 100059) == []
    assert kinds(step(st, off, 100060)) == [("restore", None)]


def test_recover_cuts_dead_outlets_without_needing_input():
    """Design changes 2 and 6."""
    st = State()
    dead = obs(battery=True, load=170, f=OFF, s=OFF)
    observe(st, dead, 0, 1000)
    st.ob_since = None  # isolate the recover rule from the shutdown rule
    assert decide(st, obs(load=170, f=OFF, s=OFF), 0, 1599, P, False) == []
    assert kinds(decide(st, obs(load=170, f=OFF, s=OFF), 0, 1600, P, False)) == [
        ("cut", None)
    ]
    assert decide(st, obs(load=170, f=OFF, s=OFF), 0, 1600, P, True) == []
    assert decide(st, obs(load=290, f=OFF, s=OFF), 0, 1600, P, False) == []


def test_dead_timer_survives_restart_and_resets_on_any_answer():
    st = State(dead_since=1000.0)  # loaded from storage
    observe(st, obs(load=170, f=OFF, s=OFF), 0, 1700)
    assert st.dead_since == 1000.0
    observe(st, obs(f=UP, s=OFF), 15, 1715)
    assert st.dead_since is None


def test_wake_only_the_server_that_is_off():
    """Design change 7."""
    st = State()
    partial = obs(f=OFF, s=UP, load=300)
    assert step(st, partial, 0) == []
    assert step(st, partial, 599) == []
    assert kinds(step(st, partial, 600)) == [("wake", "f")]
    assert step(st, partial, 615) == []  # timer restarted, no spam
    for blocked in (
        obs(f=BOOTING, s=UP),
        obs(f=ServerObs(False, None), s=UP),
        obs(f=OFF, s=UP, runtime=20),
    ):
        st = State()
        step(st, blocked, 0)
        assert step(st, blocked, 600) == []
    st = State()
    step(st, partial, 0)
    assert step(st, partial, 600, maintenance=True) == []


def test_maintenance_still_shuts_down_on_outage():
    st = State()
    step(st, obs(battery=True), 0, maintenance=True)
    assert kinds(step(st, obs(battery=True), 120, maintenance=True)) == [
        ("shutdown", "f"),
        ("shutdown", "s"),
    ]
