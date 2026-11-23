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
    enter_cut,
    observe,
    stuck,
)

P = Params(load_idle_max=210)
UP = ServerObs(True, True)
OFF = ServerObs(False, False)
BOOTING = ServerObs(False, True)


def obs(
    battery=False,
    low=False,
    charge=94,
    load=400,
    group=True,
    f=UP,
    s=UP,
    grid=True,
):
    return Obs(
        input_present=not battery,
        on_battery=battery,
        low_battery=low,
        runtime_min=150,  # at present load: must never gate anything
        charge_pct=charge,
        load_w=load,
        group_on=group,
        servers={"f": f, "s": s},
        grid_down=grid is False,
        grid_ok=grid is True,
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


def test_group_back_on_after_an_unseen_cut_does_not_shut_servers_down_again():
    """The card acknowledged the cut, then the UPS was unreadable while the
    group came back on. The servers are booting on good power: leave them."""
    st = State()
    t = shut_down(st)
    enter_cut(st)  # what the controller does when the SNMP write succeeds
    assert jump(st, obs(f=BOOTING, s=BOOTING), t + 400) == []
    assert st.ctl == IDLE
    assert step(st, obs(), t + 2000) == []


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


def test_restore_needs_stable_input_and_charge():
    st = State(ctl=CUT)
    off = dict(group=False, f=OFF, s=OFF, load=150)
    assert step(st, obs(**off), 0) == []
    assert step(st, obs(**off), 359) == []
    assert step(st, obs(charge=79, **off), 360) == []
    assert step(st, obs(**off), 360, maintenance=True) == []
    assert kinds(step(st, obs(**off), 360)) == [("restore", None)]
    assert step(st, obs(battery=True, **off), 375) == []
    assert step(st, obs(**off), 390) == []  # hold timer restarted


def test_restore_goes_by_charge_whatever_the_runtime_says():
    """2026-11-20: a grid-trigger shutdown at 94 % charge, the UPS never on
    battery. The runtime scaled to the peak read 20 to 22 with the group
    cut, and the group was never restored. Runtime no longer gates it."""
    st = State(ctl=CUT)
    off = dict(group=False, f=OFF, s=OFF, load=155)
    assert step(st, obs(**off), 0) == []
    assert kinds(step(st, obs(**off), 360)) == [("restore", None)]
    st = State(ctl=CUT)
    p = Params(load_idle_max=210, c_restore=95)
    assert step(st, obs(**off), 0, p) == []
    assert step(st, obs(**off), 3600, p) == []


def test_a_wake_goes_by_charge_too():
    st = State()
    part = dict(f=OFF, s=UP)
    assert step(st, obs(charge=79, **part), 0) == []
    assert step(st, obs(charge=79, **part), 900) == []
    assert kinds(step(st, obs(charge=80, **part), 915)) == [("wake", "f")]


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
    low = obs(group=False, f=OFF, s=OFF, load=150, charge=50)
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
        obs(f=OFF, s=UP, charge=79),
    ):
        st = State()
        step(st, blocked, 0)
        assert step(st, blocked, 600) == []
    st = State()
    step(st, partial, 0)
    assert step(st, partial, 600, maintenance=True) == []


# ---- grid sensor: the UPS still has input, but from a battery upstream ----


def test_grid_down_for_t_grid_shuts_down_while_the_ups_is_still_online():
    st = State()
    assert step(st, obs(grid=False), 0) == []
    assert step(st, obs(grid=False), 299) == []
    acts = step(st, obs(grid=False), 300)
    assert kinds(acts) == [("shutdown", "f"), ("shutdown", "s")]
    assert "grid is down" in acts[0].reason and st.ctl == SHUTDOWN


def test_no_grid_shutdown_before_the_idle_load_is_set():
    """The cut could never be approved, and the servers would sit shut down
    on live outlets. Saving the battery can wait."""
    uncalibrated = Params(load_idle_max=None)
    st = State()
    assert step(st, obs(grid=False), 0, uncalibrated) == []
    assert step(st, obs(grid=False), 900, uncalibrated) == []
    assert st.ctl == IDLE
    st = State()
    # A real outage is never held back by this.
    step(st, obs(grid=False, battery=True), 915, uncalibrated)
    assert kinds(step(st, obs(grid=False, battery=True), 1035, uncalibrated)) == [
        ("shutdown", "f"),
        ("shutdown", "s"),
    ]


def test_reason_names_the_trigger_that_actually_fired():
    """On battery for a moment, but it is the grid timer that expired."""
    st = State()
    step(st, obs(grid=False), 0)
    step(st, obs(grid=False), 270)
    acts = step(st, obs(grid=False, battery=True), 300)
    assert "grid is down" in acts[0].reason


