# SPDX-License-Identifier: GPL-3.0-or-later
"""Raye's own packages built from one source move together.

2026-11-19, a Dell Latitude 5490 going from 0.5 to 1.0 Beta: the install in
use took raye-desktop 1.0 and left raye-system at 0.5 for the next restart
(raye-system 1.0 needed grub-pc-bin, a boot package that only installs at
start-up). raye-desktop's sign-in settings named a helper only raye-system
1.0 has: after a plain restart, no sign-in screen. Against the real apt, a
small archive shaped like that one; dpkg itself never runs."""
import email.utils
import hashlib
import os
import shutil

import pytest

from conftest import ru

pytestmark = pytest.mark.skipif(
    not shutil.which("apt-get") or not os.path.exists("/usr/lib/apt/methods/copy"),
    reason="needs apt")

# name, version, source, depends
ARCHIVE = [
    ("grub-pc-bin", "2", "grub2", ""),
    ("raye-system", "2", "raye-base", "grub-pc-bin"),
    # the 1.0~beta1.1 raye-desktop: raye-system without a version
    ("raye-desktop", "2", "raye-base", "raye-system"),
    ("raye-settings", "2", "raye-settings", ""),
    ("hello", "2", "hello", ""),
]


def make_archive(repo, packages):
    os.makedirs(repo)
    paras = []
    for name, version, source, depends in packages:
        data = f"{name} {version}\n".encode() * 64
        filename = f"{name}_{version}_all.deb"
        with open(os.path.join(repo, filename), "wb") as f:
            f.write(data)
        lines = [f"Package: {name}", f"Source: {source}", f"Version: {version}",
                 "Architecture: all", "Maintainer: Raye <raye@example.org>",
                 f"Filename: ./{filename}", f"Size: {len(data)}",
                 f"SHA256: {hashlib.sha256(data).hexdigest()}", "Description: test"]
        if depends:
            lines.append(f"Depends: {depends}")
        paras.append("\n".join(lines))
    packages_text = ("\n\n".join(paras) + "\n").encode()
    with open(os.path.join(repo, "Packages"), "wb") as f:
        f.write(packages_text)
    with open(os.path.join(repo, "Release"), "w") as f:
        f.write(f"Origin: Raye\nLabel: Raye\nSuite: resolute\nCodename: resolute\n"
                f"Date: {email.utils.formatdate(usegmt=True)}\nSHA256:\n"
                f" {hashlib.sha256(packages_text).hexdigest()} {len(packages_text)} Packages\n")


def status_text(installed):
    return "".join(
        f"Package: {n}\nStatus: install ok installed\nVersion: {v}\nSource: {s}\n"
        f"Architecture: all\nMaintainer: Raye <raye@example.org>\nDescription: test\n\n"
        for n, v, s in installed)


@pytest.fixture
def raye_apt(tmp_path, paths, no_tools, monkeypatch):
    repo, root = str(tmp_path / "repo"), tmp_path / "root"
    for d in ("etc/apt/preferences.d", "var/lib/dpkg", "var/lib/apt/lists/partial",
              "var/cache/apt/archives/partial", "log"):
        (root / d).mkdir(parents=True)
    status = root / "var/lib/dpkg/status"
    (root / "etc/apt/sources.list").write_text(f"deb [trusted=yes] copy:{repo} ./\n")
    opts = ["-o", f"Dir={root}", "-o", f"Dir::State::status={status}",
            "-o", f"Dir::Etc={root}/etc/apt", "-o", "Dir::Etc::sourceparts=-",
            "-o", "Dir::Etc::parts=-", "-o", f"Dir::Log={root}/log",
            "-o", "APT::Architecture=amd64", "-o", "Debug::NoLocking=1",
            "-o", "Dir::Bin::dpkg=/bin/true",
            "-o", "APT::Machine-ID=0123456789abcdef0123456789abcdef"]
    monkeypatch.setattr(ru, "APT_OPTS", ru.APT_OPTS + opts)
    monkeypatch.setattr(ru, "ARCHIVES", str(root / "var/cache/apt/archives"))
    monkeypatch.setattr(ru, "DPKG_STATUS", str(status), raising=False)
    real_run = ru.run
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=3600: (0, "", "")
                        if cmd[0].startswith("dpkg") else real_run(cmd, timeout))
    for name, value in (("battery_level", lambda: (True, None)), ("is_metered", lambda: False),
                        ("is_gaming", lambda: False), ("free_bytes", lambda p: 50 << 30),
                        ("list_firmware", lambda: []), ("obsolete_plan", lambda coming=(): []),
                        ("dpkg_half_configured", lambda: False), ("live_running", lambda: False),
                        ("start_live", lambda: None), ("restore_point_before", lambda now=None: None),
                        ("restore_point_after", lambda point: None),
                        ("relaunch_updated_apps", lambda names: None),
                        ("restart_user_units", lambda names: None)):
        monkeypatch.setattr(ru, name, value)

    def setup(installed, archive=ARCHIVE):
        make_archive(repo, archive)
        status.write_text(status_text(installed))
        rc, out, err = ru.run(["apt-get", "update", *ru.APT_OPTS])
        assert rc == 0, out + err
    return setup


