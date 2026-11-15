# SPDX-License-Identifier: GPL-3.0-or-later
"""Active hours, picked restart times and the automatic restart."""
import pytest

from conftest import ru


@pytest.mark.parametrize("start,end,ok", [
    (8, 17, True), (22, 6, True), (0, 18, True), (6, 0, True),
    (8, 8, False), (0, 19, False), (23, 18, False), (-1, 5, False), (5, 24, False),
    (True, 5, False), ("8", 17, False), (None, 17, False),
])
def test_valid_active_hours(start, end, ok):
    assert ru.valid_active_hours(start, end) is ok


@pytest.mark.parametrize("hour,inside", [(7, False), (8, True), (16, True), (17, False), (23, False)])
def test_in_active_hours_day(hour, inside):
    assert ru.in_active_hours(hour, 8, 17) is inside


@pytest.mark.parametrize("hour,inside", [(21, False), (22, True), (2, True), (5, True), (6, False)])
def test_in_active_hours_across_midnight(hour, inside):
    assert ru.in_active_hours(hour, 22, 6) is inside


def ready(**kw):
    st = ru.State(phase="ready", readyId="abc", needsRestart=True)
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def test_no_automatic_restart_unless_the_owner_wants_it():
    assert ru.restart_decision(ready(), now=0, hour=3, gaming=False) is None


def test_automatic_restart_only_outside_active_hours():
    st = ready(autoRestart=True)
    assert ru.restart_decision(st, now=0, hour=3, gaming=False) == "auto"
    assert ru.restart_decision(st, now=0, hour=10, gaming=False) is None


def test_automatic_restart_needs_mains_and_a_downloaded_set():
    assert ru.restart_decision(ready(autoRestart=True), 0, 3, False, on_ac=False) is None
    st = ru.State(phase="idle", autoRestart=True)
    assert ru.restart_decision(st, 0, 3, False) is None


def test_never_during_a_game():
    st = ready(autoRestart=True, phase="armed", armed={"then": "restart", "at": 10})
    assert ru.restart_decision(st, now=100, hour=3, gaming=True) is None


def test_a_picked_time_restarts_even_in_active_hours():
    st = ready(phase="armed", armed={"then": "restart", "at": 500})
    assert ru.restart_decision(st, now=499, hour=10, gaming=False) is None
    assert ru.restart_decision(st, now=500, hour=10, gaming=False) == "scheduled"


def test_armed_for_the_next_restart_is_not_a_schedule():
    st = ready(phase="armed", armed={"then": "shutdown", "at": 0})
    assert ru.restart_decision(st, now=9e9, hour=10, gaming=False) is None


@pytest.fixture
def root(paths, no_tools, monkeypatch):
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ru, "battery_level", lambda: (True, None))
    monkeypatch.setattr(ru, "is_gaming", lambda: False)
    return no_tools


def test_restart_due_restarts_at_the_picked_time(root, monkeypatch):
    st = ready()
    ru.arm(st, "shutdown", at=100)
    ru.save(st)
    monkeypatch.setattr(ru.time, "time", lambda: 50.0)
    assert ru.main(["raye-update", "restart-due"]) == 0 and root["power"] == []
    monkeypatch.setattr(ru.time, "time", lambda: 150.0)
    assert ru.main(["raye-update", "restart-due"]) == 0
    assert root["power"] == ["restart"]                  # restart into the install
    assert ru.load().armed == {"then": "shutdown", "at": 0}


def test_restart_due_arms_and_restarts_on_its_own(root, monkeypatch, paths):
    import os
    ru.save(ready(autoRestart=True, activeHours={"start": 8, "end": 17}))
    monkeypatch.setattr(ru.time, "time", lambda: 1000.0)
    monkeypatch.setattr(ru.time, "localtime", lambda t=None: type("T", (), {"tm_hour": 3})())
    assert ru.main(["raye-update", "restart-due"]) == 0
    assert root["power"] == ["restart"]
    assert os.readlink(paths["link"]) == paths["target"]
    assert ru.load().armed == {"then": "restart", "at": 0}


def test_restart_due_quiet_in_active_hours(root, monkeypatch):
    ru.save(ready(autoRestart=True))
    monkeypatch.setattr(ru.time, "localtime", lambda t=None: type("T", (), {"tm_hour": 10})())
    assert ru.main(["raye-update", "restart-due"]) == 0
    assert root["power"] == []
