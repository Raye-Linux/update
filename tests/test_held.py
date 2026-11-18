# SPDX-License-Identifier: GPL-3.0-or-later
"""Updates apt keeps back, phases or could not download: they wait quietly
for the next round, they never stop or fail the rest."""

import pytest

from conftest import fixture, ru
from test_live import Apt, restart_of

PHASED = "APT::Get::Never-Include-Phased-Updates=true"


def phased_off(cmd):
    return PHASED in cmd


def from_cache(cmd):
    return "--no-download" in cmd and "--fix-missing" in cmd


class CachedApt(Apt):
    """The same apt, plus what the cache can install: `cached` answers a
    simulation run with --no-download --fix-missing."""

    def __init__(self, cached="apt-simulate.txt", download_rc=0, download_out="", **kw):
        super().__init__(**kw)
        self.cached, self.download_rc, self.download_out = cached, download_rc, download_out
        self.pins_everything = None     # a pinned simulation not limited to the cache

    def run(self, cmd, timeout=3600):
        pinned = any(a.startswith("Dir::Etc::Preferences=") for a in cmd)
        if cmd[:2] == ["apt-get", "-s"] and pinned and self.pins_everything and not from_cache(cmd):
            self.calls.append(cmd)
            return 0, fixture(self.pins_everything), ""
        if cmd[:2] == ["apt-get", "-s"] and from_cache(cmd) and not any(
                a.startswith("Dir::Etc::Preferences=") for a in cmd):
            self.calls.append(cmd)
            return 0, fixture(self.after if self.installed else self.cached), ""
        if cmd[:2] == ["apt-get", "--print-uris"]:
            self.calls.append(cmd)
            return 0, ("" if self.installed else fixture("apt-print-uris.txt")), ""
        return super().run(cmd, timeout)

    def stream(self, cmd, on_status):
        if "-d" in cmd:
            self.streams.append(cmd)
            return self.download_rc, self.download_out
        return super().stream(cmd, on_status)


@pytest.fixture
def capt(paths, no_tools, monkeypatch):
    a = CachedApt()
    monkeypatch.setattr(ru, "run", a.run)
    monkeypatch.setattr(ru, "run_with_status", a.stream)
    monkeypatch.setattr(ru, "battery_level", lambda: (True, None))
    monkeypatch.setattr(ru, "is_metered", lambda: False)
    monkeypatch.setattr(ru, "is_gaming", lambda: False)
    monkeypatch.setattr(ru, "free_bytes", lambda p: 50 << 30)
    monkeypatch.setattr(ru, "dpkg_half_configured", lambda: False)
    monkeypatch.setattr(ru, "relaunch_updated_apps", lambda names: [])
    monkeypatch.setattr(ru, "restart_user_units", lambda names: [])
    return a


# --- phased updates ------------------------------------------------------------

def test_every_apt_run_waits_for_the_whole_phase(capt):
    st = ru.download(ru.check(ru.State()))
    ru.install_live(st)
    apt_runs = [c for c in capt.calls + capt.streams if c[0] == "apt-get" and "upgrade" in c]
    assert apt_runs and all(phased_off(c) for c in apt_runs)


def test_the_size_to_download_is_worked_out_the_same_way(monkeypatch):
    seen = []
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=0: seen.append(cmd) or (0, "", ""))
    ru.bytes_to_download()
    assert phased_off(seen[0]) and seen[0][:2] == ["apt-get", "--print-uris"]


def test_the_install_at_start_up_waits_for_the_whole_phase_too(monkeypatch):
    streams = []
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=0: (0, "", ""))
    monkeypatch.setattr(ru, "run_with_status", lambda cmd, cb: streams.append(cmd) or (0, ""))
    ru.install_packages(lambda p: None)
    assert phased_off(streams[0])


# --- installing only what was downloaded ----------------------------------------

def test_the_install_at_start_up_takes_what_is_downloaded(monkeypatch):
    runs, streams = [], []
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=0: runs.append(cmd) or (0, "", ""))

    def fake_stream(cmd, cb):
        streams.append(cmd)
        return (1, "boom") if len(streams) == 1 else (0, "")
    monkeypatch.setattr(ru, "run_with_status", fake_stream)
    ok, _ = ru.install_packages(lambda p: None)
    assert ok
    # a package that turned up after the download (or lost its file) waits
    # for next time instead of failing the whole install
    assert all(from_cache(c) for c in streams)
    recovery = [c for c in runs if c[-2:] == ["-f", "install"]]
    assert recovery and from_cache(recovery[0])


