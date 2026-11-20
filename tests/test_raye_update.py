# SPDX-License-Identifier: GPL-3.0-or-later
import os

from conftest import fixture, ru


def test_parse_simulation_reads_every_install():
    pending = ru.parse_simulation(fixture("apt-simulate.txt"))
    assert [p["name"] for p in pending] == [
        "libc6", "linux-modules-7.0.0-12-generic", "linux-image-7.0.0-12-generic",
        "linux-generic", "firefox", "nvidia-driver-580", "libgl1:i386"]


def test_parse_simulation_versions_and_origin():
    by_name = {p["name"]: p for p in ru.parse_simulation(fixture("apt-simulate.txt"))}
    libc = by_name["libc6"]
    assert (libc["from"], libc["to"]) == ("2.42-1ubuntu1", "2.42-1ubuntu2")
    assert libc["origin"] == "Ubuntu:26.04/resolute-security"   # the security archive wins
    new = by_name["linux-image-7.0.0-12-generic"]
    assert (new["from"], new["to"]) == ("", "7.0.0-12.12")       # a new package has no old version
    assert by_name["firefox"]["to"] == "1:141.0~build2"


def test_parse_simulation_kinds():
    kinds = {p["name"]: p["kind"] for p in ru.parse_simulation(fixture("apt-simulate.txt"))}
    assert kinds == {"libc6": "security", "linux-modules-7.0.0-12-generic": "kernel",
                     "linux-image-7.0.0-12-generic": "kernel", "linux-generic": "kernel",
                     "firefox": "app", "nvidia-driver-580": "driver", "libgl1:i386": "system"}


def test_parse_simulation_ignores_conf_and_remv():
    out = ("Inst a [1] (2 Ubuntu:26.04/resolute-updates [amd64])\n"
           "Conf a (2 Ubuntu:26.04/resolute-updates [amd64])\nRemv oldthing [1.0]\n")
    assert [p["name"] for p in ru.parse_simulation(out)] == ["a"]


def test_parse_simulation_empty():
    assert ru.parse_simulation(fixture("apt-simulate-none.txt")) == []


def test_why_wait_order_and_thresholds():
    assert ru.why_wait(True, None, False, False) == ""
    assert ru.why_wait(False, 39, False, False) == "battery"
    assert ru.why_wait(False, 40, False, False) == ""
    assert ru.why_wait(True, 10, False, False) == ""        # on mains, battery level does not matter
    assert ru.why_wait(True, None, True, False) == "metered"
    assert ru.why_wait(False, 10, True, True) == "game"     # a game wins over everything


