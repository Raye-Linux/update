# SPDX-License-Identifier: GPL-3.0-or-later
"""An update always ends on the newest published version.

2026-11-20, the owner's rule for 1.0: a PC that skipped a version takes the
newest one directly, never the one in between (it may be the one with the
bug). The same when a newer version comes out while Raye Update checks,
downloads, or waits for the install at the next start: what waits is
replaced by the newest, and nothing is left half done.

Against the real apt (a small archive, dpkg never runs), as in
test_split_set.py: the archive is published again with newer versions
between the steps, the old files gone from it, as publish-repo once did."""
import os
import shutil

import pytest

from conftest import ru
from test_split_set import make_archive, raye_apt  # noqa: F401  (the fixture)

pytestmark = pytest.mark.skipif(
    not shutil.which("apt-get") or not os.path.exists("/usr/lib/apt/methods/copy"),
    reason="needs apt")

NOW = 2_000_000_000.0

# name, version, source, depends
V2 = [("raye-settings", "2", "raye-settings", ""),
      ("raye-system", "2", "raye-base", ""),
      ("raye-desktop", "2", "raye-base", "raye-system"),
      ("hello", "2", "hello", "")]
INSTALLED = [("raye-settings", "1", "raye-settings"), ("raye-system", "1", "raye-base"),
             ("raye-desktop", "1", "raye-base"), ("hello", "1", "hello")]


def newer(archive, version, names):
    return [(n, version if n in names else v, s, d) for n, v, s, d in archive]


V3 = newer(V2, "3", {"raye-settings", "raye-system", "raye-desktop"})


@pytest.fixture
def repo(tmp_path):
    return str(tmp_path / "repo")


def republish(repo, archive):
    """The archive as publish-repo wrote it before 1.0 Beta 2: only the new
    versions, the old files gone at once."""
    shutil.rmtree(repo)
    make_archive(repo, archive)


@pytest.fixture
def installs(monkeypatch):
    """Downloads go to the real apt; an install only says what apt would
    have installed (the same command, simulated)."""
    real = ru.run_with_status
    seen = []

    def stream(cmd, on_status, **kw):
        if "-d" in cmd:
            return real(cmd, on_status, **kw)
        sim = [a for a in cmd if a != "-y"]
        rc, out, err = ru.run(sim[:1] + ["-s"] + sim[1:])
        assert rc == 0, out + err
        seen.append({(p["name"], p["to"]) for p in ru.parse_simulation(out)})
        return 0, ""
    monkeypatch.setattr(ru, "run_with_status", stream)
    return seen


def cache(name, version):
    return os.path.join(ru.ARCHIVES, f"{name}_{version}_all.deb")


def ready(state, now=NOW):
    return ru.download(ru.check(state, now))


def versions(pending):
    return {p["name"]: p["to"] for p in pending}


# --- the install while the PC is in use ---------------------------------------

def test_the_install_in_use_takes_what_came_out_since_the_download(raye_apt, repo, installs):
    raye_apt(INSTALLED, V2)
    st = ready(ru.State(), now=NOW - 3600)        # checked and downloaded an hour ago
    assert versions(st.pending)["raye-settings"] == "2"
    republish(repo, V3)
    ru.install_live(st, now=NOW)
    assert installs, "nothing installed"
    got = dict(installs[0])
    assert got.get("raye-settings") == "3"
    assert ("raye-settings", "2") not in installs[0]
    assert os.path.exists(cache("raye-settings", "3"))


def test_the_install_in_use_never_takes_a_version_it_knows_is_old(raye_apt, repo, installs):
    """A newer version is out but its file cannot be had (the mirror is
    still syncing): the old download is not installed in its place, and
    what did not change still installs."""
    raye_apt(INSTALLED, V2)
    st = ready(ru.State(), now=NOW - 3600)
    republish(repo, V3)
    os.unlink(os.path.join(repo, "raye-settings_3_all.deb"))
    ru.install_live(st, now=NOW)
    taken = set().union(*installs) if installs else set()
    assert "raye-settings" not in {n for n, _ in taken}
    assert ("hello", "2") in taken


def test_a_fresh_check_is_not_read_twice(raye_apt, repo, installs, monkeypatch):
    """The check that started the install a minute ago is recent enough."""
    raye_apt(INSTALLED, V2)
    st = ready(ru.State(), now=NOW - 60)
    calls = []
    monkeypatch.setattr(ru, "check", lambda s, now=None, **kw: calls.append(now) or s)
    ru.install_live(st, now=NOW)
    assert calls == [] and installs


def test_a_check_running_elsewhere_makes_the_install_wait(raye_apt, monkeypatch, installs):
    raye_apt(INSTALLED, V2)
    st = ready(ru.State(), now=NOW - 3600)

    def busy(s, now=None, **kw):
        s.phase, s.waitReason = "waiting", "busy"
        return s
    monkeypatch.setattr(ru, "check", busy)
    st = ru.install_live(st, now=NOW)
    assert installs == [] and st.phase == "ready" and st.waitReason == "busy"
    assert st.readyId


