# SPDX-License-Identifier: GPL-3.0-or-later
"""Checking and downloading, against recorded apt and fwupd output."""
import json
import os
import stat
import sys
import textwrap
import time

import pytest

from conftest import fixture, ru


def test_print_uris_sums_the_sizes():
    assert ru.parse_print_uris(fixture("apt-print-uris.txt")) == 3260116 + 15728640 + 72351744
    assert ru.parse_print_uris("") == 0
    assert ru.parse_print_uris("'http://x/a.deb' a.deb notanumber MD5:1\n") == 0


def test_status_lines_download():
    lines = [ru.parse_status(l) for l in fixture("apt-status-download.txt").splitlines()]
    assert lines[0] == ("dlstatus", "1", 0.0, "Retrieving file 1 of 3")
    assert [l[2] for l in lines] == [0.0, 4.0164, 35.5, 80.25, 100.0]


def test_status_lines_install_with_arch_qualified_names():
    lines = [ru.parse_status(l) for l in fixture("apt-status-install.txt").splitlines()]
    assert lines[1] == ("pmstatus", "libc6:amd64", 12.5, "Preparing libc6:amd64 (amd64)")
    assert lines[3][0] == "pmconffile"
    assert lines[-1][2] == 100.0


def test_status_line_garbage():
    assert ru.parse_status("Reading package lists...") is None
    assert ru.parse_status("") is None


@pytest.mark.parametrize("text,code", [
    ("E: You don't have enough free space in /var/cache/apt/archives/.", "no-space"),
    ("dpkg: error: No space left on device", "no-space"),
    ("W: Temporary failure resolving 'archive.ubuntu.com'", "no-internet"),
    ("E: Failed to fetch http://archive.ubuntu.com/... 404  Not Found", "server"),
    ("E: Some index files failed to download.", "server"),
    ("The following packages have unmet dependencies:", "broken"),
    ("something else entirely", "unknown"),
    ("", "unknown"),
])
def test_classify_error(text, code):
    assert ru.classify_error(text) == code


def test_lock_error():
    assert ru.is_lock_error("E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 42")
    assert not ru.is_lock_error("E: Failed to fetch")


def test_ready_key_changes_with_the_set():
    a = [{"name": "a", "to": "1"}, {"name": "b", "to": "2"}]
    assert ru.ready_key(a, []) == ru.ready_key(list(reversed(a)), [])
    assert ru.ready_key(a, []) != ru.ready_key([{"name": "a", "to": "2"}, a[1]], [])
    assert ru.ready_key(a, []) != ru.ready_key(a, [{"deviceId": "d", "to": "1"}])
    assert ru.ready_key([], []) == ""


def test_parse_fwupd_takes_the_newest_release_and_skips_devices_without_one():
    fw = ru.parse_fwupd(fixture("fwupd-get-updates.json"))
    assert fw == [{"device": "System Firmware",
                   "deviceId": "6a3c1f0e9b2d4c5a8e7f6d5c4b3a29181716151a",
                   "from": "1.07", "to": "1.09", "summary": "Firmware for the Example laptop",
                   "size": 9437184,
                   "sha256": "1bf2dd354e35e86138478f17c83724be644e0dc4c2b82fa95a2cfc7ebfad13d5",
                   "location": "https://fwupd.org/downloads/1bf2dd354e35e86138478f17c83724be644e0dc4c2b82fa95a2cfc7ebfad13d5-Example-1.09.cab",
                   "needsPower": False}]


def fwupd_device(have, offered, flags=()):
    return json.dumps({"Devices": [{"Name": "dbx", "DeviceId": "d1", "Version": have,
                                    "Flags": list(flags),
                                    "Releases": [{"Version": offered, "Size": 1}]}]})


@pytest.mark.parametrize("have,offered", [("20260707", "20260707"), ("1.58.0", "1.56.0"),
                                          ("20260801", "20260707")])
def test_firmware_already_current_is_not_offered(have, offered):
    assert ru.parse_fwupd(fwupd_device(have, offered)) == []


