"""The user side: raye-update-notify and its unit."""
import json
import os
import subprocess
from pathlib import Path

TOP = Path(__file__).resolve().parent.parent
SCRIPT = TOP / "src/raye-update-notify"
UNIT = TOP / "data/raye-update-notify.service"


def fake_tool(bin_dir, name, body):
    tool = bin_dir / name
    tool.write_text("#!/bin/sh\n" + body + "\n")
    tool.chmod(0o755)


def run_notify(tmp_path, state, answer="later", arm_rc=0, plasma_rc=0, lang="C.UTF-8", language=""):
    (tmp_path / "state.json").write_text(json.dumps(state))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "notify.log"
    calls = tmp_path / "calls.log"
    fake_tool(bin_dir, "notify-send", f'printf "%s\\n" "$@" > {log}\necho {answer}')
    fake_tool(bin_dir, "raye-update", f'echo "raye-update $*" >> {calls}\nexit {arm_rc}')
    fake_tool(bin_dir, "busctl", f'echo "busctl $*" >> {calls}\n'
                                 f'case "$*" in *org.kde.Shutdown*) exit {plasma_rc};; esac')
    fake_tool(bin_dir, "pkgcli", f'echo "pkgcli $*" >> {calls}')
    fake_tool(bin_dir, "systemctl", f'echo "systemctl $*" >> {calls}')
    # LANGUAGE as Settings writes it; empty: the machine's own does not leak in
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", LANG=lang, LANGUAGE=language,
               HOME=str(tmp_path), XDG_STATE_HOME=str(tmp_path / "st"),
               RAYE_UPDATE_STATE=str(tmp_path / "state.json"))
    subprocess.run(["sh", str(SCRIPT)], env=env, check=True, timeout=30)
    run_notify.calls = calls.read_text().splitlines() if calls.exists() else []
    return log.read_text() if log.exists() else None


READY = {"state": "ready", "ready_id": "r1", "updates": 2}


def test_restart_now_arms_then_asks_plasma(tmp_path):
    run_notify(tmp_path, READY, answer="restart")
    assert run_notify.calls == [
        "raye-update arm --then restart",
        "busctl --user call org.kde.Shutdown /Shutdown org.kde.Shutdown logoutAndReboot"]


def test_restart_now_falls_back_to_logind(tmp_path):
    run_notify(tmp_path, READY, answer="restart", plasma_rc=1)
    assert run_notify.calls[-1].startswith("busctl call org.freedesktop.login1")
    assert run_notify.calls[-1].endswith("Reboot b true")


def test_restart_now_never_restarts_when_arming_failed(tmp_path):
    run_notify(tmp_path, READY, answer="restart", arm_rc=126)
    assert run_notify.calls == ["raye-update arm --then restart"]


def test_later_does_nothing(tmp_path):
    run_notify(tmp_path, READY)
    assert run_notify.calls == []


def test_firmware_counts_as_an_update(tmp_path):
    out = run_notify(tmp_path, {"state": "ready", "ready_id": "a1", "updates": 0,
                                "firmware": [{"device": "UEFI dbx"}]})
    assert "1 update downloaded. Restart now to install it." in out


def test_packages_and_firmware_add_up(tmp_path):
    out = run_notify(tmp_path, {"state": "ready", "ready_id": "a2", "updates": 3,
                                "firmware": [{"device": "UEFI dbx"}]})
    assert "4 updates downloaded. Restart now to install them." in out


def test_one_update_in_french(tmp_path):
    out = run_notify(tmp_path, {"state": "ready", "ready_id": "a3", "updates": 1},
                     lang="fr_CA.UTF-8")
    assert "1 mise à jour téléchargée. Redémarrez maintenant pour l'installer." in out


def test_several_updates_in_french(tmp_path):
    out = run_notify(tmp_path, {"state": "ready", "ready_id": "a4", "updates": 2},
                     lang="fr_FR.UTF-8")
    assert "2 mises à jour téléchargées. Redémarrez maintenant pour les installer." in out


