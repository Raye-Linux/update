# SPDX-License-Identifier: GPL-3.0-or-later
"""The downloaded packages go once they are installed, never before, and
never the ones of updates still waiting for a restart."""
import fcntl
import os

import pytest

from conftest import ru
from test_install import ready_state, offline  # noqa: F401  (fixture)
from test_live import apt, downloaded  # noqa: F401  (fixture)

SET = ["firefox_146.0+build1-0ubuntu1_amd64.deb", "libc6_2.42-1ubuntu2_amd64.deb",
       "libgl1_1.7.0-2_i386.deb", "linux-image-7.0.0-12-generic_7.0.0-12.12_amd64.deb",
       "linux-modules-7.0.0-12-generic_7.0.0-12.12_amd64.deb", "linux-generic_7.0.0.12.12_amd64.deb",
       "nvidia-driver-580_580.105-0ubuntu1_amd64.deb", "raye-wine-compat_0.5.6_amd64.deb"]


@pytest.fixture
def cache(paths):
    d = ru.ARCHIVES
    os.makedirs(os.path.join(d, "partial"))
    for name in SET:
        open(os.path.join(d, name), "w").close()
    open(os.path.join(d, "partial", "vlc_3.0.21_amd64.deb"), "w").close()
    open(os.path.join(d, "lock"), "w").close()
    return d


def debs(d):
    return sorted(f for f in os.listdir(d) if f.endswith(".deb"))


def test_everything_installed_empties_the_cache(cache):
    assert ru.clean_downloads() == len(SET)
    assert debs(cache) == []
    # a download of another apt run and apt's own lock stay
    assert os.listdir(os.path.join(cache, "partial")) == ["vlc_3.0.21_amd64.deb"]
    assert os.path.exists(os.path.join(cache, "lock"))


def test_packages_still_waiting_stay(cache):
    ru.clean_downloads(keep=["libc6", "libgl1:i386", "linux-image-7.0.0-12-generic"])
    assert debs(cache) == ["libc6_2.42-1ubuntu2_amd64.deb", "libgl1_1.7.0-2_i386.deb",
                           "linux-image-7.0.0-12-generic_7.0.0-12.12_amd64.deb"]


def test_nothing_goes_while_another_apt_holds_the_cache(cache):
    with open(os.path.join(cache, "lock"), "w") as held:
        fcntl.lockf(held, fcntl.LOCK_EX)
        pid = os.fork()
        if pid == 0:    # fcntl locks are per process: ask from another one
            os._exit(ru.clean_downloads())
        _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert debs(cache) == sorted(SET)


def test_no_cache_folder_is_not_an_error(paths):
    assert ru.clean_downloads() == 0


# --- after the install at start-up ------------------------------------------

def test_cache_emptied_after_the_install_at_start_up(paths, offline, cache, monkeypatch):
    seen = []
    install = ru.install_packages
    monkeypatch.setattr(ru, "install_packages", lambda on: (seen.append(debs(cache)), install(on))[1])
    st = ready_state()
    ru.arm(st, "restart")
    ru.install_offline(st, now=1.0)
    # the install had every package; they went after it
    assert seen == [sorted(SET)]
    assert debs(cache) == []


def test_failed_install_at_start_up_keeps_the_packages(paths, offline, cache):
    offline["apt_ok"] = False
    st = ready_state()
    ru.arm(st, "restart")
    ru.install_offline(st, now=1.0)
    assert debs(cache) == sorted(SET)


def test_half_done_install_at_start_up_keeps_the_packages(paths, offline, cache):
    offline["apt_ok"] = False
    offline["dpkg_broken"][0] = True
    st = ready_state()
    ru.arm(st, "restart")
    assert ru.install_offline(st, now=1.0) == "restart"
    assert debs(cache) == sorted(SET)


# --- after the install while the PC is in use --------------------------------

def test_install_in_use_keeps_what_waits_for_the_restart(apt, cache, monkeypatch):
    seen = []
    stream = apt.stream
    monkeypatch.setattr(ru, "run_with_status", lambda cmd, cb: (
        seen.append(debs(cache)) if "--no-download" in cmd else None, stream(cmd, cb))[1])
    st = ru.install_live(downloaded(apt))
    assert seen == [sorted(SET)]
    left = {p["name"].split(":", 1)[0] for p in st.pending}
    assert left and "firefox" not in left
    kept = {f.split("_", 1)[0] for f in debs(cache)}
    assert kept == left


def test_install_in_use_of_everything_empties_the_cache(apt, cache):
    apt.plain = apt.pins = "apt-simulate-pinned.txt"
    apt.after = "apt-simulate-none.txt"
    ru.install_live(downloaded(apt))
    assert debs(cache) == []


def test_failed_install_in_use_keeps_the_packages(apt, cache):
    apt.install_rc = 100
    ru.install_live(downloaded(apt))
    assert debs(cache) == sorted(SET)


def test_install_in_use_blocked_by_another_install_keeps_the_packages(apt, cache, monkeypatch):
    st = downloaded(apt)
    monkeypatch.setattr(ru, "run_with_status",
                        lambda cmd, cb: (100, "E: Could not get lock /var/lib/dpkg/lock-frontend"))
    ru.install_live(st)
    assert debs(cache) == sorted(SET)


def test_install_in_use_keeps_the_swap_for_the_restart(apt, cache, monkeypatch):
    # raye-wine waits for the restart: what replaces the old Windows layer
    # with it is downloaded beside it and must still be there then
    monkeypatch.setattr(ru, "obsolete_plan", lambda coming=(): ["raye-wine-compat", "wine-stable-"])
    ru.install_live(downloaded(apt))
    assert "raye-wine-compat_0.5.6_amd64.deb" in debs(cache)
