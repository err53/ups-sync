"""Randomised outage simulator driving the real controller logic.

A Python port of the environment in formal/UpsSync2.tla, in real seconds
instead of 2-minute ticks: adversarial input loss, HA crashes, the card's
own load shed and auto-return, server NUT backstops, variable shutdown and
boot times, ping dying before power-off, iDRAC sometimes silent, noisy load
readings, stretches where the UPS cannot be read, and (optionally) failing
commands and humans pressing power buttons.

Like the real UPS, this one reports runtime at its PRESENT load, with a
load-to-runtime curve the controller does not know, and its low-battery flag,
load shed and return thresholds all use that figure. The card may turn the
group back on by itself whenever its own threshold is met.

Checked on every step: no server loses power unless it is off (NoHardCut),
shutdown is only ever started on battery (NoSpurious). Checked at the end:
once power and HA stay up, every server a human did not turn off is running.

UPS_SYNC_SIM_SEEDS=<n> runs more seeds.
"""

import os
import random

import pytest

from custom_components.ups_sync.machine import (
    IDLE,
    Obs,
    Params,
    ServerObs,
    State,
    all_up_since,
    cut_guard,
    decide,
    enter_cut,
    observe,
    peak_sample,
    reserve_minutes,
    update_peak,
)

DT = 15
# Energy `e` is minutes of runtime with both servers running flat out.
# Card settings, in minutes of runtime at the present load: low battery 16,
# shed 3, min return 60 (the most the card accepts).
E_MAX, E_LB, E_SHED, CARD_RETURN = 57.0, 16.0, 3.0, 60.0
# A running server draws anywhere from RUN_MIN_W to RUN_MAX_W and wanders.
# Booting draws far more than the controller ever counts towards its peak,
# and a booting server already answers on the network for the last part of
# its boot.
BASE_W, STANDBY_W = 150, 10
RUN_MIN_W, RUN_MAX_W, SHUT_W, BOOT_W = 80, 160, 150, 280
FULL_W = BASE_W + 2 * RUN_MAX_W
NOISE_W = 15
P = Params(load_idle_max=BASE_W + 2 * STANDBY_W + 40)
assert BASE_W + STANDBY_W + RUN_MIN_W - NOISE_W > P.load_idle_max  # guard can tell
NAMES = ("f", "s")


class HardCut(AssertionError):
    pass


