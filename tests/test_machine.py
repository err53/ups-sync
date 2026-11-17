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
    peak_sample,
    reserve_minutes,
    stuck,
    update_peak,
)

P = Params(load_idle_max=210)
UP = ServerObs(True, True)
OFF = ServerObs(False, False)
BOOTING = ServerObs(False, True)


def obs(battery=False, low=False, reserve=57, load=400, group=True, f=UP, s=UP):
    return Obs(
        input_present=not battery,
        on_battery=battery,
        low_battery=low,
        runtime_min=150,  # at present load: must never gate anything
        reserve_min=reserve,
        load_w=load,
        group_on=group,
        servers={"f": f, "s": s},
    )


def step(st, o, t, p=P, maintenance=False):
    """Hold reading `o` from the previous step until time t, polling every
    30 s like the real loop. Returns every action emitted on the way."""
    acts = []
    last = st.last_obs
    while last is not None and t - last > 30:
        last += 30
        observe(st, o, last, last)
        acts += decide(st, o, last, last, p, maintenance)
    observe(st, o, t, t)
    return acts + decide(st, o, t, t, p, maintenance)


def jump(st, o, t, p=P, maintenance=False):
    """One reading at time t with nothing in between (a gap in polling)."""
    observe(st, o, t, t)
    return decide(st, o, t, t, p, maintenance)


def kinds(acts):
    """Distinct (kind, server) pairs in order; repeats of a retried command collapse."""
    return list(dict.fromkeys((a.kind, a.server) for a in acts))


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
    acts = step(st, obs(battery=True, low=True), 0)
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
    assert jump(st, dark, t + 15) == []  # first dark reading
    assert step(st, dark, t + 134) == []
    acts = step(st, dark, t + 135)
    assert kinds(acts) == [("cut", None)]
    assert st.ctl == SHUTDOWN  # not CUT until the group is seen off
    step(st, obs(battery=True, load=150, group=False, f=OFF, s=OFF), t + 150)
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
    jump(st, dark, t + 15)
    assert kinds(step(st, dark, t + 135)) == [("cut", None)]

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
    assert kinds(step(st, obs(), t + 800)) == [("shutdown", "f"), ("shutdown", "s")]
    assert st.ctl == SHUTDOWN

def test_abort_when_no_command_was_accepted_and_power_is_back():
    st = State()
    step(st, obs(battery=True), 0)
    step(st, obs(battery=True), 120)  # commands sent, none acked
    assert jump(st, obs(), 135) == []  # input back
    assert ("abort", None) not in kinds(step(st, obs(), 494))
    assert ("abort", None) in kinds(step(st, obs(), 495))
    assert st.ctl == IDLE

def test_abort_after_alarm_when_power_is_back():
    st = State()
    t = shut_down(st)
    assert ("alarm", None) in kinds(step(st, obs(battery=True), t + 1440))
    assert st.ctl == SHUTDOWN
    assert ("abort", None) in kinds(step(st, obs(), t + 1900))
    assert st.ctl == IDLE

def test_reconcile_after_restart():
    """Design change 1: a fresh controller trusts the group, not a flag."""
    st = State()
    assert step(st, obs(battery=True, group=False, f=OFF, s=OFF, load=150), 0) == []
    assert st.ctl == CUT
    step(st, obs(f=BOOTING, s=BOOTING), 15)  # card returned the group itself
    assert st.ctl == IDLE


def test_restore_needs_stable_input_and_reserve():
    st = State(ctl=CUT)
    off = dict(group=False, f=OFF, s=OFF, load=150)
    assert step(st, obs(**off), 0) == []
    assert step(st, obs(**off), 359) == []
    assert step(st, obs(reserve=25, **off), 360) == []
    assert step(st, obs(reserve=None, **off), 360) == []  # ratio never learned
    assert step(st, obs(**off), 360, maintenance=True) == []
    assert kinds(step(st, obs(**off), 360)) == [("restore", None)]
    assert step(st, obs(battery=True, **off), 375) == []
    assert step(st, obs(**off), 390) == []  # hold timer restarted


def test_restore_waits_t_hold_after_the_cut_even_if_input_was_stable_for_days():
    st = State()
    assert step(st, obs(), 0) == []
    assert step(st, obs(), 100000) == []
    off = obs(group=False, f=OFF, s=OFF, load=150)
    assert jump(st, off, 100015) == [] and st.ctl == CUT
    assert step(st, off, 100374) == []
    assert kinds(step(st, off, 100375)) == [("restore", None)]

def test_recover_cuts_dead_outlets_without_needing_input():
    """Design changes 2 and 6."""
    dead = obs(load=170, f=OFF, s=OFF)
    st = State()
    assert step(st, dead, 0) == []
    assert step(st, dead, 599) == []
    assert kinds(step(st, dead, 600)) == [("cut", None)]
    assert step(st, dead, 600, maintenance=True) == []
    assert step(st, obs(load=290, f=OFF, s=OFF), 600) == []
    # On battery the same rule applies; the shutdown rule wins at t_ob and
    # then cuts by the same guard.
    st = State()
    step(st, obs(battery=True, load=170, f=OFF, s=OFF), 0)
    step(st, obs(battery=True, load=170, f=OFF, s=OFF), 120)
    assert kinds(step(st, obs(battery=True, load=170, f=OFF, s=OFF), 720)) == [
        ("cut", None)
    ]