@pytest.mark.parametrize("have,offered", [("1.56.0", "1.58.0"), ("", "20260707"), ("A1", "1.2")])
def test_firmware_behind_is_offered(have, offered):
    assert len(ru.parse_fwupd(fwupd_device(have, offered))) == 1


def test_firmware_that_wants_the_charger_says_so():
    fw = ru.parse_fwupd(fwupd_device("1.0", "1.1", ["internal", "updatable", "require-ac"]))
    assert fw[0]["needsPower"] is True
    assert ru.validate({**ru.to_json(ru.State()), "firmware": fw}) == []


@pytest.mark.parametrize("text", ["", "not json", "[]", '{"Devices": [1, {"Releases": [2]}]}',
                                  '{"Devices": [{"Name": "x", "Releases": "no"}]}'])
def test_parse_fwupd_survives_odd_output(text):
    assert ru.parse_fwupd(text) == []


class FakeTools:
    """Answers run() like apt-get and fwupdmgr would, from fixtures."""

    def __init__(self, simulate="apt-simulate.txt", uris="apt-print-uris.txt", update_rc=0,
                 update_err="", firmware=None, cached=None):
        self.simulate, self.uris, self.update_rc, self.update_err = simulate, uris, update_rc, update_err
        self.firmware = firmware
        self.cached = cached        # what the downloaded files can install
        self.calls = []

    def run(self, cmd, timeout=3600):
        self.calls.append(cmd)
        if cmd[:2] == ["apt-get", "update"]:
            return self.update_rc, "", self.update_err
        if cmd[:2] == ["apt-get", "-s"]:
            if "--no-download" in cmd and self.cached:
                return 0, fixture(self.cached), ""
            return 0, fixture(self.simulate), ""
        if cmd[:2] == ["apt-get", "--print-uris"]:
            return 0, fixture(self.uris) if self.uris else "", ""
        if cmd[:2] == ["fwupdmgr", "get-updates"]:
            if self.firmware:
                return 0, fixture(self.firmware), ""
            return 2, "", "No updates available"
        return 0, "", ""


@pytest.fixture
def machine(monkeypatch):
    """A plugged-in, unmetered machine with room to spare, not in a game."""
    monkeypatch.setattr(ru, "battery_level", lambda: (True, None))
    monkeypatch.setattr(ru, "is_metered", lambda: False)
    monkeypatch.setattr(ru, "is_gaming", lambda: False)
    monkeypatch.setattr(ru, "free_bytes", lambda p: 50 << 30)
    monkeypatch.setattr(ru, "fetch", lambda url, dest: False)   # no network in tests


def test_check_fills_the_pending_set(paths, monkeypatch):
    tools = FakeTools(firmware="fwupd-get-updates.json")
    monkeypatch.setattr(ru, "run", tools.run)
    st = ru.check(ru.State(), now=1234.0)
    assert st.phase == "idle" and st.count == 7 and st.lastCheck == 1234.0
    assert st.kernelChange and len(st.firmware) == 1
    assert st.error is None
    # never removes anything: upgrade with new packages, not full-upgrade
    sim = [c for c in tools.calls if c[:2] == ["apt-get", "-s"]][0]
    assert sim[-2:] == ["upgrade", "--with-new-pkgs"]


def test_check_without_updates(paths, monkeypatch):
    monkeypatch.setattr(ru, "run", FakeTools(simulate="apt-simulate-none.txt").run)
    st = ru.check(ru.State(readyId="old", needsRestart=True))
    assert (st.phase, st.count, st.readyId, st.needsRestart, st.kernelChange) == ("idle", 0, "", False, False)


def test_check_offline_is_a_plain_words_error(paths, monkeypatch):
    tools = FakeTools(update_rc=100, update_err="W: Temporary failure resolving 'archive.ubuntu.com'")
    monkeypatch.setattr(ru, "run", tools.run)
    st = ru.check(ru.State())
    assert st.phase == "error" and st.error["code"] == "no-internet"
    assert ru.validate(ru.to_json(st)) == []


