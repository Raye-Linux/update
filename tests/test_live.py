# SPDX-License-Identifier: GPL-3.0-or-later
"""Updates that need no restart install while the PC is in use; only the
ones that do wait for the next start-up."""
import os

import pytest

from conftest import fixture, ru


def pinned(cmd):
    return any(a.startswith("Dir::Etc::Preferences=") for a in cmd)


class Apt:
    """apt-get as raye-update calls it: the plain simulation, the one with
    the pins, the install itself."""

    def __init__(self, plain="apt-simulate.txt", pins="apt-simulate-pinned.txt",
                 after="apt-simulate-after-live.txt", install_rc=0):
        self.plain, self.pins, self.after, self.install_rc = plain, pins, after, install_rc
        self.installed = False
        self.calls, self.streams = [], []
        self.sources = {}           # package -> source, for apt-cache show
        self.interrupted = False    # dpkg was cut: apt refuses until it is finished

    def run(self, cmd, timeout=3600):
        self.calls.append(cmd)
        if cmd[:3] == ["dpkg", "--configure", "-a"]:
            self.interrupted = False
        if cmd[:2] == ["apt-get", "-s"]:
            if self.interrupted:
                return 100, "", "E: dpkg was interrupted, you must manually run 'dpkg --configure -a'"
            if pinned(cmd):
                return 0, fixture(self.pins), ""
            return 0, fixture(self.after if self.installed else self.plain), ""
        if cmd[:2] == ["apt-cache", "show"]:
            return 0, "\n\n".join(f"Package: {n}\nSource: {self.sources[n]}"
                                    for n in cmd[3:] if n in self.sources), ""
        if cmd[:2] == ["fwupdmgr", "get-updates"]:
            return 2, "", ""
        return 0, "", ""

    def stream(self, cmd, on_status):
        self.streams.append(cmd)
        if "-d" in cmd:
            return 0, ""
        on_status("pmstatus", "firefox", 50.0, "Unpacking")
        self.installed = self.install_rc == 0
        return self.install_rc, "dpkg: error processing firefox" if self.install_rc else ""


@pytest.fixture
def apt(paths, no_tools, monkeypatch):
    a = Apt()
    monkeypatch.setattr(ru, "run", a.run)
    monkeypatch.setattr(ru, "run_with_status", a.stream)
    monkeypatch.setattr(ru, "battery_level", lambda: (True, None))
    monkeypatch.setattr(ru, "is_metered", lambda: False)
    monkeypatch.setattr(ru, "is_gaming", lambda: False)
    monkeypatch.setattr(ru, "free_bytes", lambda p: 50 << 30)
    monkeypatch.setattr(ru, "dpkg_half_configured", lambda: a.interrupted)
    return a


def restart_of(state):
    return {p["name"]: p["restart"] for p in state.pending}


def downloaded(apt):
    return ru.download(ru.check(ru.State()))


@pytest.mark.parametrize("name,offline", [
    ("linux-image-7.0.0-12-generic", True), ("linux-modules-extra-7.0.0-12-generic", True),
    ("linux-firmware", True), ("grub-efi-amd64-signed", True), ("shim-signed", True),
    ("nvidia-driver-580", True), ("v4l2loopback-dkms", True), ("intel-microcode", True),
    ("libc6", True), ("libc6:i386", True), ("systemd", True), ("dbus", True), ("libdbus-1-3", True),
    ("libqt6core6t64", True), ("qml6-module-qtquick", True), ("libkf6kiocore6", True),
    ("plasma-workspace", True), ("kwin-wayland", True), ("libgl1-mesa-dri", True),
    ("mesa-vulkan-drivers", True), ("raye-theme", True),
    ("raye-shell", True), ("raye-style", True), ("raye-polkit-agent", True),
    ("xdg-desktop-portal-raye", True), ("libtaskmanager6", True),
    ("libnotificationmanager1", True), ("libkworkspace6-6", True), ("libkpipewire6", True),
    ("liblayershellqtinterface6", True), ("libkwaylandclient6", True),
    ("libpowerdevilcore2", True), ("libkglobalacceld0", True), ("libksysguardsensors2", True),
    ("milou", True), ("libinput10", True),
    ("raye-icon-theme", False), ("raye-sound-theme", False), ("raye-windows-ui", False),
    ("firefox", False), ("raye-settings", False), ("raye-store", False), ("raye-update", False),
    ("libssl3t64", False), ("libc6-dev", False), ("systemd-timesyncd", False),
    ("linux-headers-7.0.0-12-generic", False), ("raye-kernel", False), ("raye-desktop", False),
    ("vlc", False), ("libgl1", False),
])
def test_what_only_installs_at_start_up(name, offline):
    assert bool(ru.OFFLINE_PACKAGES.match(name.split(":", 1)[0])) == offline


