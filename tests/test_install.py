# SPDX-License-Identifier: GPL-3.0-or-later
"""Arming, and the install at start-up (system-update.target)."""
import os

import pytest

from conftest import fixture, ru

PENDING = ru.parse_simulation(fixture("apt-simulate.txt"))
FIRMWARE = ru.parse_fwupd(fixture("fwupd-get-updates.json"))


def ready_state(**kw):
    st = ru.State(phase="ready", pending=PENDING, count=len(PENDING), needsRestart=True,
                  readyId=ru.ready_key(PENDING, []), kernelChange=True)
    for k, v in kw.items():
        setattr(st, k, v)
    return st


# --- arm / disarm -----------------------------------------------------------

def test_arm_points_system_update_at_us(paths):
    st = ready_state()
    assert ru.arm(st, "restart") == 0
    assert os.readlink(paths["link"]) == paths["target"]
    assert os.path.exists(paths["target"])
    assert st.armed == {"then": "restart", "at": 0} and st.phase == "armed"
    assert ru.offline_owner() == "raye"


def test_arm_twice_changes_the_action(paths):
    st = ready_state()
    ru.arm(st, "restart")
    assert ru.arm(st, "shutdown", 5000) == 0
    assert st.armed == {"then": "shutdown", "at": 5000}


def test_arm_needs_a_downloaded_set(paths):
    assert ru.arm(ru.State(), "restart") == 3
    assert ru.arm(ru.State(phase="downloading", readyId=""), "restart") == 3
    assert not os.path.lexists(paths["link"])


def test_arm_while_updates_install_in_use(paths):
    st = ready_state(phase="installing", installPercent=40)
    assert ru.arm(st, "restart") == 0
    assert os.readlink(paths["link"]) == paths["target"] and st.phase == "armed"
    ru.save(st)
    # the install in use goes on and saves its progress: the arm stays
    live = ready_state(phase="installing", installPercent=60)
    ru.save_run(live)
    st = ru.load()
    assert st.phase == "armed" and st.armed == {"then": "restart", "at": 0}


def test_arm_refused_while_the_start_up_install_runs(paths):
    st = ready_state(phase="installing", armed={"then": "restart", "at": 0})
    assert ru.arm(st, "shutdown") == 3


def test_arm_never_takes_over_another_tool(paths):
    os.symlink("/var/lib/PackageKit/prepared-update", paths["link"])
    st = ready_state()
    assert ru.arm(st, "restart") == 4
    assert os.readlink(paths["link"]) == "/var/lib/PackageKit/prepared-update"
    assert st.armed is None


def test_arm_while_paused_is_allowed(paths):
    st = ready_state(phase="paused", pausedUntil=9e12)
    assert ru.arm(st, "shutdown") == 0


def test_disarm_puts_everything_back(paths):
    st = ready_state()
    ru.arm(st, "restart")
    ru.disarm(st)
    assert not os.path.lexists(paths["link"]) and not os.path.exists(paths["target"])
    assert st.armed is None and st.phase == "ready"


def test_disarm_leaves_another_tool_alone(paths):
    os.symlink("/var/lib/PackageKit/prepared-update", paths["link"])
    st = ready_state()
    ru.disarm(st)
    assert os.path.lexists(paths["link"])


def test_disarm_while_paused_goes_back_to_paused(paths):
    st = ready_state(pausedUntil=9e12)
    ru.arm(st, "restart")
    ru.disarm(st)
    assert st.phase == "paused"


# --- install at start-up ------------------------------------------------------

@pytest.fixture
def offline(paths, no_tools, monkeypatch):
    """An armed set and fake tools; returns the record of what ran."""
    rec = {"dpkg_broken": [False], "apt": [], "fw": [], "plymouth": no_tools["plymouth"]}

    def fake_install(on_percent):
        rec["apt"].append("install")
        for line in fixture("apt-status-install.txt").splitlines():
            kind, _, pct, _ = ru.parse_status(line)
            if kind == "pmstatus":
                on_percent(pct)
        return rec.get("apt_ok", True), "dpkg: error processing firefox"
    monkeypatch.setattr(ru, "install_packages", fake_install)
    monkeypatch.setattr(ru, "remove_obsolete", lambda: False)   # test_obsolete.py
    monkeypatch.setattr(ru, "still_pending", lambda pending: [])   # test_held.py
    # what apt takes from the cache (test_always_latest.py): the set itself
    rec["plan"] = []
    monkeypatch.setattr(ru, "cached_set_plan", lambda: rec["plan"])
    monkeypatch.setattr(ru, "dpkg_half_configured", lambda: rec["dpkg_broken"][0])
    monkeypatch.setattr(ru, "install_firmware",
                        lambda fw: rec["fw"].append(len(fw)) or rec.get("fw_ok", True))
    rec["run"] = []
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=0: rec["run"].append(cmd)
                        or rec.get("run_rc", (0, "", "")))
    return rec