def test_check_while_another_install_runs_waits(paths, monkeypatch):
    tools = FakeTools(update_rc=100, update_err="E: Could not get lock /var/lib/apt/lists/lock")
    monkeypatch.setattr(ru, "run", tools.run)
    st = ru.check(ru.State())
    assert (st.phase, st.waitReason, st.error) == ("waiting", "busy", None)


def test_check_keeps_a_set_already_downloaded(paths, monkeypatch):
    monkeypatch.setattr(ru, "run", FakeTools().run)
    pending = ru.parse_simulation(fixture("apt-simulate.txt"))
    key = ru.ready_key(pending, [])
    st = ru.check(ru.State(phase="ready", readyId=key, needsRestart=True))
    assert (st.phase, st.readyId, st.needsRestart) == ("ready", key, True)


def test_download_reports_bytes_and_ends_ready(paths, monkeypatch, machine):
    monkeypatch.setattr(ru, "run", FakeTools().run)
    seen = []

    def fake_stream(cmd, on_status):
        assert "-d" in cmd and cmd[-2:] == ["upgrade", "--with-new-pkgs"]
        for line in fixture("apt-status-download.txt").splitlines():
            on_status(*ru.parse_status(line))
            seen.append(ru.load().downloadedBytes)
        return 0, ""
    monkeypatch.setattr(ru, "run_with_status", fake_stream)
    st = ru.check(ru.State())
    st = ru.download(st)
    total = 3260116 + 15728640 + 72351744
    assert (st.phase, st.totalBytes, st.downloadedBytes, st.needsRestart) == ("ready", total, total, True)
    assert st.readyId == ru.ready_key(st.pending, st.firmware)
    assert seen[-1] == total and seen == sorted(seen)
    assert ru.load().phase == "downloading"      # what the readers saw during the download


@pytest.mark.parametrize("setup,reason", [
    ({"is_gaming": lambda: True}, "game"),
    ({"is_metered": lambda: True}, "metered"),
    ({"battery_level": lambda: (False, 20)}, "battery"),
])
def test_download_waits_for_a_better_moment(paths, monkeypatch, machine, setup, reason):
    for name, fn in setup.items():
        monkeypatch.setattr(ru, name, fn)
    monkeypatch.setattr(ru, "run", FakeTools().run)
    monkeypatch.setattr(ru, "run_with_status", lambda *a: (_ for _ in ()).throw(AssertionError()))
    st = ru.download(ru.check(ru.State()))
    assert (st.phase, st.waitReason, st.readyId) == ("waiting", reason, "")


def test_download_refuses_without_room(paths, monkeypatch, machine):
    monkeypatch.setattr(ru, "free_bytes", lambda p: 100 << 20)
    monkeypatch.setattr(ru, "run", FakeTools().run)
    monkeypatch.setattr(ru, "run_with_status", lambda *a: (_ for _ in ()).throw(AssertionError()))
    st = ru.download(ru.check(ru.State()))
    assert st.phase == "error" and st.error["code"] == "no-space"


def test_download_failure_is_classified(paths, monkeypatch, machine):
    # nothing arrived: the cache cannot install anything
    monkeypatch.setattr(ru, "run", FakeTools(cached="apt-simulate-none.txt").run)
    monkeypatch.setattr(ru, "run_with_status",
                        lambda cmd, cb: (100, "E: Failed to fetch https://packages.mozilla.org/x 503 "))
    st = ru.download(ru.check(ru.State()))
    assert st.phase == "error" and st.error["code"] == "server" and st.readyId == ""


def test_download_of_firmware_only_needs_no_apt(paths, monkeypatch, machine):
    monkeypatch.setattr(ru, "run", FakeTools(simulate="apt-simulate-none.txt",
                                             firmware="fwupd-get-updates.json").run)
    monkeypatch.setattr(ru, "run_with_status", lambda *a: (_ for _ in ()).throw(AssertionError()))
    fetched = []
    monkeypatch.setattr(ru, "fetch", lambda url, dest: fetched.append(url) or False)
    st = ru.download(ru.check(ru.State()))
    assert (st.phase, st.count, len(st.firmware), st.needsRestart) == ("ready", 0, 1, True)
    # the cabinet is fetched now, the install at start-up has no network
    assert fetched == [st.firmware[0]["location"]]