def test_raye_kernel_is_not_a_second_kernel():
    assert ru.classify("raye-kernel", ["Raye:resolute"]) == "system"
    assert ru.classify("linux-image-7.0.0-12-generic", ["Ubuntu:26.04/resolute"]) == "kernel"


def test_pins_hold_what_needs_a_restart_where_it_is():
    pending = ru.parse_simulation(fixture("apt-simulate.txt"))
    text = ru.live_pins(pending, base="/nonexistent")
    assert "Package: libc6\nPin: version 2.42-1ubuntu1\nPin-Priority: 1001\n" in text
    assert "Package: linux-image-7.0.0-12-generic\nPin: version *\nPin-Priority: -1\n" in text
    assert "firefox" not in text and "libgl1" not in text


def test_a_library_of_a_desktop_source_waits_for_the_restart(apt):
    # a name nobody listed, built from the same source as plasmashell
    apt.sources = {"firefox": "firefox", "libgl1": "libglvnd"}
    pending = [{"name": "libshellbits3", "from": "6.6.0", "to": "6.6.1", "origin": "",
                "kind": "system"}] + ru.parse_simulation(fixture("apt-simulate.txt"))
    assert "libshellbits3" not in ru.offline_names(pending)
    apt.sources["libshellbits3"] = "plasma-workspace"
    offline = ru.offline_names(pending)
    assert "libshellbits3" in offline and "firefox" not in offline
    assert "Package: libshellbits3\nPin: version 6.6.0\n" in ru.live_pins(pending, "/nonexistent")


def test_pins_keep_the_preferences_already_there(tmp_path):
    base = tmp_path / "preferences"
    base.write_text("Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n")
    text = ru.live_pins(ru.parse_simulation(fixture("apt-simulate.txt")), base=str(base))
    assert text.startswith("Package: *\nPin: origin packages.mozilla.org")


def test_check_says_which_ones_need_a_restart(apt):
    st = ru.check(ru.State())
    assert restart_of(st) == {"firefox": False, "libgl1:i386": False, "libc6": True,
                              "linux-modules-7.0.0-12-generic": True,
                              "linux-image-7.0.0-12-generic": True, "linux-generic": True,
                              "nvidia-driver-580": True}
    assert ru.validate(ru.to_json(st)) == []


def test_an_update_that_drags_in_a_restart_one_waits_too(apt):
    # apt keeps libgl1 back: its new version wants a pinned package
    apt.pins = "apt-simulate-pinned-firefox.txt"
    st = ru.check(ru.State())
    assert restart_of(st)["firefox"] is False and restart_of(st)["libgl1:i386"] is True


@pytest.mark.parametrize("pins", ["apt-simulate.txt", "apt-simulate-remove.txt"])
def test_when_the_pins_do_not_hold_everything_waits_for_the_restart(apt, pins):
    apt.pins = pins
    st = ru.check(ru.State())
    assert all(restart_of(st).values())


def test_a_set_with_a_restart_one_needs_a_restart(apt):
    st = downloaded(apt)
    assert (st.phase, st.needsRestart) == ("ready", True)


