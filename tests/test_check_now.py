# SPDX-License-Identifier: GPL-3.0-or-later
"""Check for updates, pressed by a person (2026-11-20, "right after
installing, Check for updates does nothing, only the notice comes later").
The press always ends in a result: a background check already running is
joined, and an apt lock held by another install (the first start of a
fresh system: unattended-upgrades, the Store's catalog) is waited for
while the page says "Checking", instead of "Waiting" until the next
timer, six hours later."""
import os

import pytest

from conftest import ru
from test_apt import FakeTools

LOCK_ERR = "E: Could not get lock /var/lib/apt/lists/lock. It is held by process 812 (apt-get)"


class BusyThenFree(FakeTools):
    def __init__(self, busy_runs, **kw):
        super().__init__(simulate="apt-simulate-none.txt", **kw)
        self.busy = busy_runs

    def run(self, cmd, timeout=3600):
        if cmd[:2] == ["apt-get", "update"] and self.busy > 0:
            self.busy -= 1
            self.calls.append(cmd)
            return 100, "", LOCK_ERR
        return super().run(cmd, timeout)


@pytest.fixture
def asked(paths, monkeypatch, tmp_path):
    monkeypatch.setattr(ru, "CHECK_ASKED", str(tmp_path / "run" / "check-asked"))
    naps = []
    monkeypatch.setattr(ru, "nap", naps.append)
    return naps


def test_check_now_marks_the_check_as_asked_before_starting_the_unit(asked, monkeypatch):
    seen = []
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=0: seen.append(
        (cmd, os.path.exists(ru.CHECK_ASKED))) or (0, "", ""))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert ru.main(["raye-update", "check-now"]) == 0
    assert seen == [(["systemctl", "start", "--no-block", "raye-update.service"], True)]


def test_an_asked_check_waits_for_the_apt_lock(asked, monkeypatch):
    tools = BusyThenFree(3)
    monkeypatch.setattr(ru, "run", tools.run)
    os.makedirs(os.path.dirname(ru.CHECK_ASKED))
    open(ru.CHECK_ASKED, "w").close()
    ru.run_auto(ru.State(), "auto", 1000.0)
    st = ru.load()
    assert (st.phase, st.waitReason) == ("idle", "")
    assert st.lastCheck == 1000.0
    assert len(asked) == 3
    # the page keeps saying "Checking" while it waits
    assert not os.path.exists(ru.CHECK_ASKED)       # used once


def test_the_page_shows_checking_while_the_lock_is_waited_for(asked, monkeypatch):
    tools = BusyThenFree(2)
    phases = []
    monkeypatch.setattr(ru, "run", tools.run)
    monkeypatch.setattr(ru, "nap", lambda s: phases.append(ru.load().phase))
    st = ru.check(ru.State(), 5.0, asked=True)
    assert phases == ["checking", "checking"] and st.phase == "idle"


def test_the_timer_alone_still_comes_back_later(asked, monkeypatch):
    monkeypatch.setattr(ru, "run", BusyThenFree(1).run)
    ru.run_auto(ru.State(), "auto", 1000.0)
    st = ru.load()
    assert (st.phase, st.waitReason) == ("waiting", "busy")
    assert asked == []


def test_a_lock_held_for_too_long_ends_in_waiting(asked, monkeypatch):
    monkeypatch.setattr(ru, "run", BusyThenFree(10 ** 6).run)
    st = ru.check(ru.State(), 5.0, asked=True)
    assert (st.phase, st.waitReason) == ("waiting", "busy")
    assert sum(asked) <= ru.ASKED_LOCK_WAIT


def test_an_old_mark_is_not_taken_for_a_new_press(asked, monkeypatch):
    # a mark left from a press before a restart (/run is gone then, but a
    # mark older than the wait is never trusted either)
    monkeypatch.setattr(ru, "run", BusyThenFree(1).run)
    os.makedirs(os.path.dirname(ru.CHECK_ASKED))
    open(ru.CHECK_ASKED, "w").close()
    os.utime(ru.CHECK_ASKED, (1, 1))
    ru.run_auto(ru.State(), "auto", 10_000.0)
    assert ru.load().phase == "waiting"


def test_the_package_ships_the_state_folder():
    # a fresh 1.0 Beta had no /var/lib/raye-update until the first check:
    # Settings could not watch a folder that did not exist, so the page
    # stayed on "Check for updates" while the check ran
    here = os.path.dirname(__file__)
    dirs = open(os.path.join(here, "..", "debian", "raye-update.dirs")).read().split()
    assert "var/lib/raye-update" in dirs
    assert os.path.dirname(ru.STATE) == "/var/lib/raye-update"


def test_raye_update_leaves_apps_to_the_store():
    # system updates only (apt, firmware): Flatpak apps and the components
    # they share update in the Raye Store, automatically or with its Get
    # updates button. Two updaters for one app would fight over its files.
    here = os.path.dirname(__file__)
    code = open(os.path.join(here, "..", "src", "raye_update.py")).read()
    assert '"flatpak"' not in code and "'flatpak'" not in code