OLD = [("raye-system", "1", "raye-base"), ("raye-desktop", "1", "raye-base"),
       ("raye-settings", "1", "raye-settings"), ("hello", "1", "hello")]


def ready(state):
    return ru.download(ru.check(state))


def test_the_case_of_the_latitude_reproduced(raye_apt):
    """Without the rule, apt alone takes raye-desktop and leaves raye-system."""
    raye_apt(OLD)
    pending = ru.parse_simulation(ru.run(["apt-get", "-s", *ru.APT_OPTS, *ru.UPGRADE])[1])
    offline = ru.offline_names(pending)
    assert offline == {"grub-pc-bin"}
    with ru.pins_file(pending, offline) as pins:
        sim = ru.parse_simulation(ru.run(["apt-get", "-s", *ru.APT_OPTS, *pins, *ru.UPGRADE])[1])
    assert {p["name"] for p in sim} == {"raye-desktop", "raye-settings", "hello"}


def test_one_source_waits_whole_when_part_of_it_needs_a_restart(raye_apt):
    raye_apt(OLD)
    st = ready(ru.State())
    restart = {p["name"]: p["restart"] for p in st.pending}
    assert restart == {"grub-pc-bin": True, "raye-system": True, "raye-desktop": True,
                       "raye-settings": False, "hello": False}


def test_the_install_in_use_never_splits_a_raye_source(raye_apt, monkeypatch):
    raye_apt(OLD)
    st = ready(ru.State())
    pins = []

    def stream(cmd, on_status):
        for a in cmd:
            if a.startswith("Dir::Etc::Preferences="):
                with open(a.split("=", 1)[1]) as f:
                    pins.append(f.read())
        return 0, ""
    monkeypatch.setattr(ru, "run_with_status", stream)
    ru.install_live(st)
    assert len(pins) == 1
    held = {para.splitlines()[0].split()[1] for para in pins[0].split("\n\n")
            if para.startswith("Package: ")}
    assert {"grub-pc-bin", "raye-system", "raye-desktop"} <= held
    assert not held & {"raye-settings", "hello"}


def test_a_source_apt_keeps_back_waits_whole(raye_apt):
    """Not a restart: raye-system's new dependency cannot be had at all, so
    apt keeps raye-system back; raye-desktop waits with it."""
    archive = [a for a in ARCHIVE if a[0] != "grub-pc-bin"]
    raye_apt(OLD, archive)
    st = ready(ru.State())
    restart = {p["name"]: p["restart"] for p in st.pending}
    assert "raye-system" not in restart
    assert restart["raye-desktop"] is True and restart["raye-settings"] is False


def test_ubuntu_packages_are_not_grouped(raye_apt):
    """The rule is for Raye's own: one Ubuntu source may still move in part."""
    archive = ARCHIVE + [("libfoo1", "2", "foo", ""), ("kwin-foo", "2", "foo", "")]
    raye_apt(OLD + [("libfoo1", "1", "foo"), ("kwin-foo", "1", "foo")], archive)
    pending = ru.parse_simulation(ru.run(["apt-get", "-s", *ru.APT_OPTS, *ru.UPGRADE])[1])
    for p in pending:
        if p["name"] in ("libfoo1", "kwin-foo"):
            p["origin"] = "Ubuntu:26.04/resolute-updates"
    offline = ru.offline_names(pending)
    assert "kwin-foo" in offline
    grown = ru.whole_raye_sources(pending, offline)
    assert "libfoo1" not in grown
    assert {"raye-system", "raye-desktop"} <= grown