def test_the_install_in_use_takes_what_is_downloaded(capt):
    st = ru.download(ru.check(ru.State()))
    capt.calls.clear()
    ru.install_live(st)
    install = [c for c in capt.streams if "--no-download" in c]
    assert install and from_cache(install[0])
    pinned_sims = [c for c in capt.calls if c[:2] == ["apt-get", "-s"]
                   and any(a.startswith("Dir::Etc::Preferences=") for a in c)]
    assert pinned_sims and all(from_cache(c) for c in pinned_sims)


def test_a_check_plans_with_everything_not_only_the_cache(capt):
    ru.check(ru.State())
    sims = [c for c in capt.calls if c[:2] == ["apt-get", "-s"]]
    assert sims and not any("--no-download" in c for c in sims)


def test_an_update_found_after_the_download_does_not_hold_the_rest(capt):
    # the plain simulation now offers one more package nobody downloaded;
    # what the cache can install is still the downloaded set
    st = ru.download(ru.check(ru.State()))
    capt.pins_everything = "apt-simulate-pinned-extra.txt"
    st = ru.install_live(st)
    assert st.lastInstall["ok"] is True and st.lastInstall["count"] == 2
    assert restart_of(st) and "firefox" not in restart_of(st)


# --- a download that only partly worked -------------------------------------------

FAILED_ONE = "E: Failed to fetch https://archive.ubuntu.com/ubuntu/pool/main/l/libc6.deb  404  Not Found"


def test_a_file_that_failed_to_download_leaves_the_rest_ready(capt):
    capt.download_rc, capt.download_out = 100, FAILED_ONE
    capt.cached = "apt-simulate-pinned.txt"     # firefox and libgl1 made it
    st = ru.download(ru.check(ru.State()))
    assert (st.phase, st.error) == ("ready", None)
    assert sorted(p["name"] for p in st.pending) == ["firefox", "libgl1:i386"]
    assert st.count == 2 and st.readyId == ru.ready_key(st.pending, st.firmware)
    assert not st.kernelChange
    assert ru.validate(ru.to_json(st)) == []


def test_the_missing_ones_come_back_at_the_next_check(capt):
    capt.download_rc, capt.download_out = 100, FAILED_ONE
    capt.cached = "apt-simulate-pinned.txt"
    st = ru.download(ru.check(ru.State()))
    ready = st.readyId
    st = ru.check(st)
    # the whole set again: a new key, so the next auto run downloads the rest
    assert st.count == 7 and st.readyId == "" and ready


def test_a_download_where_nothing_arrived_is_still_an_error(capt):
    capt.download_rc, capt.download_out = 100, FAILED_ONE
    capt.cached = "apt-simulate-none.txt"
    st = ru.download(ru.check(ru.State()))
    assert st.phase == "error" and st.error["code"] == "server" and st.readyId == ""


def test_a_full_disk_is_never_waved_through(capt):
    capt.download_rc, capt.download_out = 100, "E: You don't have enough free space in /var/cache/apt/archives/."
    st = ru.download(ru.check(ru.State()))
    assert st.phase == "error" and st.error["code"] == "no-space"


def test_a_busy_package_manager_still_waits(capt):
    capt.download_rc, capt.download_out = 100, "E: Could not get lock /var/cache/apt/archives/lock"
    st = ru.download(ru.check(ru.State()))
    assert (st.phase, st.waitReason, st.error) == ("waiting", "busy", None)


def test_a_partial_set_keeps_its_restart_marks_from_the_cache(capt):
    capt.download_rc, capt.download_out = 100, FAILED_ONE
    capt.cached = "apt-simulate.txt"
    st = ru.download(ru.check(ru.State()))
    assert restart_of(st)["firefox"] is False and restart_of(st)["libc6"] is True


# --- what needs a restart, package by package (point 74) ----------------------------

@pytest.mark.parametrize("name,offline", [
    ("grub-theme-raye", False), ("grub-theme-starfield", False),
    ("grub-common", True), ("grub-pc", True), ("grub-efi-amd64-bin", True), ("grub2-common", True),
    ("qml6-module-org-raye-account", False), ("qml6-module-org-raye-setup", False),
    ("qml6-module-qtquick", True), ("qml6-module-org-kde-kirigami", True),
    ("raye-polkit-agent", False), ("raye-account", False),
    ("raye-theme", True), ("raye-shell", True), ("raye-style", True),
])
def test_what_needs_a_restart_after_the_review(name, offline):
    assert bool(ru.OFFLINE_PACKAGES.match(name)) == offline


def test_settings_opens_again_when_its_account_module_changed():
    assert ru.RELAUNCH["qml6-module-org-raye-account"] == ("raye-settings", "systemsettings")