def test_stuck_when_the_grid_sensor_keeps_the_group_off_for_hours():
    st = State(ctl=CUT)
    held = obs(group=False, f=OFF, s=OFF, load=150, grid=False)
    assert step(st, held, 0) == []
    step(st, held, 6 * 3600 - 30)
    assert not stuck(st, held, 6 * 3600 - 30, 0, P)
    step(st, held, 6 * 3600)
    assert stuck(st, held, 6 * 3600, 0, P)
    # With the UPS itself on battery it is simply an outage, not "stuck".
    dark = obs(group=False, f=OFF, s=OFF, load=150, grid=False, battery=True)
    step(st, dark, 6 * 3600 + 15)
    assert not stuck(st, dark, 6 * 3600 + 15, 0, P)


def test_a_silent_grid_sensor_neither_shuts_down_nor_restores():
    """grid=None: no entity is being provided, so there is no evidence."""
    st = State()
    assert step(st, obs(grid=None), 0) == []
    assert step(st, obs(grid=None), 3600) == []
    assert st.ctl == IDLE

    st = State(ctl=CUT)
    off = dict(group=False, f=OFF, s=OFF, load=150)
    assert step(st, obs(grid=None, **off), 0) == []
    assert (
        step(st, obs(grid=None, **off), 3600) == []
    )  # input present, grid unconfirmed
    assert not stuck(st, obs(grid=None, **off), 3600, 0, P)
    step(st, obs(grid=None, **off), 6 * 3600)
    assert stuck(st, obs(grid=None, **off), 6 * 3600, 0, P)
    assert step(st, obs(**off), 6 * 3600 + 375) == []  # six minutes of grid first
    assert kinds(step(st, obs(**off), 6 * 3600 + 405)) == [("restore", None)]

    st = State()
    assert step(st, obs(f=OFF, s=UP, load=300, grid=None), 0) == []
    assert step(st, obs(f=OFF, s=UP, load=300, grid=None), 900) == []  # no wake


def test_grid_timer_does_not_count_unwatched_time():
    st = State()
    jump(st, obs(grid=False), 0)
    assert jump(st, obs(grid=False), 400) == []  # nobody watched in between
    assert step(st, obs(grid=False), 699) == []
    assert kinds(step(st, obs(grid=False), 700)) == [
        ("shutdown", "f"),
        ("shutdown", "s"),
    ]


def test_grid_blip_or_flapping_sensor_does_not_shut_down():
    st = State()
    step(st, obs(grid=False), 0)
    assert step(st, obs(grid=False), 280) == []
    assert step(st, obs(), 290) == []  # one reading with the grid up
    assert step(st, obs(grid=False), 580) == []  # timer started again
    assert st.ctl == IDLE


def test_no_restore_and_no_wake_while_the_grid_is_down():
    """The UPS has input from the upstream battery; that is not "power is back"."""
    st = State(ctl=CUT)
    off = dict(group=False, f=OFF, s=OFF, load=150)
    assert step(st, obs(grid=False, **off), 0) == []
    assert step(st, obs(grid=False, **off), 4000) == []
    # Grid returns: the hold starts now, not when the UPS first had input.
    assert step(st, obs(**off), 4015 + 345) == []
    assert kinds(step(st, obs(**off), 4015 + 375)) == [("restore", None)]

    # With a grid delay longer than the wake delay, wake would come first.
    slow = Params(load_idle_max=210, t_grid=10**6)
    st = State()
    assert step(st, obs(f=OFF, s=UP, load=300, grid=False), 0, slow) == []
    assert step(st, obs(f=OFF, s=UP, load=300, grid=False), 900, slow) == []
    assert kinds(step(st, obs(f=OFF, s=UP, load=300), 915, slow)) == [("wake", "f")]


def test_grid_trigger_then_full_cycle():
    st = State()
    step(st, obs(grid=False), 0)
    step(st, obs(grid=False), 300)
    st.acked |= {"f", "s"}
    dark = obs(grid=False, load=170, f=OFF, s=OFF)
    jump(st, dark, 315)
    assert kinds(step(st, dark, 435)) == [("cut", None)]
    enter_cut(st)
    cut = obs(grid=False, group=False, f=OFF, s=OFF, load=150)
    assert step(st, cut, 3000) == []  # stays off for as long as the grid is out
    back = obs(group=False, f=OFF, s=OFF, load=150)
    assert kinds(step(st, back, 3400)) == [("restore", None)]


def test_maintenance_turns_the_grid_trigger_off_but_not_the_battery_one():
    st = State()
    step(st, obs(grid=False), 0, maintenance=True)
    assert step(st, obs(grid=False), 900, maintenance=True) == []
    step(st, obs(grid=False, battery=True), 915, maintenance=True)
    assert kinds(step(st, obs(grid=False, battery=True), 1035, maintenance=True)) == [
        ("shutdown", "f"),
        ("shutdown", "s"),
    ]


def test_maintenance_still_shuts_down_on_outage():
    st = State()
    step(st, obs(battery=True), 0, maintenance=True)
    assert kinds(step(st, obs(battery=True), 120, maintenance=True)) == [
        ("shutdown", "f"),
        ("shutdown", "s"),
    ]
