# SPDX-License-Identifier: GPL-3.0-or-later
"""What the screen says during the install at start-up (2026-11-20, "the
first update is a black screen for a long time"): a step that holds the
number for minutes (the start-up image of each kernel, a driver built, the
boot menu, the restore point) says so, and a start without the boot screen
(plymouth.enable=0, level 1 of the graphics ladder) still shows the
progress, in plain text, instead of a blank console."""
import pytest

from conftest import ru
from test_install import ready_state


@pytest.fixture
def screen_paths(tmp_path, monkeypatch):
    tty = tmp_path / "tty1"
    tty.write_text("")
    monkeypatch.setattr(ru, "TEXT_TTY", str(tty))
    monkeypatch.setattr(ru, "LOCALE_FILES", (str(tmp_path / "locale"),))
    return tmp_path


def messages(rec):
    return [a[1].split("=", 1)[1] for a in rec["plymouth"] if a[0] == "display-message"]


@pytest.fixture
def slow_offline(paths, no_tools, monkeypatch, screen_paths):
    """install_packages reports items like apt does: a kernel, its trigger,
    then a plain package."""
    rec = {"plymouth": no_tools["plymouth"]}
    steps = [("linux-image-7.0.15-raye", 40.0), ("initramfs-tools", 90.0),
             ("firefox:amd64", 95.0)]

    def fake_install(on_percent, on_item=None, **kw):
        for item, pct in steps:
            if on_item:
                on_item(item)
            on_percent(pct)
            rec["plymouth"].append(("--", item, pct))
        return True, ""
    monkeypatch.setattr(ru, "install_packages", fake_install)
    monkeypatch.setattr(ru, "remove_obsolete", lambda: False)
    monkeypatch.setattr(ru, "still_pending", lambda pending: [])
    monkeypatch.setattr(ru, "cached_set_plan", lambda: [])
    monkeypatch.setattr(ru, "dpkg_half_configured", lambda: False)
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=0: (0, "", ""))
    monkeypatch.setattr(ru, "plymouth_running", lambda: True)
    return rec


def test_getting_ready_is_a_slow_step(slow_offline):
    st = ready_state()
    ru.arm(st, "restart")
    ru.install_offline(st, now=1.0)
    m = messages(slow_offline)
    assert m[0] == "raye:mode:updates"
    # the restore point, the TPM and dpkg's own repair come before apt's
    # first number: the note says it can take a while
    assert m[1] == "raye:slow-step"


def test_a_kernel_and_its_start_up_image_say_so_then_the_note_comes_back(slow_offline):
    st = ready_state()
    ru.arm(st, "restart")
    ru.install_offline(st, now=1.0)
    seq = [a for a in slow_offline["plymouth"]
           if a[0] == "--" or (a[0] == "display-message" and "slow" in a[1])]
    names = [a[1] if a[0] == "--" else a[1].split("=", 1)[1] for a in seq]
    assert names == ["raye:slow-step", "linux-image-7.0.15-raye", "initramfs-tools",
                     "raye:slow-step-done", "firefox:amd64"]


@pytest.mark.parametrize("item,slow", [
    ("initramfs-tools", True), ("linux-image-7.0.15-raye", True),
    ("linux-headers-7.0.0-11-generic", True), ("nvidia-dkms-580", True), ("dkms", True),
    ("grub-efi-amd64-signed", True), ("shim-signed", True), ("raye-system", True),
    ("raye-kernel", True), ("firefox:amd64", False), ("libc6:amd64", False),
    ("dpkg-exec", False), ("raye-settings", False)])
def test_which_steps_are_slow(item, slow):
    assert ru.slow_item(item) is slow


# --- no boot screen: the progress in plain text ------------------------------

def text(screen_paths):
    return (screen_paths / "tty1").read_text()


def test_without_the_boot_screen_the_progress_shows_in_text(slow_offline, screen_paths, monkeypatch):
    monkeypatch.setattr(ru, "plymouth_running", lambda: False)
    st = ready_state()
    ru.arm(st, "restart")
    ru.install_offline(st, now=1.0)
    out = text(screen_paths)
    assert "Working on updates  0%" in out
    assert "Working on updates  95%" in out
    assert "Don't turn off your PC" in out
    assert "Preparing the new version of the system. This can take a few minutes." in out
    # the cursor stays hidden (vt.global_cursor_default=0) and the screen is
    # written over, never scrolled line after line
    assert "\033[H\033[2J" in out


def test_in_french_when_the_system_speaks_french(slow_offline, screen_paths, monkeypatch):
    monkeypatch.setattr(ru, "plymouth_running", lambda: False)
    (screen_paths / "locale").write_text('LANG="fr_CA.UTF-8"\n')
    st = ready_state()
    ru.arm(st, "restart")
    ru.install_offline(st, now=1.0)
    out = text(screen_paths)
    assert "Mise à jour en cours  95%" in out
    assert "N'éteignez pas votre PC" in out


def test_with_the_boot_screen_nothing_is_written_to_the_console(slow_offline, screen_paths):
    st = ready_state()
    ru.arm(st, "restart")
    ru.install_offline(st, now=1.0)
    assert text(screen_paths) == ""


def test_a_console_that_cannot_be_written_never_stops_the_install(slow_offline, screen_paths,
                                                                  monkeypatch):
    monkeypatch.setattr(ru, "plymouth_running", lambda: False)
    monkeypatch.setattr(ru, "TEXT_TTY", str(screen_paths / "no" / "tty1"))
    st = ready_state()
    ru.arm(st, "restart")
    assert ru.install_offline(st, now=1.0) in ("restart", "continue")
    assert st.lastInstall["ok"]


def test_install_packages_hands_each_item_over(monkeypatch):
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=0: (0, "", ""))
    lines = ["pmstatus:dpkg-exec:0.0000:Running dpkg",
             "pmstatus:linux-image-7.0.15-raye:40.0:Configuring linux-image-7.0.15-raye",
             "pmstatus:initramfs-tools:90.0:Running post-installation trigger initramfs-tools",
             "pmstatus:firefox:amd64:95.0:Configuring firefox (amd64)"]

    def fake_stream(cmd, cb):
        for l in lines:
            cb(*ru.parse_status(l))
        return 0, ""
    monkeypatch.setattr(ru, "run_with_status", fake_stream)
    items = []
    ok, _ = ru.install_packages(lambda p: None, on_item=items.append)
    assert ok and items == ["dpkg-exec", "linux-image-7.0.15-raye", "initramfs-tools", "firefox:amd64"]