def test_the_admin_prompt_and_the_account_service_start_again(tmp_path):
    ran = []
    users = ["ana", "bo"]
    done = ru.restart_user_units({"raye-polkit-agent", "firefox", "raye-account:amd64"},
                                 runner=lambda cmd, timeout=0: ran.append(cmd) or (0, "", ""),
                                 users=users)
    assert done == users
    assert ran[0] == ["systemctl", "--machine=ana@.host", "--user", "try-restart",
                      "raye-accountd.service", "raye-polkit-agent.service"]
    assert len(ran) == 2


def test_nothing_to_start_again_runs_nothing():
    ran = []
    assert ru.restart_user_units({"firefox"}, runner=lambda *a, **k: ran.append(a),
                                 users=["ana"]) == []
    assert ran == []


def test_session_users_are_the_people_signed_in(tmp_path):
    (tmp_path / "0").mkdir()
    (tmp_path / "1000").mkdir()
    (tmp_path / "1001").mkdir()
    (tmp_path / "notes").mkdir()
    names = {1000: "ana", 1001: "bo"}
    assert ru.session_users(str(tmp_path), name_of=names.__getitem__) == ["ana", "bo"]


def test_the_units_start_again_after_an_install_in_use(capt, monkeypatch):
    seen = []
    monkeypatch.setattr(ru, "restart_user_units", lambda names: seen.append(set(names)))
    ru.install_live(ru.download(ru.check(ru.State())))
    assert seen == [{"firefox", "libgl1"}]


# --- progress, as Raye Update shows it (point 42) -------------------------------------

def test_a_download_says_its_speed_and_time_left(paths, monkeypatch):
    t = [0.0]
    monkeypatch.setattr(ru, "clock", lambda: t[0])
    st = ru.State(totalBytes=100 << 20)
    meter = ru.SpeedMeter(st)
    for second, done in enumerate([0, 10, 20, 30, 40], start=0):
        t[0] = float(second)
        meter.update(done << 20)
    assert st.bytesPerSecond == 10 << 20
    assert st.secondsLeft == 6
    meter.stop()
    assert (st.bytesPerSecond, st.secondsLeft) == (0, 0)


def test_a_stalled_download_says_nothing_rather_than_forever(paths, monkeypatch):
    t = [0.0]
    monkeypatch.setattr(ru, "clock", lambda: t[0])
    st = ru.State(totalBytes=100 << 20)
    meter = ru.SpeedMeter(st)
    meter.update(0)
    t[0] = 5.0
    meter.update(0)
    assert (st.bytesPerSecond, st.secondsLeft) == (0, 0)


def test_the_download_writes_speed_and_ends_without_it(capt, monkeypatch):
    seen = []

    def stream(cmd, on_status):
        on_status("dlstatus", "1", 50.0, "Retrieving file 1 of 2")
        seen.append(ru.load())
        return 0, ""
    capt.stream = stream
    monkeypatch.setattr(ru, "run_with_status", stream)
    monkeypatch.setattr(ru, "bytes_to_download", lambda: 1000)
    st = ru.download(ru.check(ru.State()))
    assert seen and seen[0].phase == "downloading"
    assert (st.bytesPerSecond, st.secondsLeft) == (0, 0)


def test_a_check_says_the_size_before_anything_downloads(capt, monkeypatch):
    monkeypatch.setattr(ru, "bytes_to_download", lambda: 4242)
    st = ru.check(ru.State())
    assert (st.phase, st.totalBytes, st.downloadedBytes) == ("idle", 4242, 0)


def test_a_check_shows_how_far_it_got(capt, monkeypatch):
    seen = []

    def stream(cmd, on_status, **kw):
        if cmd[:2] == ["apt-get", "update"]:
            for pct in (0.0, 30.0, 20.0, 80.0):
                on_status("dlstatus", "1", pct, "Retrieving file 1 of 4")
                seen.append(ru.load().checkPercent)
            return 0, "Hit:1 http://archive.ubuntu.com/ubuntu resolute InRelease\n"
        return capt.stream(cmd, on_status)
    monkeypatch.setattr(ru, "run_with_status", stream)
    monkeypatch.setattr(ru, "CHECK_PROGRESS", True)
    st = ru.check(ru.State())
    # never goes back, and the finished check does not keep a number
    assert seen == [0, 30, 30, 80] and st.checkPercent == 0
    assert st.count == 7