def test_a_set_without_one_needs_no_restart(apt):
    apt.plain = apt.pins = "apt-simulate-pinned.txt"
    st = downloaded(apt)
    assert (st.phase, st.needsRestart) == ("ready", False)
    d = ru.to_json(st)
    # what readers of the first version show "Restart" for
    assert d["state"] == "idle" and d["needsRestart"] is False


def test_install_in_use_leaves_only_what_needs_a_restart(apt):
    st = ru.install_live(downloaded(apt), now=900.0)
    install = [c for c in apt.streams if "--no-download" in c]
    assert len(install) == 1 and pinned(install[0]) and install[0][-2:] == ["upgrade", "--with-new-pkgs"]
    assert (st.phase, st.needsRestart, st.count) == ("ready", True, 5)
    assert "firefox" not in restart_of(st) and all(restart_of(st).values())
    assert st.readyId == ru.ready_key(st.pending, [])
    assert st.lastInstall == {"time": 900.0, "count": 2, "firmware": 0, "ok": True}
    assert ru.validate(ru.to_json(st)) == []


def test_install_in_use_of_everything_is_the_end_of_it(apt):
    apt.plain = apt.pins = "apt-simulate-pinned.txt"
    apt.after = "apt-simulate-none.txt"
    st = ru.install_live(downloaded(apt))
    assert (st.phase, st.needsRestart, st.count, st.readyId) == ("idle", False, 0, "")
    assert ru.to_json(st)["state"] == "idle"


def test_install_in_use_shows_its_progress(apt, monkeypatch):
    seen = []
    save_run = ru.save_run
    monkeypatch.setattr(ru, "save_run", lambda s: (seen.append((s.phase, s.installPercent)), save_run(s)))
    ru.install_live(downloaded(apt))
    assert ("installing", 0) in seen and ("installing", 50) in seen


def test_install_in_use_failing_leaves_it_all_for_the_restart(apt):
    apt.install_rc = 100
    st = ru.install_live(downloaded(apt))
    assert (st.phase, st.needsRestart) == ("ready", True)
    assert all(restart_of(st).values()) and "firefox" in restart_of(st)
    assert ["dpkg", "--configure", "-a", "--force-confdef", "--force-confold"] in apt.calls
    assert st.error is None and st.lastInstall["ok"] is False


def test_a_failed_install_in_use_is_not_tried_again(apt, monkeypatch):
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    apt.install_rc = 100
    ru.save(ru.install_live(downloaded(apt)))
    apt.calls.clear()
    # the timer comes back: the pinned simulation offers firefox again
    assert ru.main(["raye-update", "auto"]) == 0
    st = ru.load()
    assert all(restart_of(st).values()) and st.needsRestart
    assert not [c for c in apt.calls if c[0] == "systemctl"]


def test_a_new_set_after_a_failed_one_installs_in_use_again(apt):
    apt.install_rc = 100
    st = ru.install_live(downloaded(apt))
    assert ru.check(st).lastInstall["restartSet"] == st.readyId
    # someone installed the rest by hand: what is left is another set
    apt.plain = apt.pins = "apt-simulate-pinned.txt"
    st = ru.check(st)
    assert restart_of(st) == {"firefox": False, "libgl1:i386": False}


def test_install_in_use_installs_with_the_pins_it_simulated(apt, monkeypatch, paths):
    st = downloaded(apt)
    apt.calls.clear()
    seen = []

    def stream(cmd, on_status):
        pref = next(a.split("=", 1)[1] for a in cmd if a.startswith("Dir::Etc::Preferences="))
        seen.append((pref, open(pref).read()))
        # a check starts meanwhile and works out pins of its own, for a
        # set where nothing is held
        apt.plain = "apt-simulate-pinned.txt"
        ru.check(ru.load())
        assert open(pref).read() == seen[0][1]
        return 0, ""
    monkeypatch.setattr(ru, "run_with_status", stream)
    ru.install_live(st)
    sims = [next(a for a in c if a.startswith("Dir::Etc::Preferences=")).split("=", 1)[1]
            for c in apt.calls if c[:2] == ["apt-get", "-s"] and pinned(c)]
    assert sims[0] == seen[0][0]
    assert len(set(sims)) == len(sims)          # each simulation had a file of its own
    assert "Package: libc6\nPin: version 2.42-1ubuntu1" in seen[0][1]
    assert not os.listdir(os.path.dirname(seen[0][0]))   # nothing left behind