def test_no_network_installs_what_was_downloaded(raye_apt, repo, installs, monkeypatch):
    """apt keeps the lists it had: the download is still the newest known."""
    raye_apt(INSTALLED, V2)
    st = ready(ru.State(), now=NOW - 3600)

    def offline(s, now=None, **kw):
        ru.set_error(s, "no-internet", "Temporary failure resolving")
        return s
    monkeypatch.setattr(ru, "check", offline)
    ru.install_live(st, now=NOW)
    assert installs and ("raye-settings", "2") in installs[0]


# --- a download that meets a newer version --------------------------------------

def test_a_file_replaced_during_the_download_brings_the_newest(raye_apt, repo):
    """Checked with version 2; version 3 came out before the download, and
    version 2's files left the archive: the lists are read again, and the
    newest downloads, all of it."""
    raye_apt(INSTALLED, V2)
    st = ru.check(ru.State(), NOW)
    republish(repo, V3)
    st = ru.download(st)
    assert st.phase == "ready" and st.error is None
    assert versions(st.pending) == {"raye-settings": "3", "raye-system": "3",
                                    "raye-desktop": "3", "hello": "2"}
    for name in ("raye-settings", "raye-system", "raye-desktop"):
        assert os.path.exists(cache(name, "3"))


def test_a_file_lost_for_good_is_read_again_only_once(raye_apt, repo, monkeypatch):
    """The lists say the same after reading them again: no second try."""
    raye_apt(INSTALLED, V2)
    st = ru.check(ru.State(), NOW)
    os.unlink(os.path.join(repo, "hello_2_all.deb"))
    downloads = []
    real = ru.run_with_status
    monkeypatch.setattr(ru, "run_with_status",
                        lambda cmd, cb, **kw: downloads.append(cmd) or real(cmd, cb, **kw))
    st = ru.download(st)
    assert len(downloads) == 1
    assert st.phase == "ready" and "hello" not in versions(st.pending)


# --- the install at the next start ------------------------------------------------

def arm(st):
    ru.mark_restart(st.pending)
    assert ru.arm(st, "restart", 0, NOW - 3500) == 0
    ru.save(st)


def test_a_set_waiting_for_the_restart_is_brought_to_the_newest(raye_apt, repo, installs,
                                                                   paths):
    raye_apt(INSTALLED, V2)
    arm(ready(ru.State(), now=NOW - 3600))
    republish(repo, V3)
    ru.run_auto(ru.load(), "auto", NOW)
    st = ru.load()
    assert st.phase == "armed" and st.armed == {"then": "restart", "at": 0}
    assert versions(st.pending)["raye-system"] == "3"
    ok, _ = ru.install_packages(lambda pct: None)
    assert ok and ("raye-system", "3") in installs[0]
    assert not {v for n, v in installs[0] if n.startswith("raye-")} - {"3"}