def progress(rec):
    return [int(a[1].split("=")[1]) for a in rec["plymouth"] if a[0] == "system-update"]


def test_install_offline_happy_path(paths, offline):
    st = ready_state()
    ru.arm(st, "shutdown")
    ru.save(st)
    then = ru.install_offline(ru.load(), now=777.0)
    assert then == "shutdown"
    # the mode goes as a message: change-mode makes plymouth read the script
    # again from the disk, where it is the English one
    assert ("display-message", "--text=raye:mode:updates") in offline["plymouth"]
    assert not any(a[0] == "change-mode" for a in offline["plymouth"])
    assert progress(offline) == [0, 0, 12, 25, 62, 87, 100, 100]
    assert ("display-message", "--text=raye:finishing-updates") not in offline["plymouth"]
    st = ru.load()
    assert st.lastInstall == {"time": 777.0, "count": 7, "firmware": 0, "ok": True}
    assert (st.phase, st.count, st.pending, st.readyId, st.needsRestart, st.armed) == \
        ("idle", 0, [], "", False, None)
    assert st.installAttempts == 0
    assert not os.path.lexists(paths["link"])
    assert ru.validate(ru.to_json(st)) == []


def test_install_offline_with_firmware_counts_apt_then_says_firmware(paths, offline, monkeypatch):
    calls = offline["plymouth"]
    monkeypatch.setattr(ru, "install_firmware",
                        lambda fw: calls.append(("firmware",)) or offline["fw"].append(len(fw)) or True)
    st = ready_state(firmware=FIRMWARE)
    ru.arm(st, "restart")
    then = ru.install_offline(st, now=1.0)
    assert then == "restart"
    # apt has the whole bar, like a set without firmware
    assert progress(offline) == [0, 0, 12, 25, 62, 87, 100, 100]
    mode = calls.index(("display-message", "--text=raye:mode:firmware-upgrade"))
    # fwupd gives no progress while it writes: no number may stand still
    # on the screen for the minute that takes
    assert calls[mode + 1] == ("firmware",)
    assert offline["fw"] == [1]
    assert st.lastInstall["firmware"] == 1 and st.firmware == []


def test_install_firmware_names_the_step_when_there_are_several(monkeypatch, tmp_path):
    import hashlib
    shown = []
    monkeypatch.setattr(ru, "plymouth", lambda *a: shown.append(a))
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=0: shown.append(cmd[0]) or (0, "", ""))
    two = []
    for i, data in enumerate((b"one", b"two")):
        fw = dict(FIRMWARE[0], deviceId=f"dev{i}", sha256=hashlib.sha256(data).hexdigest())
        (tmp_path / (fw["sha256"] + ".cab")).write_bytes(data)
        two.append(fw)
    assert ru.install_firmware(two, cache=str(tmp_path))
    steps = [a for a in shown if isinstance(a, tuple)]
    assert steps == [("display-message", "--text=raye:firmware-step:1:2"),
                     ("display-message", "--text=raye:firmware-step:2:2")]
    # each step is said before its write starts
    assert shown.index(steps[1]) < len(shown) - 1 - shown[::-1].index("fwupdmgr")
    shown.clear()
    assert ru.install_firmware(two[:1], cache=str(tmp_path))
    assert not [a for a in shown if isinstance(a, tuple)]


def test_install_offline_firmware_failure_is_reported_packages_kept(paths, offline):
    offline["fw_ok"] = False
    st = ready_state(firmware=FIRMWARE)
    ru.arm(st, "restart")
    ru.install_offline(st, now=1.0)
    assert st.lastInstall["ok"] is True and st.lastInstall["firmware"] == 0
    assert st.phase == "error" and st.error["code"] == "firmware-failed"


def test_install_offline_suspends_the_tpm_for_a_new_shim_or_grub(paths, offline):
    for name in ("shim-signed", "grub-efi-amd64-signed", "secureboot-db"):
        offline["run"].clear()
        pending = PENDING + [{"name": name, "from": "1", "to": "2", "origin": "", "kind": "system"}]
        st = ready_state(pending=pending, count=len(pending))
        ru.arm(st, "restart")
        ru.install_offline(st, now=1.0)
        assert offline["run"] == [["/usr/libexec/raye-tpm", "suspend"]], name