def test_a_cut_install_is_finished_before_apt_is_asked(apt, monkeypatch):
    st = downloaded(apt)
    st.phase = "installing"
    ru.save(st)
    apt.interrupted = True
    apt.calls.clear()
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    assert ru.main(["raye-update", "check"]) == 0
    first = [c[:3] for c in apt.calls if c[0] in ("dpkg", "apt-get")]
    assert first[0] == ["dpkg", "--configure", "-a"]
    assert ru.load().phase == "ready"


def test_a_check_finishes_an_interrupted_dpkg_and_asks_again(apt):
    apt.interrupted = True
    st = ru.check(ru.State())
    assert ["dpkg", "--configure", "-a", "--force-confdef", "--force-confold"] in apt.calls
    assert st.phase == "idle" and st.count == 7 and st.error is None


def test_install_now_finishes_an_interrupted_dpkg_first(apt):
    st = downloaded(apt)
    apt.interrupted = True
    apt.calls.clear()
    st = ru.install_live(st, asked=True)
    repair = apt.calls.index(["dpkg", "--configure", "-a", "--force-confdef", "--force-confold"])
    sim = next(i for i, c in enumerate(apt.calls) if c[:2] == ["apt-get", "-s"])
    assert repair < sim
    assert [c for c in apt.streams if "--no-download" in c]


def test_install_in_use_while_another_install_runs_tries_later(apt, monkeypatch):
    st = downloaded(apt)
    monkeypatch.setattr(ru, "run_with_status",
                        lambda cmd, cb: (100, "E: Could not get lock /var/lib/dpkg/lock-frontend"))
    st = ru.install_live(st)
    assert (st.phase, st.needsRestart) == ("ready", True)
    assert restart_of(st)["firefox"] is False


@pytest.mark.parametrize("asked,machine,waits", [
    (False, {"is_gaming": lambda: True}, True),
    (False, {"is_metered": lambda: True}, True),
    (False, {"battery_level": lambda: (False, 20)}, True),
    (True, {"is_gaming": lambda: True}, False),
    (True, {"is_metered": lambda: True}, False),
    (True, {"battery_level": lambda: (False, 20)}, True),
])
def test_install_in_use_waits_like_the_download(apt, monkeypatch, asked, machine, waits):
    st = downloaded(apt)
    for name, fn in machine.items():
        monkeypatch.setattr(ru, name, fn)
    st = ru.install_live(st, asked=asked)
    installs = [c for c in apt.streams if "--no-download" in c]
    assert bool(installs) != waits


def test_install_now_on_a_low_battery_says_why(apt, monkeypatch):
    st = downloaded(apt)
    monkeypatch.setattr(ru, "battery_level", lambda: (False, 30))
    st = ru.install_live(st, asked=True)
    assert (st.phase, st.waitReason) == ("ready", "battery")
    assert ru.to_json(st)["reason"] == "battery"
    assert ru.validate(ru.to_json(st)) == []
    # plugged in, the next click installs and the reason goes away
    monkeypatch.setattr(ru, "battery_level", lambda: (True, 30))
    st = ru.install_live(st, asked=True)
    assert [c for c in apt.streams if "--no-download" in c]
    assert st.waitReason == "" and ru.to_json(st)["reason"] == ""


def test_an_armed_set_installs_at_the_restart_not_now(apt):
    st = downloaded(apt)
    ru.arm(st, "restart")
    ru.install_live(st)
    assert not [c for c in apt.streams if "--no-download" in c]