class World:
    def __init__(
        self, seed: int, humans: bool, cmd_fail: float, sensor: bool = False
    ) -> None:
        self.r = random.Random(seed)
        self.humans, self.cmd_fail = humans, cmd_fail
        # Optional grid sensor. It flips at random, unrelated to the UPS
        # input, so it is often wrong in both directions.
        self.sensor, self.grid_down, self.grid_until = sensor, False, 0
        self.t = 0
        self.e = E_MAX
        self.input = True
        self.input_until = self.r.randint(600, 7200)
        self.group, self.off_by = True, "ha"  # ha | shed | dead
        self.curve = self.r.uniform(1.0, 1.3)  # Peukert-like exponent
        self.peak: float | None = None
        self.prev_sample: float | None = None
        self.up_since: float | None = None
        self.boot_tail = dict.fromkeys(NAMES, 0)
        self.prev_load: float | None = None
        self.watts = {n: self.r.uniform(RUN_MIN_W, RUN_MAX_W) for n in NAMES}
        self.blind_until = 0
        self.restores: list[float] = []  # true reserve at each restore
        self.off_at = 0
        self.srv = dict.fromkeys(NAMES, "RUN")
        self.timer = dict.fromkeys(NAMES, 0)
        self.tail = dict.fromkeys(NAMES, 0)
        self.srv_ob = dict.fromkeys(NAMES, 0)
        self.manual: set[str] = set()
        self.ha_down_until = 0
        self.st = State()
        self.alarms = 0
        self.settling = False

    # ---- what the controller can see ------------------------------------

    def powered(self) -> bool:
        return self.group and (self.input or self.e > 0)

    def load(self) -> float:
        if not self.powered():
            return BASE_W
        draw = {"OFF": STANDBY_W, "SHUT": SHUT_W, "BOOT": BOOT_W}
        return BASE_W + sum(
            draw.get(s, self.watts[n]) for n, s in self.srv.items()
        )

    def drain(self) -> float:
        """Energy used per minute on battery, in full-load minutes."""
        return (self.load() / FULL_W) ** self.curve

    def card_runtime(self) -> float:
        """What the UPS reports: minutes left at the present load."""
        return self.e / self.drain()

    def obs(self) -> Obs:
        powered = self.powered()
        servers = {}
        for n in NAMES:
            state = self.srv[n]
            reach = powered and (
                state == "RUN"
                or (state == "SHUT" and self.timer[n] > self.tail[n])
                or (state == "BOOT" and self.timer[n] <= self.boot_tail[n])
            )
            idrac = None if not powered or self.r.random() < 0.1 else state != "OFF"
            servers[n] = ServerObs(reach, idrac)
        load_w = self.load() + self.r.uniform(-NOISE_W, NOISE_W)
        all_up = all(s.reachable for s in servers.values())
        self.up_since = all_up_since(self.up_since, self.group, all_up, self.t)
        sample = peak_sample(load_w, self.up_since, self.t)
        self.peak = update_peak(self.peak, self.prev_sample, sample)
        self.prev_sample = sample
        reserve_load = min(self.prev_load or load_w, load_w)
        self.prev_load = load_w
        return Obs(
            input_present=self.input,
            on_battery=not self.input,
            low_battery=not self.input and self.card_runtime() < E_LB,
            runtime_min=self.card_runtime(),
            reserve_min=reserve_minutes(self.card_runtime(), reserve_load, self.peak),
            load_w=load_w,
            group_on=self.group,
            servers=servers,
            grid_down=self.sensor and self.grid_down,
        )

    # ---- server state changes --------------------------------------------

    def begin_shutdown(self, n: str) -> None:
        self.srv[n] = "SHUT"
        self.timer[n] = self.r.randint(120, 480)
        self.tail[n] = self.r.randint(0, 120)

    def begin_boot(self, n: str) -> None:
        self.srv[n] = "BOOT"
        self.timer[n] = self.r.randint(120, 360)
        self.boot_tail[n] = self.r.randint(0, 120)

    def set_group(self, on: bool, by: str) -> None:
        was = self.powered()
        self.group, self.off_by = on, by
        if not on:
            self.off_at = self.t
        self.power_edge(was)

    def power_edge(self, was: bool) -> None:
        now = self.powered()
        for n in NAMES:
            if was and not now and self.srv[n] != "OFF":
                raise HardCut(f"t={self.t}s {n} lost power while {self.srv[n]}")
            if now and not was and self.srv[n] == "OFF":
                self.begin_boot(n)  # BIOS restore on AC
                self.manual.discard(n)

    # ---- one step ---------------------------------------------------------

    def controller(self) -> None:
        o = self.obs()
        observe(self.st, o, self.t, self.t)
        for act in decide(self.st, o, self.t, self.t, P, False):
            failed = self.r.random() < self.cmd_fail
            if act.kind == "shutdown":
                if self.st.shutdown_since == self.t and not o.on_battery:
                    # Only the grid trigger may start one with the UPS online,
                    # and only when all its conditions hold.
                    held = self.st.grid_since is not None and (
                        self.t - self.st.grid_since >= P.t_grid
                    )
                    assert o.grid_down and held and o.reserve_min is not None, "spurious"
                if not failed:
                    self.st.acked.add(act.server)
                    if self.srv[act.server] in ("RUN", "BOOT"):  # it answered
                        self.begin_shutdown(act.server)
            elif act.kind == "cut" and not failed:
                assert cut_guard(o, P)
                self.set_group(False, "ha")
                enter_cut(self.st)
            elif act.kind == "restore" and not failed:
                assert o.reserve_min >= P.e_restore
                assert not o.grid_down, "restored while the grid sensor read down"
                self.restores.append(self.e)
                self.set_group(True, "ha")
            elif act.kind == "wake" and not failed:
                if self.powered() and self.srv[act.server] == "OFF":
                    self.begin_boot(act.server)
            elif act.kind == "alarm":
                self.alarms += 1

    def step(self) -> None:
        r = self.r
        # Environment: input, HA crashes.
        if not self.settling:
            if self.t >= self.input_until:
                was = self.powered()
                self.input = not self.input
                long = r.random() < 0.3
                self.input_until = self.t + (
                    r.randint(1800, 14400) if long else r.randint(15, 900)
                )
                self.power_edge(was)
            if self.sensor and self.t >= self.grid_until:
                self.grid_down = not self.grid_down
                self.grid_until = self.t + (
                    r.randint(60, 10800) if self.grid_down else r.randint(60, 14400)
                )
            if r.random() < 1 / 1500:
                self.ha_down_until = self.t + r.randint(30, 900)
                self.st = State(dead_since=self.st.dead_since)  # restart
                self.up_since = self.prev_sample = self.prev_load = None
        if not (self.input or self.e > 0):
            self.ha_down_until = max(self.ha_down_until, self.t + 120)

        if not self.settling and r.random() < 1 / 800:
            self.blind_until = self.t + r.randint(15, 420)  # UPS unreadable
        if self.t < self.blind_until:  # a missed reading, as in the controller
            self.up_since = self.prev_sample = self.prev_load = None
        elif self.t >= self.ha_down_until:
            self.controller()

        # Server NUT backstops, polling the card directly.
        for n in NAMES:
            if self.input:
                self.srv_ob[n] = 0
            elif self.powered():
                self.srv_ob[n] += DT
                low = self.card_runtime() < E_LB
                if self.srv[n] == "RUN" and (low or self.srv_ob[n] >= 600):
                    self.begin_shutdown(n)

        # Card: load shed. And return: certain after a full UPS death, and
        # treated as possible at any time otherwise, so nothing here depends
        # on the card's "stay off after load shed" setting.
        if self.group and not self.input and self.card_runtime() < E_SHED:
            self.set_group(False, "shed")
        elif (
            not self.group
            and self.input
            and self.card_runtime() >= CARD_RETURN
            and self.t - self.off_at >= 60  # not in the same instant as the cut
        ):
            if self.off_by == "dead" or (not self.settling and r.random() < 0.01):
                self.set_group(True, "ha")

        # Humans, within the documented assumptions.
        if self.humans and not self.settling and self.input and self.e >= P.e_restore:
            if r.random() < 1 / 400:
                n = r.choice(NAMES)
                if self.srv[n] == "RUN":
                    self.begin_shutdown(n)
                    self.manual.add(n)
                elif self.srv[n] == "OFF" and self.powered():
                    self.begin_boot(n)
                    self.manual.discard(n)

        # Physics.
        for n in NAMES:  # workloads wander
            self.watts[n] = min(
                RUN_MAX_W, max(RUN_MIN_W, self.watts[n] + r.uniform(-4, 4))
            )
        was = self.powered()
        if self.input:
            self.e = min(E_MAX, self.e + 0.5 * DT / 60)
        else:
            self.e = max(0.0, self.e - self.drain() * DT / 60)
            if self.e == 0 and self.group:
                self.off_by = "dead"  # card holds the group off after UPS death
                self.group = False
                self.off_at = self.t
        self.power_edge(was)
        for n in NAMES:
            if self.srv[n] in ("SHUT", "BOOT") and self.powered():
                self.timer[n] -= DT
                if self.timer[n] <= 0:
                    self.srv[n] = "OFF" if self.srv[n] == "SHUT" else "RUN"
        self.t += DT