def test_install_offline_suspends_the_tpm_for_a_new_dbx_package(paths, offline):
    # 0.2 Beta 2 had no secureboot-db: it comes in new, with a recommend
    pending = PENDING + [{"name": "secureboot-db", "from": "", "to": "1.9build2",
                          "origin": "Ubuntu:26.04/resolute", "kind": "system"}]
    st = ready_state(pending=pending, count=len(pending))
    ru.arm(st, "restart")
    ru.install_offline(st, now=1.0)
    assert ["/usr/libexec/raye-tpm", "suspend"] in offline["run"]


def test_the_tpm_is_suspended_for_a_shim_the_lists_brought_since(paths, offline):
    """The lists were read again after the set was saved (the newest
    version taken while it waited): the shim is not in the saved set, but
    apt installs it, and the TPM must know before."""
    st = ready_state()
    ru.arm(st, "restart")
    offline["plan"] = [{"name": "shim-signed", "from": "1", "to": "2", "origin": "",
                        "kind": "system"}]
    ru.install_offline(st, now=1.0)
    assert offline["run"] == [["/usr/libexec/raye-tpm", "suspend"]]


def test_tpm_suspend_ignores_the_arch_suffix(paths, offline):
    for name in ("shim-signed:amd64", "secureboot-db:all"):
        offline["run"].clear()
        pending = PENDING + [{"name": name, "from": "1", "to": "2", "origin": "", "kind": "system"}]
        st = ready_state(pending=pending, count=len(pending))
        ru.arm(st, "restart")
        ru.install_offline(st, now=1.0)
        assert offline["run"] == [["/usr/libexec/raye-tpm", "suspend"]], name


def test_install_offline_leaves_the_tpm_alone_for_other_packages(paths, offline):
    st = ready_state()
    ru.arm(st, "restart")
    ru.install_offline(st, now=1.0)
    assert offline["run"] == []


def test_install_offline_a_refused_suspension_does_not_hold_the_update(paths, offline):
    offline["run_rc"] = (4, "", "no TPM binding opens it now")
    pending = PENDING + [{"name": "shim-signed", "from": "1", "to": "2", "origin": "", "kind": "system"}]
    st = ready_state(pending=pending, count=len(pending))
    ru.arm(st, "restart")
    ru.install_offline(st, now=1.0)
    assert offline["apt"] == ["install"] and st.lastInstall["ok"] is True


def test_install_offline_does_nothing_when_another_tool_armed(paths, offline):
    os.symlink("/var/lib/PackageKit/prepared-update", paths["link"])
    assert ru.install_offline(ready_state()) is None
    assert offline["apt"] == []
    assert os.path.lexists(paths["link"])


def test_install_offline_failure_with_dpkg_clean_gives_up_at_once(paths, offline):
    offline["apt_ok"] = False
    st = ready_state()
    ru.arm(st, "shutdown")
    assert ru.install_offline(st, now=2.0) == "shutdown"
    assert st.phase == "error" and st.error["code"] == "install-failed"
    assert st.lastInstall == {"time": 2.0, "count": 7, "firmware": 0, "ok": False}
    assert not os.path.lexists(paths["link"])      # no boot loop
    assert st.readyId == ""                         # the next check offers the set again
    assert ru.validate(ru.to_json(st)) == []


def test_install_offline_failure_with_dpkg_half_configured_tries_again(paths, offline):
    offline["apt_ok"] = False
    offline["dpkg_broken"][0] = True
    st = ready_state()
    ru.arm(st, "shutdown")
    ru.save(st)
    assert ru.install_offline(ru.load()) == "restart"   # restart into the next try, not off
    assert os.path.lexists(paths["link"])
    st = ru.load()
    assert st.phase == "installing" and st.installAttempts == 1

    # the next start-up says "Finishing updates" and succeeds
    offline["apt_ok"] = True
    offline["plymouth"].clear()
    assert ru.install_offline(st, now=3.0) == "shutdown"
    assert ("display-message", "--text=raye:finishing-updates") in offline["plymouth"]
    assert st.lastInstall["ok"] and not os.path.lexists(paths["link"])


def test_install_offline_power_cut_is_finished_at_the_next_start(paths, offline):
    st = ready_state()
    ru.arm(st, "restart")
    st.phase, st.installAttempts = "installing", 1      # what the cut left behind
    ru.install_offline(st, now=4.0)
    assert ("display-message", "--text=raye:finishing-updates") in offline["plymouth"]
    assert st.lastInstall["ok"]