def test_a_pc_already_split_finishes_at_the_next_start(raye_apt, paths):
    """The PCs the 0.5 Raye Update left that way: raye-desktop 2 is in,
    raye-system 1 waits for a restart nobody asks for, and the sign-in
    screen does not come up. Any next start, even a plain one, finishes."""
    raye_apt([("raye-system", "1", "raye-base"), ("raye-desktop", "2", "raye-base"),
              ("raye-settings", "2", "raye-settings"), ("hello", "2", "hello")])
    st = ru.State()
    ru.run_auto(st, "auto", 1000.0)
    st = ru.load()
    assert st.phase == "armed" and st.armed["then"] == "restart"
    assert os.path.islink(paths["link"])


def test_a_set_that_only_waits_for_a_restart_is_not_armed(raye_apt, paths):
    """Nothing split: the restart stays the person's choice."""
    raye_apt(OLD)
    ru.run_auto(ru.State(), "auto", 1000.0)
    st = ru.load()
    assert st.phase == "ready" and not st.armed
    assert not os.path.lexists(paths["link"])


def test_split_on_disk_reads_the_dpkg_status(raye_apt):
    raye_apt([("raye-system", "1", "raye-base"), ("raye-desktop", "2", "raye-base (2)")])
    pending = [{"name": "raye-system", "from": "1", "to": "2", "origin": "Raye:resolute",
                "kind": "system", "restart": True}]
    assert ru.split_raye_sources(pending) == {"raye-base"}
    # the hotfix: the one that waits now goes past the one that went ahead
    pending[0]["to"] = "3"
    assert ru.split_raye_sources(pending) == {"raye-base"}
    pending[0]["origin"] = "Ubuntu:26.04/resolute-updates"
    assert ru.split_raye_sources(pending) == set()


def test_a_package_left_behind_by_an_older_version_is_no_split(raye_apt):
    raye_apt([("raye-system", "2", "raye-base"), ("raye-old-thing", "1", "raye-base")])
    pending = [{"name": "raye-system", "from": "2", "to": "3", "origin": "Raye:resolute",
                "kind": "system", "restart": True}]
    assert ru.split_raye_sources(pending) == set()
    # and one that installs in use is no reason to arm the restart
    pending[0]["restart"] = False
    assert ru.split_raye_sources(pending) == set()


def test_the_upgrade_of_raye_update_arms_a_split_pc_at_once(raye_apt, paths):
    """The new Raye Update arrives in use on a split PC: its postinst arms
    the rest, so the next start (even a forced one) finishes it."""
    raye_apt([("raye-system", "1", "raye-base"), ("raye-desktop", "2", "raye-base")])
    st = ready(ru.State())
    ru.mark_restart(st.pending)
    ru.save(st)
    assert ru.arm_if_split(1000.0) == 0
    st = ru.load()
    assert st.phase == "armed" and st.armed == {"then": "restart", "at": 0}
    assert os.path.islink(paths["link"])


def test_arm_if_split_never_waits_for_the_state(raye_apt, paths):
    raye_apt([("raye-system", "1", "raye-base"), ("raye-desktop", "2", "raye-base")])
    st = ready(ru.State())
    ru.mark_restart(st.pending)
    ru.save(st)
    with ru.locked():
        # the install at start-up holds it: dpkg runs the postinst under it
        assert ru.arm_if_split(1000.0) == 0
    assert not ru.load().armed


def test_arm_if_split_leaves_a_set_start_up_alone(raye_apt, paths):
    raye_apt([("raye-system", "1", "raye-base"), ("raye-desktop", "2", "raye-base")])
    st = ready(ru.State())
    ru.mark_restart(st.pending)
    ru.save(st)
    os.symlink("/somewhere/else", paths["link"])
    assert ru.arm_if_split(1000.0) == 0
    assert not ru.load().armed and os.readlink(paths["link"]) == "/somewhere/else"
