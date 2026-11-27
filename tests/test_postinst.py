"""debian/raye-update.postinst: bring back a failed notify watch."""
import os
import socket
import subprocess
from pathlib import Path

TOP = Path(__file__).resolve().parent.parent
POSTINST = TOP / "debian/raye-update.postinst"


def run(tmp_path, *args, users=(("1000", "testr"),), socket_for=("1000",)):
    runtime = tmp_path / "run-user"
    for uid, _ in users:
        d = runtime / uid / "systemd"
        d.mkdir(parents=True)
        if uid in socket_for:
            s = socket.socket(socket.AF_UNIX)
            s.bind(str(d / "private"))
            s.close()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    (bin_dir / "systemctl").write_text(f'#!/bin/sh\necho "$*" >> {calls}\n')
    lines = "\n".join(f'[ "$2" = {uid} ] && echo {name}:x:{uid}:{uid}::/home/{name}:/bin/sh'
                      for uid, name in users)
    (bin_dir / "getent").write_text(f"#!/bin/sh\n{lines}\nexit 0\n")
    # its own record: the systemctl calls stay what the tests above compare
    (bin_dir / "raye-update").write_text(f'#!/bin/sh\necho "$*" >> {tmp_path / "raye-calls"}\n')
    (bin_dir / "systemd-run").write_text(f'#!/bin/sh\necho "$*" >> {tmp_path / "run-calls"}\n')
    for tool in ("systemctl", "getent", "raye-update", "systemd-run"):
        (bin_dir / tool).chmod(0o755)
    script = tmp_path / "postinst"
    script.write_text(POSTINST.read_text().replace("#DEBHELPER#", ""))
    booted = tmp_path / "run-systemd-system"
    booted.mkdir()
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", RAYE_USER_RUNTIME=str(runtime),
               RAYE_SYSTEMD_RUN=str(booted), RAYE_UPDATE=str(bin_dir / "raye-update"))
    subprocess.run(["sh", str(script), *args], env=env, check=True, timeout=30)
    return calls.read_text().splitlines() if calls.exists() else []


def test_upgrade_restarts_the_watch_of_a_signed_in_user(tmp_path):
    calls = run(tmp_path, "configure", "0.3~beta1")
    assert calls == [
        "--system daemon-reload",
        "--user -M testr@ daemon-reload",
        "--user -M testr@ reset-failed raye-update-notify.path raye-update-notify.service "
        "raye-update-progress.path raye-update-progress.service",
        "--user -M testr@ restart raye-update-notify.path raye-update-progress.path"]


def test_no_user_manager_no_call(tmp_path):
    assert run(tmp_path, "configure", "0.3~beta1", socket_for=()) == ["--system daemon-reload"]


def test_new_shutdown_screen_counts_for_the_next_restart(tmp_path):
    # debhelper reloads systemd before it enables the new unit: without one
    # more reload, the restart right after this upgrade did not run it
    calls = run(tmp_path, "configure", "0.5.1~beta1", socket_for=())
    assert calls == ["--system daemon-reload"]


def test_only_on_configure(tmp_path):
    assert run(tmp_path, "abort-upgrade", "0.5~beta1") == []


def test_upgrade_finishes_a_pc_an_older_raye_update_left_half_way(tmp_path):
    run(tmp_path, "configure", "1.0~beta1")
    assert (tmp_path / "raye-calls").read_text().splitlines() == ["arm-if-split"]


def test_no_arm_if_split_outside_configure(tmp_path):
    run(tmp_path, "abort-upgrade", "1.0~beta1")
    assert not (tmp_path / "raye-calls").exists()


def test_a_failing_arm_if_split_never_fails_the_install(tmp_path):
    bin_dir = tmp_path / "bin2"
    bin_dir.mkdir()
    script = tmp_path / "postinst2"
    script.write_text(POSTINST.read_text().replace("#DEBHELPER#", ""))
    (bin_dir / "raye-update").write_text("#!/bin/sh\nexit 3\n")
    (bin_dir / "raye-update").chmod(0o755)
    env = dict(os.environ, RAYE_UPDATE=str(bin_dir / "raye-update"),
               RAYE_USER_RUNTIME=str(tmp_path / "none"), RAYE_SYSTEMD_RUN=str(tmp_path / "none"))
    assert subprocess.run(["sh", str(script), "configure"], env=env, timeout=30).returncode == 0


def test_upgrade_checks_again_once_the_install_in_use_ends(tmp_path):
    # the fix of a broken release is armed by a check that runs after this
    # install, which may hold the state while arm-if-split runs
    run(tmp_path, "configure", "1.0.1")
    calls = (tmp_path / "run-calls").read_text().splitlines()
    assert len(calls) == 1 and "--on-active=90" in calls[0] and "raye-update.service" in calls[0]


def test_no_later_check_outside_configure(tmp_path):
    run(tmp_path, "abort-upgrade", "1.0.1")
    assert not (tmp_path / "run-calls").exists()