def test_auto_starts_the_install_in_use_after_the_download(apt, monkeypatch):
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    assert ru.main(["raye-update", "auto"]) == 0
    assert ["systemctl", "start", "--no-block", "raye-update-live@auto.service"] in apt.calls


def test_auto_without_anything_for_now_starts_nothing(apt, monkeypatch):
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    apt.pins = "apt-simulate-none.txt"
    assert ru.main(["raye-update", "auto"]) == 0
    assert not [c for c in apt.calls if c[0] == "systemctl"]


def test_the_install_button_runs_in_its_own_unit(apt, monkeypatch):
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    assert ru.main(["raye-update", "install-now"]) == 0
    assert ["systemctl", "start", "--no-block", "raye-update-live@now.service"] in apt.calls


def test_check_now_starts_the_check_unit(apt, monkeypatch):
    # through pkexec, which waits for a password as long as it takes
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    assert ru.main(["raye-update", "check-now"]) == 0
    assert ["systemctl", "start", "--no-block", "raye-update.service"] in apt.calls


def test_install_live_command_installs_and_saves(apt, monkeypatch):
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    ru.save(downloaded(apt))
    assert ru.main(["raye-update", "install-live", "now"]) == 0
    st = ru.load()
    assert st.phase == "ready" and "firefox" not in restart_of(st)


def test_one_install_in_use_at_a_time(apt, monkeypatch):
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    ru.save(downloaded(apt))
    with ru.live_lock() as held:
        assert held is not None
        assert ru.live_running()
        assert ru.main(["raye-update", "install-live", "auto"]) == 0
    assert not [c for c in apt.streams if "--no-download" in c]
    assert not ru.live_running()


def test_an_install_in_use_cut_short_is_not_stuck(apt, monkeypatch):
    st = downloaded(apt)
    st.phase = "installing"
    ru.save(st)
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    assert ru.main(["raye-update", "check"]) == 0
    assert ru.load().phase == "ready"


def test_the_install_in_use_survives_an_upgrade_of_this_package():
    """dpkg must never stop the unit the install runs in, so it is not one
    dh_installsystemd knows about."""
    here = os.path.join(os.path.dirname(__file__), "..")
    rules = open(os.path.join(here, "debian", "rules")).read()
    assert "raye-update-live" not in rules
    assert "data/raye-update-live@.service usr/lib/systemd/system" in \
        open(os.path.join(here, "debian", "install")).read()
    unit = open(os.path.join(here, "data", "raye-update-live@.service")).read()
    assert "ExecStart=/usr/libexec/raye-update install-live %i" in unit
    # a restart or shutdown waits for dpkg instead of cutting it
    assert "KillSignal=SIGCONT" in unit


def test_only_the_install_button_is_allowed_without_a_password():
    rules = open(os.path.join(os.path.dirname(__file__), "..", "data", "50-raye-update.rules")).read()
    assert '"raye-update-live@now.service"' in rules
    assert "raye-update-live@auto" not in rules


def test_a_killed_install_in_use_stops_saying_installing(apt, monkeypatch):
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    st = downloaded(apt)
    st.phase, st.installPercent = "installing", 40
    ru.save(st)
    with ru.live_lock() as held:
        # still running: left alone
        assert held is not None and ru.main(["raye-update", "live-ended"]) == 0
        assert ru.load().phase == "installing"
    assert ru.main(["raye-update", "live-ended"]) == 0
    assert (ru.load().phase, ru.load().installPercent) == ("ready", 0)


def test_live_ended_leaves_an_armed_set_alone(apt, monkeypatch):
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    st = downloaded(apt)
    ru.arm(st, "restart")
    st.phase = "installing"
    ru.save(st)
    ru.main(["raye-update", "live-ended"])
    assert ru.load().phase == "installing"


def test_the_live_unit_cleans_up_after_itself():
    unit = open(os.path.join(os.path.dirname(__file__), "..", "data",
                             "raye-update-live@.service")).read()
    assert "ExecStopPost=/usr/libexec/raye-update live-ended" in unit