def test_install_offline_stops_after_three_starts(paths, offline):
    offline["apt_ok"] = False
    offline["dpkg_broken"][0] = True
    st = ready_state()
    ru.arm(st, "restart")
    for _ in range(ru.MAX_ATTEMPTS):
        ru.install_offline(st)
    assert not os.path.lexists(paths["link"])
    assert st.phase == "error" and st.lastInstall["ok"] is False
    # and a fourth start does not loop
    assert ru.install_offline(st) is None


def test_install_offline_never_prints(paths, offline, capsys):
    st = ready_state()
    ru.arm(st, "restart")
    ru.install_offline(st)
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_install_packages_retries_once_with_apt_recovery(monkeypatch):
    runs, streams = [], []
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=0: runs.append(cmd) or (0, "", ""))

    def fake_stream(cmd, cb):
        streams.append(cmd)
        return (1, "boom") if len(streams) == 1 else (0, "")
    monkeypatch.setattr(ru, "run_with_status", fake_stream)
    ok, _ = ru.install_packages(lambda p: None)
    assert ok and len(streams) == 2
    assert runs[0][:3] == ["dpkg", "--configure", "-a"]
    assert ["-f", "install"] == runs[-1][-2:]
    assert "--no-download" in streams[0] and streams[0][-2:] == ["upgrade", "--with-new-pkgs"]


def cached(tmp_path, data=b"cab"):
    """FIRMWARE with its cabinet already downloaded under tmp_path."""
    import hashlib
    fw = [dict(FIRMWARE[0], sha256=hashlib.sha256(data).hexdigest())]
    (tmp_path / (fw[0]["sha256"] + ".cab")).write_bytes(data)
    return fw


def test_parse_fwupd_keeps_checksum_and_location():
    assert FIRMWARE[0]["sha256"] == "1bf2dd354e35e86138478f17c83724be644e0dc4c2b82fa95a2cfc7ebfad13d5"
    assert FIRMWARE[0]["location"].startswith("https://fwupd.org/downloads/")
    assert ru.validate(ru.to_json(ru.State(firmware=FIRMWARE))) == []


def test_install_firmware_prepares_the_tpm_first(monkeypatch, tmp_path):
    runs = []
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=0: runs.append(cmd) or (0, "", ""))
    fw = cached(tmp_path)
    assert ru.install_firmware(fw, cache=str(tmp_path))
    assert runs[0] == ["/usr/libexec/raye-tpm", "prepare-firmware"]
    # no network at start-up: the cabinet downloaded beforehand is installed
    assert runs[1][:4] == ["fwupdmgr", "local-install", str(tmp_path / (fw[0]["sha256"] + ".cab")),
                           fw[0]["deviceId"]]
    assert "--no-reboot-check" in runs[1] and "update" not in runs[1]
    # fwupd has written the new variables now: raye-tpm predicts them
    assert runs[2] == ["/usr/libexec/raye-tpm", "prepare-firmware"] and len(runs) == 3


def test_install_firmware_predicts_again_after_a_failed_install(monkeypatch, tmp_path):
    runs = []
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=0: runs.append(cmd) or
                        ((1, "", "") if cmd[0] == "fwupdmgr" else (0, "", "")))
    assert not ru.install_firmware(cached(tmp_path), cache=str(tmp_path))
    assert runs[-1] == ["/usr/libexec/raye-tpm", "prepare-firmware"]


def test_install_firmware_skipped_when_the_tpm_cannot_be_prepared(monkeypatch, tmp_path):
    runs = []
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=0: runs.append(cmd) or (1, "", "no"))
    assert not ru.install_firmware(cached(tmp_path), cache=str(tmp_path))
    assert len(runs) == 1


def test_install_firmware_without_raye_tpm_installed(monkeypatch, tmp_path):
    answers = {"/usr/libexec/raye-tpm": (127, "", ""), "fwupdmgr": (0, "", "")}
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=0: answers[cmd[0]])
    assert ru.install_firmware(cached(tmp_path), cache=str(tmp_path))
    assert ru.install_firmware([])


@pytest.mark.parametrize("rc, installed", [(0, True), (3, True), (1, False), (2, False), (4, False)])
def test_install_firmware_follows_what_raye_tpm_says(monkeypatch, tmp_path, rc, installed):
    runs = []
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=0: runs.append(cmd) or
                        ((rc, "", "") if cmd[0].endswith("raye-tpm") else (0, "", "")))
    assert ru.install_firmware(cached(tmp_path), cache=str(tmp_path)) is installed
    assert any(c[0] == "fwupdmgr" for c in runs) is installed