def test_is_gaming_follows_the_game_mode_flag(tmp_path, monkeypatch):
    flag = tmp_path / "game-active"
    monkeypatch.setattr(ru.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert not ru.is_gaming(str(flag))
    flag.write_text("")
    assert ru.is_gaming(str(flag))


def _supply(root, name, kind, **files):
    d = os.path.join(root, name)
    os.makedirs(d)
    open(os.path.join(d, "type"), "w").write(kind)
    for k, v in files.items():
        open(os.path.join(d, k), "w").write(v)


def test_battery_level_laptop(tmp_path):
    _supply(tmp_path, "AC", "Mains", online="0")
    _supply(tmp_path, "BAT0", "Battery", capacity="55")
    _supply(tmp_path, "BAT1", "Battery", capacity="30")
    assert ru.battery_level(str(tmp_path)) == (False, 30)


def test_battery_level_desktop(tmp_path):
    _supply(tmp_path, "AC", "Mains", online="1")
    assert ru.battery_level(str(tmp_path)) == (True, None)
    assert ru.battery_level(str(tmp_path / "missing")) == (True, None)


WEEK = 7 * 24 * 3600


def test_pause_sets_the_end_one_to_five_weeks_ahead():
    state = ru.State()
    assert ru.pause(state, 2, now=1000.0)
    assert state.pausedUntil == 1000.0 + 2 * WEEK
    assert state.phase == "paused"


def test_pause_refuses_out_of_range():
    state = ru.State()
    assert not ru.pause(state, 0, now=1000.0)
    assert not ru.pause(state, 6, now=1000.0)
    assert not ru.pause(state, True, now=1000.0)
    assert state.pausedUntil == 0


def test_pause_keeps_an_armed_set_armed():
    state = ru.State(phase="armed", readyId="x", armed={"then": "restart", "at": 0})
    assert ru.pause(state, 1, now=0)
    assert state.phase == "armed"


def test_is_paused_until_the_end_then_not():
    state = ru.State(pausedUntil=5000.0)
    assert ru.is_paused(state, now=4999.0)
    assert not ru.is_paused(state, now=5000.0)


def test_resume_clears_the_pause():
    state = ru.State(phase="paused", pausedUntil=9e12)
    ru.resume(state)
    assert state.pausedUntil == 0
    assert state.phase == "idle"
    state = ru.State(phase="paused", pausedUntil=9e12, readyId="abc")
    ru.resume(state)
    assert state.phase == "ready"       # a set downloaded before the pause is still there


def test_auto_does_nothing_while_paused(paths, monkeypatch):
    ru.save(ru.State(pausedUntil=9e12))
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    called = []
    monkeypatch.setattr(ru, "check", lambda s, now=None: called.append("check") or s)
    assert ru.main(["raye-update", "auto"]) == 0
    assert called == []
    assert ru.load().phase == "paused"


def test_auto_runs_again_once_the_pause_is_over(paths, monkeypatch):
    ru.save(ru.State(phase="paused", pausedUntil=1.0))
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    called = []

    def fake_check(s, now=None):
        called.append("check")
        s.phase = "idle"
        return s
    monkeypatch.setattr(ru, "check", fake_check)
    assert ru.main(["raye-update", "auto"]) == 0
    assert called == ["check"]
    assert ru.load().pausedUntil == 0


def test_auto_brings_an_armed_set_to_the_newest_and_keeps_it_armed(paths, monkeypatch):
    """The owner's rule (2026-11-20): a set waiting for the next start is
    replaced by the newest version; it stays armed (test_always_latest.py)."""
    ru.save(ru.State(phase="armed", readyId="x", armed={"then": "restart", "at": 0}))
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    called = []

    def fake_check(s, now=None, **kw):
        called.append(kw)
        s.phase = "idle"
        return s
    monkeypatch.setattr(ru, "check", fake_check)
    assert ru.main(["raye-update", "auto"]) == 0
    assert len(called) == 1 and called[0].get("firmware") is False
    assert ru.load().phase == "armed" and ru.load().armed == {"then": "restart", "at": 0}


def test_auto_leaves_the_start_up_install_alone_while_it_runs(paths, monkeypatch):
    ru.save(ru.State(phase="installing", readyId="x", armed={"then": "restart", "at": 0}))
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ru, "check", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    assert ru.main(["raye-update", "auto"]) == 0
    assert ru.load().phase == "installing"


def test_a_long_run_never_undoes_what_a_person_set_meanwhile(paths, monkeypatch):
    """auto checks for minutes; an arm or a pause in between must survive its saves."""
    ru.save(ru.State())
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)

    def slow_check(s, now=None):
        # someone picks "Update and restart" and active hours while the check runs
        disk = ru.load()
        disk.phase, disk.readyId = "armed", "abc"
        disk.armed = {"then": "restart", "at": 0}
        disk.activeHours = {"start": 9, "end": 18}
        ru.save(disk)
        s.phase, s.readyId = "ready", "abc"
        return s
    monkeypatch.setattr(ru, "check", slow_check)
    monkeypatch.setattr(ru, "bytes_to_download", lambda: 0)
    assert ru.main(["raye-update", "auto"]) == 0
    st = ru.load()
    assert st.armed == {"then": "restart", "at": 0} and st.phase == "armed"
    assert st.activeHours == {"start": 9, "end": 18}


def test_a_pause_set_during_a_run_wins(paths, monkeypatch):
    ru.save(ru.State())
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)

    def check_then_pause(s, now=None):
        disk = ru.load()
        ru.pause(disk, 1)
        ru.save(disk)
        s.phase = "idle"
        return s
    monkeypatch.setattr(ru, "check", check_then_pause)
    ru.main(["raye-update", "auto"])
    assert ru.load().phase == "paused"