def test_auto_checks_then_downloads(paths, monkeypatch, machine):
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ru, "run", FakeTools().run)
    monkeypatch.setattr(ru, "run_with_status", lambda cmd, cb: (0, ""))
    assert ru.main(["raye-update", "auto"]) == 0
    st = ru.load()
    assert st.phase == "ready" and st.count == 7
    assert ru.validate(ru.to_json(st)) == []


def test_auto_downloads_again_what_the_cache_lost(paths, monkeypatch, machine):
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ru, "run", FakeTools().run)
    pending = ru.parse_simulation(fixture("apt-simulate.txt"))
    ru.save(ru.State(phase="ready", readyId=ru.ready_key(pending, []), needsRestart=True))
    runs = []
    monkeypatch.setattr(ru, "run_with_status", lambda cmd, cb: runs.append(cmd) or (0, ""))
    assert ru.main(["raye-update", "auto"]) == 0
    assert len(runs) == 1 and ru.load().phase == "ready"


def test_check_command_never_downloads(paths, monkeypatch, machine):
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ru, "run", FakeTools().run)
    monkeypatch.setattr(ru, "run_with_status", lambda *a: (_ for _ in ()).throw(AssertionError()))
    assert ru.main(["raye-update", "check"]) == 0
    assert ru.load().count == 7


def test_run_with_status_reads_the_status_pipe_of_a_real_process(tmp_path, monkeypatch):
    """The plumbing, with a stand-in for apt-get that writes to the fd it is given."""
    fake = tmp_path / "apt-get"
    fake.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import os, sys
        fd = int(sys.argv[2].split("=")[1])
        print("Get:1 http://archive.ubuntu.com something", flush=True)
        os.write(fd, b"dlstatus:1:50.0000:Retrieving file 1 of 2\\n")
        os.write(fd, b"dlstatus:2:100.0000:Retrieving file 2 of 2\\n")
        print("args:", " ".join(sys.argv[1:]))
        sys.exit(3)
        """))
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    got = []
    rc, out = ru.run_with_status(["apt-get", "-y", "-d", "upgrade"], lambda *a: got.append(a))
    assert rc == 3
    assert [g[2] for g in got] == [50.0, 100.0]
    assert "Get:1" in out and "args: -o APT::Status-Fd=" in out and "-y -d upgrade" in out


def test_run_with_status_says_nothing_once_apt_is_done(tmp_path, monkeypatch):
    """apt writes its status lines faster than the boot screen takes them. A
    line still queued when apt ends must not reach the screen later: it put
    "Working on updates 96%" back up after the install had finished."""
    fake = tmp_path / "apt-get"
    fake.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import os, sys
        fd = int(sys.argv[2].split("=")[1])
        for i in range(200):
            os.write(fd, b"pmstatus:pkg:%d.0:Installing pkg\\n" % (i // 2))
        """))
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    got = []

    def slow(*a):
        time.sleep(0.05)
        got.append(a)
    rc, _ = ru.run_with_status(["apt-get", "-y", "upgrade"], slow)
    assert rc == 0
    seen = len(got)
    time.sleep(0.5)
    assert len(got) == seen


def test_install_sends_a_number_only_when_it_changes(monkeypatch):
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=0: (0, "", ""))

    def stream(cmd, cb):
        for pct in (0.0, 0.4, 12.5, 12.9, 25.0, 25.0, 99.0, 100.0):
            cb("pmstatus", "pkg", pct, "Installing")
        cb("dlstatus", "pkg", 50.0, "Retrieving")
        return 0, ""
    monkeypatch.setattr(ru, "run_with_status", stream)
    sent = []
    assert ru.install_packages(sent.append)[0]
    assert sent == [0, 12, 25, 99, 100]


def test_run_with_status_missing_tool():
    assert ru.run_with_status(["/nonexistent/apt-get"], lambda *a: None)[0] == 127


def test_run_missing_tool():
    assert ru.run(["/nonexistent/tool"])[0] == 127
