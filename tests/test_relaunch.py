"""Open Raye apps updated in use are closed and opened again; apps where a
person writes are never touched."""
import os

import raye_update as ru


def fake_proc(tmp_path, procs):
    """procs: {pid: (uid, exe)}; a /proc with exe links and owners."""
    root = tmp_path / "proc"
    root.mkdir()
    for pid, (uid, exe) in procs.items():
        d = root / str(pid)
        d.mkdir()
        os.symlink(exe, d / "exe")
        os.chown(d, uid, -1) if os.geteuid() == 0 else None
    return str(root)


def test_updated_app_is_closed_and_opened_again(tmp_path):
    proc = fake_proc(tmp_path, {
        101: (1000, "/usr/bin/dolphin (deleted)"),
        102: (1000, "/usr/bin/kate"),
        103: (0, "/usr/bin/dolphin"),
    })
    if os.geteuid() != 0:
        return  # owners can only be set as root (the build runs as root)
    killed, ran = [], []
    done = ru.relaunch_updated_apps(
        {"raye-files", "raye-notepad"}, proc=proc,
        kill=lambda pid, sig: killed.append((pid, sig)),
        runner=lambda cmd, timeout=0: (ran.append(cmd), (0, "", ""))[1],
        alive=lambda pid: False, sleep=lambda s: None,
        user_of=lambda uid: "anna")
    assert killed == [(101, 15)]
    assert done == [("anna", "dolphin")]
    assert ran[0][:4] == ["systemd-run", "--machine=anna@.host", "--user", "--collect"]
    assert ran[0][-1] == "/usr/bin/dolphin"


def test_nothing_updated_from_the_list_touches_nothing(tmp_path):
    proc = fake_proc(tmp_path, {201: (1000, "/usr/bin/kate")})
    assert ru.running_apps({"raye-notepad", "linux-firmware"}, proc=proc) == {}


def test_a_missing_proc_does_not_fail(tmp_path):
    assert ru.running_apps({"raye-files"}, proc=str(tmp_path / "nothing")) == {}