def test_no_count_shows_in_brackets(tmp_path):
    # "update(s)" reads as a template nobody filled in
    for lang, n in (("C.UTF-8", 1), ("C.UTF-8", 3), ("fr_CA.UTF-8", 1), ("fr_CA.UTF-8", 3)):
        sub = tmp_path / f"{lang}-{n}"
        sub.mkdir()
        out = run_notify(sub, {"state": "ready", "ready_id": f"b{n}", "updates": n}, lang=lang)
        assert "(s)" not in out


def test_nothing_ready_says_nothing(tmp_path):
    assert run_notify(tmp_path, {"state": "idle", "updates": 2}) is None


def test_unit_never_holds_a_start_job_while_waiting():
    # a oneshot unit stays "activating" while notify-send --wait waits, and
    # that queued start job makes systemd refuse to stop the session
    text = UNIT.read_text()
    assert "Type=oneshot" not in text
    assert "Type=exec" in text
    assert "PartOf=graphical-session.target" in text


def test_unit_survives_a_burst_of_state_writes():
    # the install rewrites state.json a few times a second; with the default
    # start limit the service failed, took the path unit down with it, and
    # the "ready" state at the end was never announced
    unit = {}
    section = None
    for line in UNIT.read_text().splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            unit[(section, k)] = v
    assert unit.get(("[Unit]", "StartLimitIntervalSec")) == "0"


def v2(pending, firmware=(), phase="ready", needs=True, to="1"):
    return {"version": 2, "phase": phase, "needsRestart": needs, "readyId": "r9",
            "pending": [{"name": n, "to": to, "restart": r} for n, r in pending],
            "firmware": [{"device": d, "deviceId": d, "to": to} for d in firmware],
            "state": "ready" if needs else "idle", "ready_id": "r9", "updates": len(pending)}


def test_updates_that_need_no_restart_say_nothing(tmp_path):
    assert run_notify(tmp_path, v2([("firefox", False), ("vlc", False)], needs=False)) is None


def test_only_what_needs_a_restart_is_counted_and_named(tmp_path):
    out = run_notify(tmp_path, v2([("firefox", False), ("linux-image-7.0.0-12-generic", True),
                                   ("libc6:i386", True)]))
    assert "2 updates downloaded. Restart now to install them." in out
    assert "Restart required: linux-image-7.0.0-12-generic, libc6" in out


def test_named_in_french(tmp_path):
    out = run_notify(tmp_path, v2([("libc6", True)], firmware=["UEFI dbx"]), lang="fr_CA.UTF-8")
    assert "Redémarrage requis : libc6, UEFI dbx" in out


def test_the_same_restart_set_is_told_once(tmp_path):
    first = v2([("firefox", False), ("libc6", True)])
    assert run_notify(tmp_path, first) is not None
    # the install in use took firefox: same restart set, no second notice
    (tmp_path / "bin").rename(tmp_path / "bin1")
    (tmp_path / "notify.log").unlink()
    assert run_notify(tmp_path, v2([("libc6", True)])) is None


def test_a_newer_set_with_the_same_names_is_told_again(tmp_path):
    assert run_notify(tmp_path, v2([("raye-theme", True)], to="0.3~beta1")) is not None
    (tmp_path / "bin").rename(tmp_path / "bin1")
    (tmp_path / "notify.log").unlink()
    assert run_notify(tmp_path, v2([("raye-theme", True)], to="0.3~beta2")) is not None


def test_new_firmware_for_the_same_device_is_told_again(tmp_path):
    assert run_notify(tmp_path, v2([], firmware=["UEFI dbx"], to="371")) is not None
    (tmp_path / "bin").rename(tmp_path / "bin1")
    (tmp_path / "notify.log").unlink()
    assert run_notify(tmp_path, v2([], firmware=["UEFI dbx"], to="372")) is not None
