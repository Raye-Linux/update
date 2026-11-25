# SPDX-License-Identifier: GPL-3.0-or-later
"""Raye Linux 1.0 runs the Raye kernel. The first start after the update
(or any start until it is done) switches: with Secure Boot on, the Raye key
is queued for the firmware and the console shows the six-digit code and the
steps of the blue screen BEFORE the restart; without Secure Boot the kernel
goes in at once and the PC restarts into its trial boot. A person away from
the PC is never caught in a loop of restarts: no key within the wait, the
request is withdrawn and the start goes on to the sign-in screen; three
tries at most, then the switch is left to Settings."""
import os

import pytest

from conftest import ru


class Setup:
    """raye-kernel-setup, mokutil, nm-online and plymouth as one fake run()."""

    def __init__(self, due="yes", status="not-enrolled", code="042137", enroll_rc=0, online=True):
        self.due, self.status, self.code, self.enroll_rc, self.online = due, status, code, enroll_rc, online
        self.calls = []

    def run(self, cmd, timeout=0):
        self.calls.append(cmd)
        if cmd[:1] == [ru.KERNEL_SETUP]:
            if cmd[1] == "switch-due":
                return (0 if self.due == "yes" else 1), self.due + "\n", ""
            if cmd[1] == "status":
                return 0, self.status + "\n", ""
            if cmd[1] == "enroll":
                return self.enroll_rc, (self.code + "\n" if self.code else ""), ""
        if cmd[0] == "nm-online":
            return (0 if self.online else 1), "", ""
        return 0, "", ""

    def names(self):
        return [" ".join(c[:2]) for c in self.calls]


@pytest.fixture
def world(tmp_path, monkeypatch, no_tools):
    setup_bin = tmp_path / "raye-kernel-setup"
    setup_bin.write_text("")
    monkeypatch.setattr(ru, "KERNEL_SETUP", str(setup_bin))
    monkeypatch.setattr(ru, "KERNEL_SWITCH_TRIES", str(tmp_path / "lib" / "kernel-switch-tries"))
    shown = []
    monkeypatch.setattr(ru, "show_on_console", lambda text: shown.append(text))
    keys = {"pressed": True, "waited": []}

    def wait_key(seconds):
        keys["waited"].append(seconds)
        return keys["pressed"]
    monkeypatch.setattr(ru, "wait_for_key", wait_key)
    monkeypatch.setattr(ru, "system_language", lambda files=None: "en")
    return {"shown": shown, "keys": keys, "tools": no_tools, "tmp": tmp_path}


def tries(world):
    p = world["tmp"] / "lib" / "kernel-switch-tries"
    return int(p.read_text()) if p.exists() else 0


def test_secure_boot_shows_the_code_then_restarts(world, monkeypatch):
    s = Setup()
    monkeypatch.setattr(ru, "run", s.run)
    assert ru.kernel_switch() == "code"
    assert s.names()[-1] == f"{ru.KERNEL_SETUP} enroll"
    [text] = world["shown"]
    assert "0 4 2 1 3 7" in text
    for words in ("blue screen", "10 seconds", "Enroll MOK", "Continue", "Yes"):
        assert words in text
    # the boot screen goes first, or the console would be drawn under it
    assert ("quit",) in world["tools"]["plymouth"]
    assert world["tools"]["power"] == ["restart"]
    assert tries(world) == 1


def test_french(world, monkeypatch):
    monkeypatch.setattr(ru, "system_language", lambda files=None: "fr")
    monkeypatch.setattr(ru, "run", Setup().run)
    ru.kernel_switch()
    [text] = world["shown"]
    assert "écran bleu" in text and "Enroll MOK" in text and "0 4 2 1 3 7" in text


def test_nobody_at_the_pc_the_start_goes_on(world, monkeypatch):
    # an update installed at night: no restart loop, no blue screen nobody sees
    world["keys"]["pressed"] = False
    s = Setup()
    monkeypatch.setattr(ru, "run", s.run)
    assert ru.kernel_switch() == "left"
    assert ["mokutil", "--revoke-import"] in s.calls
    assert world["tools"]["power"] == []
    assert tries(world) == 1


def test_three_tries_then_left_to_settings(world, monkeypatch):
    world["keys"]["pressed"] = False
    s = Setup()
    monkeypatch.setattr(ru, "run", s.run)
    for _ in range(3):
        ru.kernel_switch()
    s.calls.clear()
    world["shown"].clear()
    assert ru.kernel_switch() == "given-up"
    assert not any(c[:2] == [ru.KERNEL_SETUP, "enroll"] for c in s.calls)
    assert world["shown"] == []


