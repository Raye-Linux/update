# SPDX-License-Identifier: GPL-3.0-or-later
"""The command line: what Start, the logout screen and Settings call."""
import json
import os
import subprocess

import pytest

from conftest import HERE, fixture, ru

PENDING = ru.parse_simulation(fixture("apt-simulate.txt"))


@pytest.fixture
def root(paths, no_tools, monkeypatch):
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    return no_tools


def ready():
    return ru.State(phase="ready", pending=PENDING, count=len(PENDING), needsRestart=True,
                    readyId=ru.ready_key(PENDING, []))


def test_status_needs_no_root(paths, monkeypatch, capsys):
    monkeypatch.setattr(ru.os, "geteuid", lambda: 1000)
    ru.save(ready())
    assert ru.main(["raye-update", "status"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["phase"] == "ready" and d["state"] == "ready" and ru.validate(d) == []


@pytest.mark.parametrize("cmd", ["arm", "disarm", "pause", "resume", "auto", "check",
                                 "active-hours", "auto-restart", "restart-due", "install-offline"])
def test_changes_need_root(paths, monkeypatch, cmd):
    monkeypatch.setattr(ru.os, "geteuid", lambda: 1000)
    assert ru.main(["raye-update", cmd]) == 1


def test_arm_then_restart(root, paths):
    ru.save(ready())
    assert ru.main(["raye-update", "arm", "--then", "restart"]) == 0
    st = ru.load()
    assert st.phase == "armed" and st.armed == {"then": "restart", "at": 0}
    assert root["power"] == []                      # arm does not restart by itself


def test_arm_then_shutdown_now_restarts_into_the_install(root):
    ru.save(ready())
    assert ru.main(["raye-update", "arm", "--then", "shutdown", "--now"]) == 0
    assert root["power"] == ["restart"]
    assert ru.load().armed["then"] == "shutdown"


def test_arm_at_a_picked_time(root, monkeypatch):
    monkeypatch.setattr(ru.time, "time", lambda: 1000.0)
    ru.save(ready())
    assert ru.main(["raye-update", "arm", "--then", "restart", "--at", "4600"]) == 0
    assert ru.load().armed == {"then": "restart", "at": 4600}


@pytest.mark.parametrize("args", [
    [], ["--then"], ["--then", "reboot"], ["--then", "restart", "--at", "999"],
    ["--then", "restart", "--at", str(1000 + 8 * 24 * 3600)],
    ["--then", "restart", "--at", "1e9"], ["--then", "restart", "--at", "$(id)"],
    ["--then", "restart", "--at", "2000", "--now"], ["--then", "restart", "--extra"],
])
def test_arm_refuses_bad_arguments(root, monkeypatch, args):
    monkeypatch.setattr(ru.time, "time", lambda: 1000.0)
    ru.save(ready())
    assert ru.main(["raye-update", "arm", *args]) == 2
    assert ru.load().armed is None


def test_arm_without_downloads(root):
    ru.save(ru.State())
    assert ru.main(["raye-update", "arm", "--then", "restart"]) == 3


def test_arm_when_another_tool_armed(root, paths):
    os.symlink("/var/lib/PackageKit/prepared-update", paths["link"])
    ru.save(ready())
    assert ru.main(["raye-update", "arm", "--then", "restart"]) == 4


def test_disarm(root, paths):
    ru.save(ready())
    ru.main(["raye-update", "arm", "--then", "restart"])
    assert ru.main(["raye-update", "disarm"]) == 0
    assert ru.load().phase == "ready" and not os.path.lexists(paths["link"])


def test_pause_and_resume(root):
    assert ru.main(["raye-update", "pause", "3"]) == 0
    assert ru.load().phase == "paused"
    assert ru.main(["raye-update", "resume"]) == 0
    assert ru.load().pausedUntil == 0
    for bad in (["pause"], ["pause", "0"], ["pause", "six"], ["pause", "6"]):
        assert ru.main(["raye-update", *bad]) == 2


def test_active_hours(root):
    assert ru.main(["raye-update", "active-hours", "22", "6"]) == 0
    assert ru.load().activeHours == {"start": 22, "end": 6}
    for bad in ([], ["8"], ["8", "8"], ["0", "20"], ["a", "b"], ["8", "17", "x"]):
        assert ru.main(["raye-update", "active-hours", *bad]) == 2
    assert ru.load().activeHours == {"start": 22, "end": 6}


def test_auto_restart(root):
    assert ru.main(["raye-update", "auto-restart", "on"]) == 0
    assert ru.load().autoRestart is True
    assert ru.main(["raye-update", "auto-restart", "off"]) == 0
    assert ru.load().autoRestart is False
    assert ru.main(["raye-update", "auto-restart", "yes"]) == 2


def test_unknown_command(root):
    assert ru.main(["raye-update", "frobnicate"]) == 2


def test_install_offline_command_powers_off_when_asked(root, monkeypatch):
    monkeypatch.setattr(ru, "install_offline", lambda st: "shutdown")
    assert ru.main(["raye-update", "install-offline"]) == 0
    assert root["power"] == ["shutdown"]
    monkeypatch.setattr(ru, "install_offline", lambda st: None)
    root["power"].clear()
    assert ru.main(["raye-update", "install-offline"]) == 0
    assert root["power"] == []


def test_install_offline_command_goes_on_to_the_desktop(root, monkeypatch):
    went = []
    monkeypatch.setattr(ru, "install_offline", lambda st: "continue")
    monkeypatch.setattr(ru, "continue_boot", lambda: went.append(1) or True)
    assert ru.main(["raye-update", "install-offline"]) == 0
    assert went == [1] and root["power"] == []


def test_install_offline_command_restarts_when_going_on_fails(root, monkeypatch):
    monkeypatch.setattr(ru, "install_offline", lambda st: "continue")
    monkeypatch.setattr(ru, "continue_boot", lambda: False)
    assert ru.main(["raye-update", "install-offline"]) == 0
    assert root["power"] == ["restart"]
    assert root["plymouth"][-1] == ("display-message", "--text=raye:mode:reboot")


def test_install_offline_command_says_restarting_before_it_restarts(root, monkeypatch):
    monkeypatch.setattr(ru, "install_offline", lambda st: "restart")
    assert ru.main(["raye-update", "install-offline"]) == 0
    assert root["power"] == ["restart"]
    assert root["plymouth"][-1] == ("display-message", "--text=raye:mode:reboot")


# --- taskbar progress --------------------------------------------------------

def test_launcher_props():
    assert ru.launcher_props(ru.State(phase="downloading", totalBytes=200, downloadedBytes=50)) == (0.25, True)
    assert ru.launcher_props(ru.State(phase="downloading", totalBytes=0)) == (0.0, False)
    assert ru.launcher_props(ru.State(phase="ready", totalBytes=200, downloadedBytes=200)) == (0.0, False)


def test_progress_loop_follows_then_hides(paths):
    states = iter([ru.State(phase="downloading", totalBytes=100, downloadedBytes=10),
                   ru.State(phase="downloading", totalBytes=100, downloadedBytes=10),
                   ru.State(phase="downloading", totalBytes=100, downloadedBytes=60),
                   ru.State(phase="ready", readyId="x")])
    emitted = []

    def tick(_):
        ru.save(next(states))
    ru.save(next(states))
    assert ru.progress_loop(sleep=tick, emit=lambda v, vis: emitted.append((v, vis))) == 0
    assert emitted == [(0.1, True), (0.6, True), (0.0, False)]


def test_emit_progress_sends_the_unity_signal(monkeypatch):
    calls = []
    monkeypatch.setattr(ru.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    ru.emit_progress(0.456, True)
    cmd = calls[0]
    assert cmd[:3] == ["gdbus", "emit", "--session"]
    assert "com.canonical.Unity.LauncherEntry.Update" in cmd
    assert cmd[-2] == "'application://org.raye.settings.desktop'"
    assert cmd[-1] == "{'progress': <0.456>, 'progress-visible': <true>}"


def test_emit_progress_reaches_a_session_bus():
    """gdbus accepts the arguments as written (needs dbus-run-session)."""
    if subprocess.run(["sh", "-c", "command -v dbus-run-session && command -v gdbus"],
                      capture_output=True).returncode != 0:
        pytest.skip("no dbus-run-session or gdbus here")
    script = ("import sys; sys.path.insert(0, %r); import raye_update as ru; "
              "import subprocess as s; "
              "r = s.run(['gdbus','emit','--session','--object-path','/org/raye/update','--signal',"
              "'com.canonical.Unity.LauncherEntry.Update', \"'%s'\", "
              "\"{'progress': <0.5>, 'progress-visible': <true>}\"], capture_output=True, text=True); "
              "print(r.returncode, r.stderr)") % (os.path.join(HERE, "..", "src"), ru.SETTINGS_APP)
    out = subprocess.run(["dbus-run-session", "--", "python3", "-c", script],
                         capture_output=True, text=True, timeout=30)
    assert out.stdout.startswith("0 "), out.stdout + out.stderr


def test_wrapper_runs_status_without_pkexec(tmp_path):
    """src/raye-update: status straight away, root commands through pkexec."""
    wrapper = os.path.join(HERE, "..", "src", "raye-update")
    log = tmp_path / "log"
    for name in ("pkexec", "libexec"):
        f = tmp_path / name
        f.write_text(f'#!/bin/sh\necho "{name} $*" >> {log}\n')
        f.chmod(0o755)
    env = {**os.environ, "RAYE_UPDATE_LIBEXEC": str(tmp_path / "libexec"),
           "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    subprocess.run([wrapper, "status"], env=env, check=True)
    subprocess.run([wrapper, "arm", "--then", "restart"], env=env, check=True)
    lines = log.read_text().splitlines()
    assert lines[0] == "libexec status"
    if os.geteuid() == 0:
        assert lines[1] == "libexec arm --then restart"
    else:
        assert lines[1] == f"pkexec {tmp_path / 'libexec'} arm --then restart"


def test_a_standard_user_types_the_password_every_time():
    # auth_admin_keep let a child's session run any raye-update command for
    # five minutes after a parent typed it (pause 5, disarm); admins get YES
    # from 50-raye-update.rules
    import xml.etree.ElementTree as ET
    here = os.path.join(os.path.dirname(__file__), "..", "data")
    action = ET.parse(os.path.join(here, "org.raye.update.policy")).find("action")
    assert {d.tag: d.text for d in action.find("defaults")} == {
        "allow_any": "auth_admin", "allow_inactive": "auth_admin", "allow_active": "auth_admin"}
    assert 'action.id == "org.raye.update"' in open(os.path.join(here, "50-raye-update.rules")).read()