def run(
    seed: int,
    humans: bool = False,
    cmd_fail: float = 0.0,
    hours: int = 16,
    sensor: bool = False,
) -> World:
    w = World(seed, humans, cmd_fail, sensor)
    while w.t < hours * 3600:
        w.step()
    # Settle: power and HA stay up, nobody interferes.
    w.settling = True
    w.grid_down = False
    was = w.powered()
    w.input = True
    w.power_edge(was)
    for _ in range(4 * 3600 // DT):
        w.step()
    return w


SEEDS = range(int(os.environ.get("UPS_SYNC_SIM_SEEDS", "40")))


@pytest.mark.parametrize("seed", SEEDS)
def test_no_hard_cut_and_recovery(seed):
    w = run(seed)
    assert w.alarms == 0, "alarm fired without human interference"
    assert all(s == "RUN" for s in w.srv.values()), w.srv
    assert w.st.ctl == IDLE and w.group


@pytest.mark.parametrize("seed", SEEDS)
def test_no_hard_cut_with_humans_and_failing_commands(seed):
    w = run(seed, humans=True, cmd_fail=0.3)
    assert all(s == "RUN" for n, s in w.srv.items() if n not in w.manual), w.srv
    assert w.group


@pytest.mark.parametrize("seed", SEEDS)
def test_no_hard_cut_with_a_grid_sensor_that_is_often_wrong(seed):
    """The sensor says "grid down" and "grid up" at random. Servers may be shut
    down more than needed, but never cut while running, and they come back."""
    w = run(seed, sensor=True)
    assert all(s == "RUN" for s in w.srv.values()), w.srv
    assert w.st.ctl == IDLE and w.group
    w = run(seed, humans=True, cmd_fail=0.3, sensor=True)
    assert all(s == "RUN" for n, s in w.srv.items() if n not in w.manual), w.srv