def test_install_firmware_skipped_when_not_downloaded(monkeypatch, tmp_path):
    runs = []
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=0: runs.append(cmd) or (0, "", ""))
    assert not ru.install_firmware(FIRMWARE, cache=str(tmp_path))
    assert runs == []                   # nothing touched, not even the TPM


def test_install_firmware_skips_a_cabinet_that_does_not_match(monkeypatch, tmp_path):
    runs = []
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=0: runs.append(cmd) or (0, "", ""))
    fw = cached(tmp_path)
    (tmp_path / (fw[0]["sha256"] + ".cab")).write_bytes(b"something else")
    assert not ru.install_firmware(fw, cache=str(tmp_path))
    assert runs == []


def test_download_firmware_checks_and_cleans(tmp_path):
    import hashlib
    data = b"real cabinet"
    fw = [dict(FIRMWARE[0], sha256=hashlib.sha256(data).hexdigest())]
    (tmp_path / "old.cab").write_bytes(b"left from an older set")
    got = []

    def get(url, dest):
        got.append(url)
        with open(dest, "wb") as f:
            f.write(data)
        return True
    assert ru.download_firmware(fw, cache=str(tmp_path), get=get)
    assert got == [fw[0]["location"]] and ru.firmware_ready(fw[0], str(tmp_path))
    assert not (tmp_path / "old.cab").exists()
    assert ru.download_firmware(fw, cache=str(tmp_path), get=get) and len(got) == 1


def test_download_firmware_drops_a_bad_download(tmp_path):
    def get(url, dest):
        with open(dest, "wb") as f:
            f.write(b"tampered")
        return True
    assert not ru.download_firmware(FIRMWARE, cache=str(tmp_path), get=get)
    assert list(tmp_path.iterdir()) == []
    no_url = [dict(FIRMWARE[0], location="")]
    assert not ru.download_firmware(no_url, cache=str(tmp_path), get=get)


def test_power_does_not_wait_for_the_reboot(monkeypatch):
    runs = []
    monkeypatch.setattr(ru.subprocess, "run", lambda cmd, **kw: runs.append(cmd))
    ru.power("restart")
    ru.power("shutdown")
    assert runs == [["systemctl", "--no-block", "reboot"], ["systemctl", "--no-block", "poweroff"]]