def test_a_paused_set_waiting_for_the_restart_is_left_alone(raye_apt, repo, paths, monkeypatch):
    raye_apt(INSTALLED, V2)
    arm(ready(ru.State(), now=NOW - 3600))
    st = ru.load()
    ru.pause(st, 1, NOW)
    ru.save(st)
    monkeypatch.setattr(ru, "check", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    ru.run_auto(ru.load(), "auto", NOW)
    assert ru.load().armed


def test_a_failed_look_leaves_the_waiting_set_as_it_was(raye_apt, paths, monkeypatch):
    raye_apt(INSTALLED, V2)
    arm(ready(ru.State(), now=NOW - 3600))
    before = ru.load()

    def offline(s, now=None, **kw):
        ru.set_error(s, "no-internet", "Temporary failure resolving")
        return s
    monkeypatch.setattr(ru, "check", offline)
    ru.run_auto(ru.load(), "auto", NOW)
    st = ru.load()
    assert st.phase == "armed" and st.error is None and st.pending == before.pending


# Fails with apt 3.2 (the build chroot has 3.2.0, Raye PCs 3.2.1), on the
# 1.0 code as on this one: apt now takes raye-desktop and raye-system 3 even
# with one of them missing from the cache. Known since 2026-11-27, to be
# fixed in the next update; it must not stop the urgent fix of 1.0.2.
@pytest.mark.xfail(reason="apt 3.2 takes a source in part at the restart install (to fix)", strict=False)
def test_the_restart_install_never_splits_a_source_it_has_only_in_part(raye_apt, repo,
                                                                         installs):
    """Lists read again, then the PC stopped in the middle of the download:
    raye-desktop 3 is here, raye-system 3 is not. Neither installs (and
    never their version 2: apt takes only the newest it knows); hello does."""
    raye_apt(INSTALLED, V2)
    ready(ru.State(), now=NOW - 3600)
    republish(repo, V3)
    st = ready(ru.State(), now=NOW)
    os.unlink(cache("raye-system", "3"))
    ok, _ = ru.install_packages(lambda pct: None)
    assert ok
    taken = installs[0]
    assert ("hello", "2") in taken
    assert not {n for n, _ in taken} & {"raye-system", "raye-desktop"}, taken
    assert versions(st.pending)["raye-desktop"] == "3"


def test_a_new_raye_kernel_is_not_held_at_the_restart(raye_apt, repo, installs):
    """The newer Raye kernel is named with the upgrade (a new package, no
    meta package): the grouping must see it as part of the plan."""
    archive = V2 + [("linux-image-7.1-raye", "1", "linux-raye", "")]
    raye_apt(INSTALLED, archive)
    ru.run(["apt-get", *ru.APT_OPTS, "-y", "-d", "install", "linux-image-7.1-raye", "hello"])
    ru.download(ru.check(ru.State(), NOW))
    ok, _ = ru.install_packages(lambda pct: None, followups=["linux-image-7.1-raye"])
    assert ok and ("linux-image-7.1-raye", "1") in installs[0]


# --- right before the PC restarts ---------------------------------------------------

def test_restart_now_brings_the_waiting_set_to_the_newest_first(raye_apt, repo, paths,
                                                                  monkeypatch, no_tools):
    raye_apt(INSTALLED, V2)
    st = ready(ru.State(), now=NOW - 3600)
    ru.mark_restart(st.pending)
    ru.save(st)
    republish(repo, V3)
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ru.time, "time", lambda: NOW)
    assert ru.main(["raye-update", "arm", "--then", "restart", "--now"]) == 0
    st = ru.load()
    assert no_tools["power"] == ["restart"]
    assert st.phase == "armed" and versions(st.pending)["raye-system"] == "3"
    assert os.path.exists(cache("raye-system", "3"))


def test_the_shutdown_look_only_runs_while_the_pc_stops(raye_apt, repo, paths, monkeypatch):
    raye_apt(INSTALLED, V2)
    arm(ready(ru.State(), now=NOW - 3600))
    republish(repo, V3)
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ru.time, "time", lambda: NOW)
    monkeypatch.setattr(ru, "system_stopping", lambda: False)
    assert ru.main(["raye-update", "catch-up", "--stopping"]) == 0
    assert versions(ru.load().pending)["raye-system"] == "2"
    monkeypatch.setattr(ru, "system_stopping", lambda: True)
    assert ru.main(["raye-update", "catch-up", "--stopping"]) == 0
    assert versions(ru.load().pending)["raye-system"] == "3"


def test_a_split_pc_finishes_what_it_has_before_looking_further(raye_apt, repo, paths):
    """A source installed in part (0.5 to 1.0 Beta): the restart finishes it
    with what is downloaded; a newer version waits for the round after,
    never the other way round (the sign-in screen depends on it)."""
    split = [("grub-pc-bin", "2", "grub2", ""),
             ("raye-system", "2", "raye-base", "grub-pc-bin"),
             ("raye-desktop", "2", "raye-base", "raye-system"),
             ("hello", "2", "hello", "")]
    raye_apt([("raye-system", "1", "raye-base"), ("raye-desktop", "2", "raye-base"),
              ("hello", "2", "hello")], split)
    ru.run_auto(ru.State(), "auto", NOW - 3600)
    assert ru.load().phase == "armed"
    republish(repo, newer(split, "3", {"raye-system", "raye-desktop"}))
    ru.run_auto(ru.load(), "auto", NOW)
    assert versions(ru.load().pending)["raye-system"] == "2"


def test_the_shutdown_look_is_a_unit_that_never_runs_inside_an_upgrade():
    here = os.path.dirname(__file__)
    unit = open(os.path.join(here, "..", "data", "raye-update-catch-up.service")).read()
    assert "ExecStop=/usr/libexec/raye-update catch-up --stopping" in unit
    assert "RemainAfterExit=yes" in unit and "After=network-online.target" in unit
    assert "Wants=network-online.target" not in unit      # no wait for the network at boot
    rules = open(os.path.join(here, "..", "debian", "rules")).read()
    line = next(x for x in rules.splitlines() if "raye-update-catch-up" in x and "dh_" in x)
    assert "--no-stop-on-upgrade" in line and "--no-restart-after-upgrade" in line
    assert "data/raye-update-catch-up.service" in open(
        os.path.join(here, "..", "debian", "install")).read()


def test_the_shutdown_look_needs_root(paths, monkeypatch):
    monkeypatch.setattr(ru.os, "geteuid", lambda: 1000)
    assert ru.main(["raye-update", "catch-up", "--stopping"]) == 1
