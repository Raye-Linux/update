# SPDX-License-Identifier: GPL-3.0-or-later
"""The short list of packages an earlier Raye installed and a later one
replaced (WineHQ's build, after raye-wine): removed at the start-up
install, and only those."""
import pytest

from conftest import ru

WINEHQ = ("winehq-stable", "wine-stable", "wine-stable-amd64", "wine-stable-i386")


class Apt:
    def __init__(self, have, removals=WINEHQ, rc=0):
        self.have, self.removals, self.rc, self.calls = set(have), removals, rc, []

    def run(self, cmd, timeout=0):
        self.calls.append(cmd)
        if cmd[0] == "dpkg-query":
            names = [a for a in cmd[3:]]
            return 0, "".join("%s ii \n" % n for n in names if n in self.have), ""
        if "-s" in cmd:
            return 0, "".join("Remv %s [11.0]\n" % n for n in self.removals), ""
        return self.rc, "", ""

    def installs(self):
        return [c for c in self.calls if c[0] == "apt-get" and "-s" not in c and "install" in c]


def test_winehq_goes_once_raye_wine_is_in(monkeypatch):
    apt = Apt({"raye-wine", *WINEHQ})
    monkeypatch.setattr(ru, "run", apt.run)
    assert ru.remove_obsolete()
    (cmd,) = apt.installs()
    assert cmd[-5:] == ["raye-wine-compat", "winehq-stable-", "wine-stable-",
                        "wine-stable-amd64-", "wine-stable-i386-"]
    assert "--no-download" in cmd and "-y" in cmd


def test_nothing_without_raye_wine(monkeypatch):
    apt = Apt(set(WINEHQ))
    monkeypatch.setattr(ru, "run", apt.run)
    assert not ru.remove_obsolete()
    assert apt.installs() == []


def test_nothing_when_winehq_is_already_gone(monkeypatch):
    apt = Apt({"raye-wine", "raye-wine-compat"})
    monkeypatch.setattr(ru, "run", apt.run)
    assert ru.obsolete_plan() == []
    assert not ru.remove_obsolete()


def test_only_what_is_installed_is_named(monkeypatch):
    apt = Apt({"raye-wine", "raye-wine-compat", "winehq-stable", "wine-stable"})
    monkeypatch.setattr(ru, "run", apt.run)
    assert ru.obsolete_plan() == ["winehq-stable-", "wine-stable-"]


def test_anything_more_to_remove_stops_it(monkeypatch, capsys):
    apt = Apt({"raye-wine", *WINEHQ}, removals=WINEHQ + ("winetricks",))
    monkeypatch.setattr(ru, "run", apt.run)
    assert not ru.remove_obsolete()
    assert apt.installs() == []


def test_the_download_brings_the_replacement(monkeypatch):
    apt = Apt(set(WINEHQ))
    monkeypatch.setattr(ru, "run", apt.run)
    # raye-wine is not in yet, but the pending set brings it
    assert ru.obsolete_plan({"raye-wine"})[:2] == ["raye-wine-compat", "winehq-stable-"]


@pytest.mark.parametrize("name", [n for e in ru.OBSOLETE for n in e["remove"]])
def test_the_list_is_only_what_raye_put_there(name):
    assert name in WINEHQ


def test_start_up_install_swaps_after_the_upgrade(paths, no_tools, monkeypatch):
    from test_install import ready_state
    order = []
    monkeypatch.setattr(ru, "install_packages", lambda cb: order.append("upgrade") or (True, ""))
    monkeypatch.setattr(ru, "remove_obsolete", lambda: order.append("obsolete") or True)
    monkeypatch.setattr(ru, "dpkg_half_configured", lambda: False)
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=0: (0, "", ""))
    st = ready_state()
    ru.arm(st, "restart")
    ru.install_offline(st)
    assert order == ["upgrade", "obsolete"]


def test_no_swap_when_the_upgrade_failed(paths, no_tools, monkeypatch):
    from test_install import ready_state
    order = []
    monkeypatch.setattr(ru, "install_packages", lambda cb: order.append("upgrade") or (False, "x"))
    monkeypatch.setattr(ru, "remove_obsolete", lambda: order.append("obsolete") or True)
    monkeypatch.setattr(ru, "dpkg_half_configured", lambda: False)
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=0: (0, "", ""))
    st = ready_state()
    ru.arm(st, "restart")
    ru.install_offline(st)
    assert order == ["upgrade"]


def fake_proc(tmp_path, exes):
    proc = tmp_path / "proc"
    for pid, exe in enumerate(exes, start=100):
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "exe").symlink_to(exe)
    (proc / "self").mkdir(parents=True, exist_ok=True)
    return str(proc)


def test_left_over_swap_downloads_then_swaps(monkeypatch, tmp_path):
    # the 0.3 release downloaded the set that brought raye-wine in, so the
    # start-up install had no raye-wine-compat to put in
    apt = Apt({"raye-wine", *WINEHQ})
    monkeypatch.setattr(ru, "run", apt.run)
    monkeypatch.setattr(ru, "PROC", fake_proc(tmp_path, ["/usr/bin/plasmashell"]))
    assert ru.swap_left_over()
    fetch, swap = apt.installs()
    assert "-d" in fetch and "--no-download" not in fetch
    assert "--no-download" in swap and swap[-5] == "raye-wine-compat"


def test_left_over_swap_waits_while_old_wine_runs(monkeypatch, tmp_path):
    apt = Apt({"raye-wine", *WINEHQ})
    monkeypatch.setattr(ru, "run", apt.run)
    monkeypatch.setattr(ru, "PROC", fake_proc(tmp_path, ["/opt/wine-stable/bin/wineserver"]))
    assert not ru.swap_left_over()
    assert [c for c in apt.installs() if "--no-download" in c] == []


def test_left_over_swap_only_fetches_when_a_restart_is_due(monkeypatch, tmp_path):
    apt = Apt({"raye-wine", *WINEHQ})
    monkeypatch.setattr(ru, "run", apt.run)
    monkeypatch.setattr(ru, "PROC", fake_proc(tmp_path, []))
    assert not ru.swap_left_over(swap=False)
    (fetch,) = apt.installs()
    assert "-d" in fetch


def test_left_over_swap_does_nothing_once_winehq_is_gone(monkeypatch, tmp_path):
    apt = Apt({"raye-wine", "raye-wine-compat"})
    monkeypatch.setattr(ru, "run", apt.run)
    monkeypatch.setattr(ru, "PROC", fake_proc(tmp_path, []))
    assert not ru.swap_left_over()
    assert apt.installs() == []


def test_the_timer_run_does_the_left_over_swap(paths, no_tools, monkeypatch):
    calls = []
    monkeypatch.setattr(ru, "check", lambda st, now=None: st)
    monkeypatch.setattr(ru, "live_running", lambda: False)
    monkeypatch.setattr(ru, "live_waiting", lambda st: False)
    monkeypatch.setattr(ru, "swap_left_over", lambda swap=True: calls.append(swap) or False)
    st = ru.load()
    st.phase = "idle"
    ru.run_auto(st, "auto", 0)
    st.needsRestart = True
    ru.run_auto(st, "auto", 0)
    assert calls == [True, False]