def test_stale_stored_dead_timer_needs_fresh_watching_first():
    """A value loaded from disk cannot cut by itself."""
    dead = obs(load=170, f=OFF, s=OFF)
    st = State(dead_since=-9000.0)  # from storage, "dark for hours"
    assert step(st, dead, 0) == []
    assert step(st, dead, 119) == []
    assert kinds(step(st, dead, 120)) == [("cut", None)]


def test_a_gap_in_readings_restarts_the_timers():
    st = State(ctl=CUT)
    off = obs(group=False, f=OFF, s=OFF, load=150)
    assert jump(st, off, 0) == []
    assert jump(st, off, 400) == []  # 400 s unobserved is not "stable for 400 s"
    assert step(st, off, 759) == []
    assert kinds(step(st, off, 760)) == [("restore", None)]

    st = State()
    dead = obs(load=170, f=OFF, s=OFF)
    jump(st, dead, 0)
    assert jump(st, dead, 605) == []  # not watched in between
    st = State()
    jump(st, obs(battery=True), 0)
    assert jump(st, obs(battery=True), 130) == []  # t_ob needs watching too


def test_stuck_dark_is_reported_when_the_guard_blocks_the_cycle():
    st = State()
    heavy = obs(load=290, f=OFF, s=OFF)
    assert step(st, heavy, 0) == []
    assert step(st, heavy, 600) == []
    assert stuck(st, heavy, 600, 600, P)
    assert not stuck(st, obs(load=170, f=OFF, s=OFF), 600, 600, P)
    assert not stuck(st, obs(load=290, f=UP, s=OFF), 600, 600, P)


def test_stuck_when_a_restore_is_hours_overdue():
    st = State(ctl=CUT)
    low = obs(group=False, f=OFF, s=OFF, load=150, reserve=10)
    assert step(st, low, 0) == []
    assert step(st, low, 6 * 3600 - 30) == []
    assert not stuck(st, low, 6 * 3600 - 30, 0, P)
    step(st, low, 6 * 3600)
    assert stuck(st, low, 6 * 3600, 0, P)


def test_readings_a_minute_apart_still_count_as_continuous():
    """Three of four polls failing must not stop a shutdown or a restore."""
    st = State()
    for t in range(0, 181, 60):
        acts = jump(st, obs(battery=True), t)
    assert st.ctl == SHUTDOWN and kinds(acts) == [("shutdown", "f"), ("shutdown", "s")]

    st = State(ctl=CUT)
    off = obs(group=False, f=OFF, s=OFF, load=150)
    acts = [a for t in range(0, 361, 60) for a in jump(st, off, t)]
    assert kinds(acts) == [("restore", None)]


def test_reserve_is_runtime_scaled_to_the_heaviest_load_seen():
    assert reserve_minutes(150, 150, None) is None
    # Servers off: the UPS says 150 min at 150 W. At a 420 W peak that is 35.
    assert round(reserve_minutes(150, 150, 420)) == 35
    # Never more than the UPS's own figure, and equal to it at or above peak.
    assert reserve_minutes(57, 420, 420) == 57
    assert reserve_minutes(57, 500, 420) == 57
    # Conservative against a linear conversion (150 * 150 / 420 = 53.6).
    assert reserve_minutes(150, 150, 420) < 150 * 150 / 420


def test_peak_counts_only_readings_with_every_server_up():
    assert peak_sample(400, True, True) == 400
    assert peak_sample(170, True, False) is None  # a server is off: standby draw
    assert peak_sample(700, True, False) is None  # a server is booting: surge
    assert peak_sample(150, False, False) is None  # group cut
    assert peak_sample(0, True, True) is None  # a card reporting 0 W


def test_peak_only_rises_and_needs_two_eligible_readings_in_a_row():
    assert update_peak(None, None, 400) is None  # one reading is not enough
    assert update_peak(None, 400, 410) == 400
    assert update_peak(400, 410, 900) == 410  # a single spike is not believed
    assert update_peak(410, 900, 905) == 900  # a sustained one is
    assert update_peak(900, 300, 300) == 900  # never falls
    assert update_peak(400, 400, None) == 400 and update_peak(None, None, None) is None
    assert reserve_minutes(57, 150, 0) is None  # a stored zero is "unknown"


def test_no_power_cycle_before_a_restore_could_ever_be_approved():
    """Set up while the servers are off: leave the outlets live."""
    dead = obs(load=170, f=OFF, s=OFF, reserve=None)
    st = State()
    assert step(st, dead, 0) == []
    assert step(st, dead, 900) == []
    assert st.ctl == IDLE and stuck(st, dead, 900, 900, P) is False
    # An outage shutdown is never held back by this.
    st = State()
    t = shut_down(st)
    jump(st, obs(battery=True, load=170, f=OFF, s=OFF, reserve=None), t + 15)
    assert kinds(
        step(st, obs(battery=True, load=170, f=OFF, s=OFF, reserve=None), t + 135)
    ) == [("cut", None)]


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
        obs(f=OFF, s=UP, reserve=20),
        obs(f=OFF, s=UP, reserve=None),
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
