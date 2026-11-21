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
    assert name in WINEHQ + ("synaptic", "qsynth")


# Synaptic came with the 1.0 Beta image; the Store does its work since
# Beta 2. It goes from PCs that got it with the image, never from one where
# a person installed it.

@pytest.fixture
def history(tmp_path, monkeypatch):
    folder = tmp_path / "apt"
    folder.mkdir()
    monkeypatch.setattr(ru, "APT_HISTORY", str(folder))
    return folder


def test_synaptic_from_the_image_goes(monkeypatch, history):
    (history / "history.log").write_text(
        "Start-Date: 2026-11-19  10:00:00\nCommandline: apt-get upgrade\n"
        "Upgrade: firefox:amd64 (160.0, 161.0), synaptic:amd64 (0.91.3, 0.91.4)\n"
        "End-Date: 2026-11-19  10:01:00\n")
    apt = Apt({"raye-desktop", "synaptic"}, removals=("synaptic",))
    monkeypatch.setattr(ru, "run", apt.run)
    assert ru.obsolete_plan() == ["synaptic-"]
    assert ru.remove_obsolete()
    (cmd,) = apt.installs()
    assert cmd[-1] == "synaptic-"


def test_synaptic_a_person_installed_stays(monkeypatch, history):
    (history / "history.log").write_text(
        "Start-Date: 2026-11-19  10:00:00\nCommandline: apt install synaptic\n"
        "Requested-By: sam (1000)\nInstall: synaptic:amd64 (0.91.3), libept1.7:amd64 (1.2.2, automatic)\n"
        "End-Date: 2026-11-19  10:01:00\n")
    apt = Apt({"raye-desktop", "synaptic"}, removals=("synaptic",))
    monkeypatch.setattr(ru, "run", apt.run)
    assert ru.obsolete_plan() == []
    assert not ru.remove_obsolete()


def test_synaptic_put_back_by_a_person_stays(monkeypatch, history):
    import gzip
    with gzip.open(history / "history.log.2.gz", "wt") as f:
        f.write("Start-Date: 2026-11-19  10:00:00\nCommandline: apt reinstall synaptic\n"
                "Reinstall: synaptic:amd64 (0.91.3)\nEnd-Date: 2026-11-19  10:01:00\n")
    apt = Apt({"raye-desktop", "synaptic"}, removals=("synaptic",))
    monkeypatch.setattr(ru, "run", apt.run)
    assert ru.obsolete_plan() == []


def test_unreadable_history_keeps_synaptic(monkeypatch, history):
    (history / "history.log.1.gz").write_bytes(b"not gzip at all")
    apt = Apt({"raye-desktop", "synaptic"}, removals=("synaptic",))
    monkeypatch.setattr(ru, "run", apt.run)
    assert ru.obsolete_plan() == []


def test_start_up_install_swaps_after_the_upgrade(paths, no_tools, monkeypatch):
    from test_install import ready_state
    order = []
    monkeypatch.setattr(ru, "install_packages", lambda cb, **kw: order.append("upgrade") or (True, ""))
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
    monkeypatch.setattr(ru, "install_packages", lambda cb, **kw: order.append("upgrade") or (False, "x"))
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
    monkeypatch.setattr(ru, "check", lambda st, now=None, **kw: st)
    monkeypatch.setattr(ru, "live_running", lambda: False)
    monkeypatch.setattr(ru, "live_waiting", lambda st: False)
    monkeypatch.setattr(ru, "swap_left_over", lambda swap=True: calls.append(swap) or False)
    st = ru.load()
    st.phase = "idle"
    ru.run_auto(st, "auto", 0)
    st.needsRestart = True
    ru.run_auto(st, "auto", 0)
    assert calls == [True, False]


# Qsynth: fluidsynth (Raye Synth, Beta 2) recommends it, so Raye Update's
# upgrade put it there with fluidsynth. Raye Synth needs no window, and
# Qsynth's own starts on JACK, which Raye does not run: it goes from PCs
# where Raye brought it, never from one where a person chose it.

def test_qsynth_raye_brought_goes(monkeypatch, history):
    (history / "history.log").write_text(
        "Start-Date: 2026-11-21  03:00:00\nCommandline: apt-get -q upgrade --with-new-pkgs\n"
        "Install: fluidsynth:amd64 (2.4.8+dfsg-1, automatic), qsynth:amd64 (1.0.3-1, automatic)\n"
        "Upgrade: raye-desktop:amd64 (0.9, 1.0)\nEnd-Date: 2026-11-21  03:05:00\n")
    apt = Apt({"raye-desktop", "qsynth"}, removals=("qsynth",))
    monkeypatch.setattr(ru, "run", apt.run)
    assert ru.obsolete_plan() == ["qsynth-"]
    assert ru.remove_obsolete()
    (cmd,) = apt.installs()
    assert cmd[-1] == "qsynth-"


def test_qsynth_a_person_installed_stays(monkeypatch, history):
    (history / "history.log").write_text(
        "Start-Date: 2026-11-21  03:00:00\nCommandline: apt-get -q upgrade --with-new-pkgs\n"
        "Install: fluidsynth:amd64 (2.4.8+dfsg-1, automatic), qsynth:amd64 (1.0.3-1, automatic)\n"
        "End-Date: 2026-11-21  03:05:00\n\n"
        "Start-Date: 2026-11-22  10:00:00\nCommandline: apt install qsynth\nRequested-By: sam (1000)\n"
        "Install: qsynth:amd64 (1.0.3-1)\nEnd-Date: 2026-11-22  10:00:05\n")
    apt = Apt({"raye-desktop", "qsynth"}, removals=("qsynth",))
    monkeypatch.setattr(ru, "run", apt.run)
    assert ru.obsolete_plan() == []


def test_qsynth_pulled_by_a_persons_own_install_stays(monkeypatch, history):
    # a person installed something that brought it: their choice, not Raye's
    (history / "history.log").write_text(
        "Start-Date: 2026-11-22  10:00:00\nCommandline: apt install fluidsynth\nRequested-By: sam (1000)\n"
        "Install: qsynth:amd64 (1.0.3-1, automatic)\nEnd-Date: 2026-11-22  10:00:05\n")
    apt = Apt({"raye-desktop", "qsynth"}, removals=("qsynth",))
    monkeypatch.setattr(ru, "run", apt.run)
    assert ru.obsolete_plan() == []


def test_qsynth_with_no_record_stays(monkeypatch, history):
    # nothing says Raye brought it (an image never had it)
    apt = Apt({"raye-desktop", "qsynth"}, removals=("qsynth",))
    monkeypatch.setattr(ru, "run", apt.run)
    assert ru.obsolete_plan() == []