def test_fetch_uses_fwupds_own_downloader(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "fwupdmgr"
    # writes the file named after the address into the folder it runs in
    fake.write_text('#!/bin/sh\n[ "$1" = download ] || exit 9\nprintf cab > "$(basename "$2")"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    dest = str(tmp_path / "x.cab")
    assert ru.fetch("https://fwupd.org/downloads/abc-x.cab", dest)
    assert open(dest).read() == "cab"
    assert sorted(os.listdir(tmp_path)) == ["bin", "x.cab"]      # no work folder left
    fake.write_text("#!/bin/sh\nexit 1\n")
    assert not ru.fetch("https://fwupd.org/downloads/abc-y.cab", str(tmp_path / "y.cab"))
    assert not os.path.exists(tmp_path / "y.cab")


# --- one restart, not two ---------------------------------------------------

# what an update set looks like without the kernel, drivers or boot loaders
PLAIN = [p for p in PENDING if p["name"] in ("libc6", "firefox", "libgl1:i386")]


def boot_file(paths, name, text="1"):
    path = os.path.join(paths["boot"], name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return path


def test_plain_updates_go_on_to_the_desktop(paths, offline):
    boot_file(paths, "vmlinuz-7.0.0-11-generic")
    boot_file(paths, "initrd.img-7.0.0-11-generic")
    st = ready_state(pending=PLAIN, count=len(PLAIN), kernelChange=False)
    ru.arm(st, "restart")
    assert ru.install_offline(st, now=1.0) == "continue"
    assert not os.path.lexists(paths["link"])   # removed before the start-up goes on
    assert st.lastInstall["ok"] is True


def test_shutdown_still_turns_the_pc_off(paths, offline):
    st = ready_state(pending=PLAIN, count=len(PLAIN), kernelChange=False)
    ru.arm(st, "shutdown")
    assert ru.install_offline(st, now=1.0) == "shutdown"


@pytest.mark.parametrize("name", ["linux-image-7.0.0-12-generic", "linux-modules-7.0.0-12-generic",
                                  "linux-signed-image-7.0.0-12-generic", "shim-signed",
                                  "grub-efi-amd64-signed", "nvidia-driver-580",
                                  "libnvidia-gl-580", "v4l2loopback-dkms"])
def test_a_new_kernel_driver_or_boot_loader_restarts(paths, offline, name):
    pending = PLAIN + [{"name": name, "from": "1", "to": "2", "origin": "", "kind": "system"}]
    st = ready_state(pending=pending, count=len(pending))
    ru.arm(st, "restart")
    assert ru.install_offline(st, now=1.0) == "restart"


def test_headers_alone_need_no_restart(paths, offline):
    pending = PLAIN + [{"name": "linux-headers-7.0.0-12-generic", "from": "1", "to": "2",
                        "origin": "", "kind": "kernel"}]
    st = ready_state(pending=pending, count=len(pending))
    ru.arm(st, "restart")
    assert ru.install_offline(st, now=1.0) == "continue"


@pytest.mark.parametrize("name", ["initrd.img-7.0.0-11-generic", "vmlinuz-7.0.0-11-generic",
                                  "efi/EFI/ubuntu/grubx64.efi", "efi/EFI/BOOT/BOOTX64.EFI"])
def test_a_changed_boot_file_restarts(paths, offline, monkeypatch, name):
    """A package that rebuilds the initramfs (microcode, cryptsetup, the boot
    screen) or a boot loader written to the ESP: the next start must use it."""
    boot_file(paths, name, "old")
    real = ru.install_packages

    def install_and_touch(on_percent):
        boot_file(paths, name, "new, longer")
        return real(on_percent)
    monkeypatch.setattr(ru, "install_packages", install_and_touch)
    st = ready_state(pending=PLAIN, count=len(PLAIN), kernelChange=False)
    ru.arm(st, "restart")
    assert ru.install_offline(st, now=1.0) == "restart"


def test_a_new_boot_file_restarts(paths, offline, monkeypatch):
    real = ru.install_packages

    def install_and_add(on_percent):
        boot_file(paths, "initrd.img-7.0.0-12-generic")
        return real(on_percent)
    monkeypatch.setattr(ru, "install_packages", install_and_add)
    st = ready_state(pending=PLAIN, count=len(PLAIN), kernelChange=False)
    ru.arm(st, "restart")
    assert ru.install_offline(st, now=1.0) == "restart"


def test_firmware_restarts(paths, offline):
    st = ready_state(pending=PLAIN, count=len(PLAIN), kernelChange=False, firmware=FIRMWARE)
    ru.arm(st, "restart")
    assert ru.install_offline(st, now=1.0) == "restart"


def test_a_failed_firmware_restarts_too(paths, offline):
    """fwupd may have written part of it: never guess, start again."""
    offline["fw_ok"] = False
    st = ready_state(pending=PLAIN, count=len(PLAIN), kernelChange=False, firmware=FIRMWARE)
    ru.arm(st, "restart")
    assert ru.install_offline(st, now=1.0) == "restart"


def test_a_clean_failure_goes_on_to_the_desktop(paths, offline):
    """Nothing half-installed and nothing under /boot changed: the desktop
    shows the error, no second restart."""
    offline["apt_ok"] = False
    st = ready_state(pending=PLAIN, count=len(PLAIN), kernelChange=False)
    ru.arm(st, "restart")
    assert ru.install_offline(st, now=1.0) == "continue"
    assert st.phase == "error" and not os.path.lexists(paths["link"])


def test_going_on_clears_the_update_words(paths, offline):
    st = ready_state(pending=PLAIN, count=len(PLAIN), kernelChange=False)
    ru.arm(st, "restart")
    ru.install_offline(st, now=1.0)
    assert offline["plymouth"][-1] == ("display-message", "--text=raye:mode:boot")


def test_boot_files_reads_kernels_initramfs_and_the_esp(paths):
    boot_file(paths, "vmlinuz-7.0.0-11-generic")
    boot_file(paths, "initrd.img-7.0.0-11-generic")
    boot_file(paths, "config-7.0.0-11-generic")
    boot_file(paths, "efi/EFI/ubuntu/shimx64.efi")
    boot_file(paths, "grub/grub.cfg")
    names = sorted(os.path.relpath(p, paths["boot"]) for p in ru.boot_files())
    assert names == ["efi/EFI/ubuntu/shimx64.efi", "initrd.img-7.0.0-11-generic",
                     "vmlinuz-7.0.0-11-generic"]


# --- going on without a restart ------------------------------------------------

@pytest.fixture
def systemctl(monkeypatch, no_tools):
    rec = {"cmds": [], "default": "graphical.target", "rc": {}}

    def fake_run(cmd, timeout=0):
        rec["cmds"].append(cmd)
        rc = rec["rc"].get(cmd[1] if cmd[0] == "systemctl" and cmd[1] != "--no-block" else cmd[2], 0)
        if "show" in cmd:
            return rc, rec["default"] + "\n", ""
        return rc, "", ""
    monkeypatch.setattr(ru, "run", fake_run)
    return rec


def test_continue_boot_reloads_then_starts_the_normal_target(systemctl, no_tools):
    assert ru.continue_boot() is True
    # plymouth-quit.service does the hand-over to the sign-in screen
    assert no_tools["plymouth"] == []
    assert systemctl["cmds"] == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "show", "--property=Id", "--value", "default.target"],
        ["systemctl", "--no-block", "start", "default.target"]]


def test_continue_boot_refuses_while_the_update_target_is_still_default(systemctl):
    systemctl["default"] = "system-update.target"
    assert ru.continue_boot() is False
    assert ["systemctl", "--no-block", "start", "default.target"] not in systemctl["cmds"]


@pytest.mark.parametrize("step", ["daemon-reload", "show", "start"])
def test_continue_boot_says_no_when_a_step_fails(systemctl, step):
    systemctl["rc"][step] = 1
    assert ru.continue_boot() is False


def test_continue_boot_never_isolates(systemctl):
    """isolate stops every unit the desktop target does not list, and so the
    services udev started for the hardware: measured in a VM, the guest
    agent stayed off until the next restart."""
    ru.continue_boot()
    assert not any("isolate" in c for c in systemctl["cmds"])


def test_upgrading_this_package_never_stops_the_install():
    """raye-update is often in the set it installs. Its preinst used to stop
    raye-update-install.service, that is the install itself: dpkg was killed
    in the middle, FailureAction restarted the PC, and the next start said
    "Finishing updates" and did it again, three times."""
    rules = open(os.path.join(os.path.dirname(__file__), "..", "debian", "rules")).read()
    line = [l for l in rules.splitlines() if "raye-update-install" in l and "dh_installsystemd" in l]
    assert line and "--no-stop-on-upgrade" in line[0]


# --- restore points ------------------------------------------------------------

@pytest.fixture
def snapper(offline, tmp_path, monkeypatch):
    config = tmp_path / "snapper-root"
    config.write_text("")
    monkeypatch.setattr(ru, "SNAPPER_CONFIG", str(config))
    monkeypatch.setattr(ru, "RESTORE_POINT", str(tmp_path / "lib" / "restore-point"))
    seen = {"env": []}

    def fake_run(cmd, timeout=0):
        offline["run"].append(cmd)
        if cmd[0] == "snapper" and "pre" in cmd:
            return 0, "42\n", ""
        return 0, "", ""

    def fake_install(on_percent):
        seen["env"].append(ru.APT_ENV.get("DISABLE_APT_SNAPSHOT"))
        return offline.get("apt_ok", True), ""
    monkeypatch.setattr(ru, "run", fake_run)
    monkeypatch.setattr(ru, "install_packages", fake_install)
    return seen


def snapper_calls(rec):
    return [c for c in rec["run"] if c[0] == "snapper"]


def test_updates_get_one_pair_of_restore_points(paths, offline, snapper):
    st = ready_state()
    ru.arm(st, "restart")
    ru.install_offline(ru.load(), now=1.0)
    pre, post = snapper_calls(offline)
    assert "pre" in pre and "important=yes,raye=update" in pre
    assert any(a.startswith("Before updates of ") for a in pre)
    assert post[post.index("--pre-number") + 1] == "42" and "post" in post
    # apt's own pair is off while we install, and on again after
    assert snapper["env"] == ["yes"]
    assert "DISABLE_APT_SNAPSHOT" not in ru.APT_ENV
    assert not os.path.exists(ru.RESTORE_POINT)


def test_interrupted_install_keeps_its_first_restore_point(paths, offline, snapper):
    st = ready_state()
    ru.arm(st, "restart")
    offline["apt_ok"] = False
    offline["dpkg_broken"][0] = True
    assert ru.install_offline(ru.load(), now=1.0) == "restart"
    assert len(snapper_calls(offline)) == 1
    offline["apt_ok"] = True
    offline["dpkg_broken"][0] = False
    ru.install_offline(ru.load(), now=2.0)
    calls = snapper_calls(offline)
    assert len(calls) == 2 and "post" in calls[1]


def test_no_snapper_no_restore_point(paths, offline):
    st = ready_state()
    ru.arm(st, "restart")
    ru.install_offline(ru.load(), now=1.0)
    assert not snapper_calls(offline)


# --- the screen on the way down ---------------------------------------------

UNITS = os.path.join(os.path.dirname(__file__), "..", "data")


def unit_keys(name):
    keys, section = {}, None
    for line in open(os.path.join(UNITS, name)).read().splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            keys[(section, k)] = v
    return keys


def test_restart_into_updates_says_so_at_once(paths, no_tools):
    st = ready_state()
    ru.arm(st, "restart")
    assert ru.shutdown_screen() == 0
    assert no_tools["plymouth"] == [("display-message", "--text=raye:mode:prepare")]


def test_restart_sets_again_a_start_up_install_packagekit_took_away(paths, no_tools):
    # PackageKit drops /system-update whenever apt tells it the lists or the
    # packages changed (StateHasChanged cache-update, from 20packagekit),
    # whoever made the link. Seen on VM 130 on 2026-11-20: the timer's look
    # at the lists while an update waited for the restart took the link away
    # a few seconds later; the state still said "armed", the restart started
    # normally and nothing was installed, every time.
    st = ready_state()
    ru.arm(st, "restart")
    ru.save(st)
    os.unlink(paths["link"])
    assert ru.shutdown_screen() == 0
    assert os.readlink(paths["link"]) == paths["target"]
    assert open(paths["target"]).read().strip() == st.readyId
    assert ru.load().armed == {"then": "restart", "at": 0}
    assert no_tools["plymouth"] == [("display-message", "--text=raye:mode:prepare")]


def test_restart_never_takes_over_another_tools_install(paths, no_tools):
    st = ready_state()
    ru.arm(st, "restart")
    ru.save(st)
    os.unlink(paths["link"])
    os.symlink("/var/lib/someone-else", paths["link"])
    assert ru.shutdown_screen() == 0
    assert os.readlink(paths["link"]) == "/var/lib/someone-else"
    assert no_tools["plymouth"] == []


def test_restart_does_not_arm_what_was_not_armed(paths, no_tools):
    ru.save(ready_state())
    assert ru.shutdown_screen() == 0
    assert not os.path.lexists(paths["link"])
    assert no_tools["plymouth"] == []


@pytest.mark.parametrize("armed", [None, "other"])
def test_plain_restart_keeps_its_words(paths, no_tools, armed):
    if armed:
        os.symlink("/var/lib/someone-else", paths["link"])
    assert ru.shutdown_screen() == 0
    assert no_tools["plymouth"] == []


def test_shutdown_screen_unit_runs_on_the_way_down():
    u = unit_keys("raye-update-shutdown-screen.service")
    assert u[("[Unit]", "DefaultDependencies")] == "no"
    after = u[("[Unit]", "After")].split()
    for p in ("plymouth-reboot.service", "plymouth-poweroff.service", "plymouth-halt.service"):
        assert p in after
    before = u[("[Unit]", "Before")].split()
    for p in ("systemd-reboot.service", "systemd-poweroff.service", "systemd-halt.service"):
        assert p in before
    # the code decides: a link PackageKit took away is set again here, so a
    # condition on the link would skip exactly that case
    assert ("[Unit]", "ConditionPathIsSymbolicLink") not in u
    # started once PackageKit and the last look at the lists are down: they
    # are what takes the link away
    for p in ("packagekit.service", "raye-update-catch-up.service"):
        assert p in after
    assert u[("[Service]", "ExecStart")] == "/usr/libexec/raye-update shutdown-screen"
    assert set(u[("[Install]", "WantedBy")].split()) == {"reboot.target", "poweroff.target",
                                                          "halt.target"}
    here = os.path.dirname(__file__)
    assert "data/raye-update-shutdown-screen.service usr/lib/systemd/system" in \
        open(os.path.join(here, "..", "debian", "install")).read()
    rules = open(os.path.join(here, "..", "debian", "rules")).read()
    assert "raye-update-shutdown-screen" in rules


def test_progress_unit_survives_a_burst_of_state_writes():
    # the install rewrites state.json several times a second; the default
    # start limit failed the service and took raye-update-progress.path down
    assert unit_keys("raye-update-progress.service")[("[Unit]", "StartLimitIntervalSec")] == "0"