def test_not_due_nothing_happens(world, monkeypatch):
    s = Setup(due="no")
    monkeypatch.setattr(ru, "run", s.run)
    assert ru.kernel_switch() == "not-due"
    assert s.names() == [f"{ru.KERNEL_SETUP} switch-due"]
    assert world["shown"] == [] and world["tools"]["power"] == [] and tries(world) == 0


def test_no_kernel_setup_nothing_happens(world, monkeypatch):
    os.unlink(ru.KERNEL_SETUP)
    s = Setup()
    monkeypatch.setattr(ru, "run", s.run)
    assert ru.kernel_switch() == "not-due"
    assert s.calls == []


def test_without_secure_boot_the_kernel_goes_in_and_the_pc_restarts(world, monkeypatch):
    s = Setup(status="no-secure-boot", code="")
    monkeypatch.setattr(ru, "run", s.run)
    assert ru.kernel_switch() == "installed"
    # the kernel may have to be downloaded: the network first
    assert s.names().index("nm-online -q") < s.names().index(f"{ru.KERNEL_SETUP} enroll")
    [text] = world["shown"]
    assert "Raye kernel" in text and "code" not in text.lower()
    assert world["keys"]["waited"] == []
    assert world["tools"]["power"] == ["restart"]


def test_without_secure_boot_and_offline_next_start(world, monkeypatch):
    s = Setup(status="no-secure-boot", code="", online=False)
    monkeypatch.setattr(ru, "run", s.run)
    assert ru.kernel_switch() == "offline"
    assert not any(c[:2] == [ru.KERNEL_SETUP, "enroll"] for c in s.calls)
    assert tries(world) == 0 and world["tools"]["power"] == []


def test_a_failed_enroll_never_stops_the_start(world, monkeypatch):
    s = Setup(enroll_rc=1, code="")
    monkeypatch.setattr(ru, "run", s.run)
    assert ru.kernel_switch() == "failed"
    assert world["tools"]["power"] == [] and tries(world) == 1


def test_main_runs_it_as_root_only(monkeypatch):
    seen = []
    monkeypatch.setattr(ru, "kernel_switch", lambda: seen.append(1) or "not-due")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert ru.main(["raye-update", "kernel-switch"]) == 0 and seen == [1]


def test_the_boot_unit_comes_before_the_sign_in_screen():
    unit = open(os.path.join(os.path.dirname(__file__), "..", "data",
                             "raye-update-kernel-switch.service")).read()
    assert "ExecStart=/usr/libexec/raye-update kernel-switch" in unit
    assert "Before=display-manager.service" in unit
    assert "After=raye-kernel-finish.service" in unit
    # never in the live session, never in the start-up that installs updates
    assert "ConditionKernelCommandLine=!boot=casper" in unit
    assert "WantedBy=multi-user.target" in unit
    rules = open(os.path.join(os.path.dirname(__file__), "..", "debian", "rules")).read()
    assert "raye-update-kernel-switch.service" in rules
    install = open(os.path.join(os.path.dirname(__file__), "..", "debian", "install")).read()
    assert "data/raye-update-kernel-switch.service usr/lib/systemd/system" in install


def test_kernel_updates_come_with_linux_raye_once_it_is_published(monkeypatch):
    # By name, a newer Raye kernel would go in before its key is enrolled
    # (a PC of the beta runs a kernel the earlier key signed): once the
    # archive has linux-raye, kernel updates come through it and
    # raye-kernel-setup installs it after the key is in.
    calls = []

    def run(cmd, timeout=0):
        calls.append(cmd)
        if cmd[:3] == ["apt-cache", "show", "linux-raye"]:
            return 0, "Package: linux-raye\n", ""
        if cmd[0] == "dpkg-query":
            return 0, "linux-image-7.0.0-40-raye install ok installed\n", ""
        if cmd[:2] == ["apt-cache", "pkgnames"]:
            return 0, "linux-image-7.0.0-40-raye\nlinux-image-7.0.14-44r4-raye\n", ""
        return 0, "", ""
    monkeypatch.setattr(ru, "run", run)
    monkeypatch.setattr(ru, "FOLLOW_RAYE_KERNEL", True)
    assert ru.raye_kernel_followups() == []