def test_each_update_says_where_it_is_while_it_installs(capt, monkeypatch):
    states = []
    save_run = ru.save_run

    def stream(cmd, on_status):
        if "-d" in cmd:
            return 0, ""
        on_status("pmstatus", "firefox:amd64", 10.0, "Preparing firefox (amd64)")
        states.append({p["name"]: p.get("state") for p in ru.load().pending})
        on_status("pmstatus", "firefox:amd64", 60.0, "Installed firefox (1:141.0~build2)")
        on_status("pmerror", "libgl1:i386", 70.0, "dpkg: error")
        states.append({p["name"]: p.get("state") for p in ru.load().pending})
        capt.installed = True
        return 0, ""
    monkeypatch.setattr(ru, "run_with_status", stream)
    monkeypatch.setattr(ru, "save_run", lambda s: save_run(s))
    st = ru.install_live(ru.download(ru.check(ru.State())), now=5.0)
    assert states[0]["firefox"] == "installing" and states[0]["libgl1:i386"] == "queued"
    assert states[0]["libc6"] is None          # waits for the restart, not part of this run
    assert states[1]["firefox"] == "installed" and states[1]["libgl1:i386"] == "failed"
    assert st.lastInstall["packages"] == ["firefox", "libgl1:i386"]
    assert all("state" not in p for p in st.pending)
    assert ru.validate(ru.to_json(st)) == []


def test_new_keys_are_checked_and_optional(paths):
    good = ru.to_json(ru.State(bytesPerSecond=5, secondsLeft=3, checkPercent=40))
    assert ru.validate(good) == []
    for key, value in (("bytesPerSecond", -1), ("secondsLeft", "x"), ("checkPercent", 101)):
        assert ru.validate(dict(good, **{key: value}))
    old = ru.to_json(ru.State())
    for key in ("bytesPerSecond", "secondsLeft", "checkPercent"):
        del old[key]
    assert ru.validate(old) == []
    bad = ru.to_json(ru.State(pending=[{"name": "a", "from": "", "to": "1", "origin": "",
                                        "kind": "app", "state": "dancing"}]))
    assert ru.validate(bad)


def test_status_lines_find_their_update_with_or_without_the_architecture():
    pending = [{"name": "libgl1:i386"}, {"name": "libgl1"}, {"name": "firefox"}]
    assert ru._entry_of(pending, "libgl1:amd64") is pending[1]
    assert ru._entry_of(pending, "libgl1:i386") is pending[0]
    assert ru._entry_of(pending, "firefox:amd64") is pending[2]
    assert ru._entry_of(pending, "dpkg-exec") is None


def test_a_foreign_architecture_never_marks_the_native_package():
    pending = [{"name": "libgl1"}]
    assert ru._entry_of(pending, "libgl1:i386") is None
    assert ru._entry_of(pending, f"libgl1:{ru.NATIVE_ARCH}") is pending[0]


def test_a_killed_install_in_use_leaves_no_step_behind(capt, monkeypatch):
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    st = ru.download(ru.check(ru.State()))
    st.phase = "installing"
    for p in st.pending:
        p["state"] = "installing"
    ru.save(st)
    assert ru.main(["raye-update", "live-ended"]) == 0
    st = ru.load()
    assert st.phase == "ready" and all("state" not in p for p in st.pending)


def test_nothing_found_means_no_size(capt):
    capt.plain = "apt-simulate-none.txt"
    st = ru.check(ru.State(totalBytes=999, downloadedBytes=999))
    assert (st.totalBytes, st.downloadedBytes) == (0, 0)


def test_a_file_missing_for_days_is_retried_quietly(capt, monkeypatch):
    # the timer comes back while the mirror still lacks the file: the same
    # set ends ready again, with the same key, so nobody is told twice
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    capt.download_rc, capt.download_out = 100, FAILED_ONE
    capt.cached = "apt-simulate-pinned.txt"
    monkeypatch.setattr(ru, "start_live", lambda asked=False: None)
    keys = []
    for _ in range(3):
        assert ru.main(["raye-update", "auto"]) == 0
        st = ru.load()
        keys.append(st.readyId)
        assert (st.phase, st.error, st.count) == ("ready", None, 2)
    assert keys[0] and len(set(keys)) == 1


def test_the_start_up_install_counts_what_it_left(paths, no_tools, monkeypatch):
    pending = ru.parse_simulation(fixture("apt-simulate.txt"))
    st = ru.State(phase="ready", pending=pending, count=len(pending),
                  readyId=ru.ready_key(pending, []), needsRestart=True)
    ru.arm(st, "restart")
    ru.save(st)
    monkeypatch.setattr(ru, "install_packages", lambda cb: (True, ""))
    monkeypatch.setattr(ru, "remove_obsolete", lambda: False)
    monkeypatch.setattr(ru, "dpkg_half_configured", lambda: False)
    # firefox's file was gone: apt still offers it after the install
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=0: (0, fixture("apt-simulate-pinned-firefox.txt"), "")
                        if cmd[:2] == ["apt-get", "-s"] else (0, "", ""))
    ru.install_offline(ru.load(), now=10.0)
    st = ru.load()
    assert st.lastInstall["ok"] is True and st.lastInstall["count"] == len(pending) - 1
    assert st.pending == [] and st.phase == "idle"
