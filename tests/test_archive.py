# SPDX-License-Identifier: GPL-3.0-or-later
"""The Raye archive's address and key: is what is installed coherent?

The archive does not exist yet. It needs a signing key, which the owner makes
and keeps, and somewhere to live -- both his decisions, neither of them taken
here. What IS taken here is the one question this client is placed to ask:
does the installed configuration hang together?

Three incoherences end the same way, and the ending is the worst kind: a
quiet "unsigned" line, a beta that never receives a fix, and nothing broken
enough for anyone to look.

  an address with no key        -> apt refuses the archive, for ever
  a key with no address         -> there is nothing to fetch
  a key that is not the archive's -> every index fails verification

Measured on apt 3.0.3 (2026-11-06): all three come out of `apt-get update`
as "The repository '...' is not signed", exit 100, and nothing else.
"""
import json
import os
import re
import shutil
import subprocess

import pytest
from conftest import ru

SOURCE = """\
Types: deb
URIs: mirror+file:/etc/apt/mirrors/raye.list
Suites: resolute
Components: main
Architectures: amd64
Signed-By: /usr/share/keyrings/raye-archive-keyring.gpg
"""
MIRRORS = """\
# Where the Raye packages come from, tried in this order.
https://dl.rayelinux.example/apt/\tpriority:1
https://raye-linux.github.example/packages/\tpriority:2
"""
# Obviously synthetic, and that is the point: the archive's real signing
# key does not exist yet, and a real-looking fingerprint sitting in a test
# is the first step towards someone pinning it for real.
FPR = "0123456789ABCDEF0123456789ABCDEF01234567"


@pytest.fixture
def archive(tmp_path):
    """A coherent installed configuration, under tmp_path. Returns a writer
    so each test breaks exactly one thing."""
    etc = tmp_path / "etc" / "apt"
    (etc / "sources.list.d").mkdir(parents=True)
    (etc / "mirrors").mkdir(parents=True)
    keys = tmp_path / "usr" / "share" / "keyrings"
    keys.mkdir(parents=True)
    key = keys / "raye-archive-keyring.gpg"
    key.write_bytes(b"\x99\x01\x0d" + b"not a real key, long enough to not be empty")
    src = etc / "sources.list.d" / "raye.sources"
    src.write_text(SOURCE.replace("/usr/share/keyrings/raye-archive-keyring.gpg", str(key)))
    mir = etc / "mirrors" / "raye.list"
    mir.write_text(MIRRORS)

    def write(source=None, mirrors=None):
        if source is not None:
            src.write_text(source)
        if mirrors is not None:
            mir.write_text(mirrors)
        return ru.archive_config(str(src), str(mir))
    write.key = key
    write.source_path = str(src)
    write.mirrors_path = str(mir)
    return write


# --- reading what is installed ---------------------------------------------

def test_the_address_and_the_key_are_read_from_what_is_installed(archive):
    cfg = archive()
    assert cfg["uris"] == ["mirror+file:/etc/apt/mirrors/raye.list"]
    assert cfg["keyring"] == str(archive.key)
    assert cfg["mirrors"] == ["https://dl.rayelinux.example/apt/",
                              "https://raye-linux.github.example/packages/"]
    assert ru.archive_problems(cfg) == []


def test_a_deb822_field_is_read_whatever_the_spacing(archive):
    cfg = archive(source="Types:deb\n"
                         "URIs:   mirror+file:/etc/apt/mirrors/raye.list\n"
                         "Suites: resolute\n"
                         f"signed-by:\t{archive.key}\n")
    assert cfg["uris"] == ["mirror+file:/etc/apt/mirrors/raye.list"]
    assert cfg["keyring"] == str(archive.key)


# --- the three incoherences -------------------------------------------------

def test_an_address_with_no_key_is_refused(archive):
    """apt would fetch it and then refuse every index of it, for ever."""
    cfg = archive(source="Types: deb\n"
                         "URIs: mirror+file:/etc/apt/mirrors/raye.list\n"
                         "Suites: resolute\nComponents: main\n")
    assert "no-key" in ru.archive_problems(cfg)


def test_a_key_with_no_address_is_refused(archive):
    cfg = archive(source=f"Types: deb\nSuites: resolute\nSigned-By: {archive.key}\n")
    assert "no-address" in ru.archive_problems(cfg)


def test_a_key_that_is_not_installed_is_refused(archive):
    os.unlink(archive.key)
    assert "key-missing" in ru.archive_problems(archive())


def test_an_empty_key_file_is_refused(archive):
    """It exists, so every path check passes, and it signs nothing."""
    archive.key.write_bytes(b"")
    assert "key-unreadable" in ru.archive_problems(archive())


def test_a_mirror_list_with_no_mirror_in_it_is_refused(archive):
    cfg = archive(mirrors="# nothing yet\n\n# still nothing\n")
    assert "no-address" in ru.archive_problems(cfg)


def test_a_mirror_that_is_not_https_is_refused(archive):
    """The archive carries the packages that become root on the machine. A
    signature makes plain HTTP survivable, not acceptable.

    Every scheme that is not https is refused, not only http: an unknown one
    passing in silence is the shape of every other hole found in this file.
    """
    for bad in ("http://dl.rayelinux.example/apt/", "ftp://dl.rayelinux.example/apt/",
                "https//dl.rayelinux.example/apt/", "dl.rayelinux.example/apt/",
                "file:///var/tmp/apt/"):
        cfg = archive(mirrors=f"{bad}\tpriority:1\n")
        assert "insecure-address" in ru.archive_problems(cfg), bad
    # and the two shapes that are legitimate stay legitimate
    for ok in ("https://dl.rayelinux.com/apt/", "tor+https://dl.rayelinux.com/apt/"):
        cfg = archive(mirrors=f"{ok}\tpriority:1\n")
        assert "insecure-address" not in ru.archive_problems(cfg), ok


def test_no_source_file_at_all_is_refused(archive):
    os.unlink(archive.source_path)
    cfg = ru.archive_config(archive.source_path, archive.mirrors_path)
    assert ru.archive_problems(cfg) == ["no-source"]


# --- the fingerprint -------------------------------------------------------

def test_a_malformed_fingerprint_is_refused():
    """The pin is typed by hand, once, from the owner's key. Every way of
    getting it wrong has to be caught here, because a wrong pin makes
    archive-check fail on a perfectly good keyring and the obvious
    conclusion is that the pin is right and the archive is broken."""
    for bad in ("",
                "DEADBEEF",          # a short key id: collidable, so not a pin
                FPR[:-1],            # 39, one short
                FPR + "0",           # 41, one too many
                FPR[:-1] + "Z",      # not hex
                FPR + "!",           # a stray character from a paste
                FPR.replace("0", "O", 1),   # the letter O for the digit zero
                None, 0, True, ["x"]):
        assert ru.well_formed_fingerprint(bad) is False, f"{bad!r} must be refused"


def test_a_well_formed_fingerprint_is_accepted():
    assert ru.well_formed_fingerprint(FPR) is True
    assert ru.well_formed_fingerprint(FPR.lower()) is True
    assert ru.well_formed_fingerprint(FPR[:4] + " " + FPR[4:]) is True   # spaced, as gpg prints


def test_a_pin_that_is_not_a_fingerprint_is_itself_a_problem(archive):
    assert "bad-fingerprint" in ru.archive_problems(archive(), pinned=("NOPE",))


def test_the_pin_is_compared_to_the_keyring_when_both_can_be_read(archive, monkeypatch):
    monkeypatch.setattr(ru, "keyring_fingerprints", lambda p: [FPR])
    assert ru.archive_problems(archive(), pinned=(FPR,)) == []
    other = "1111222233334444555566667777888899990000"
    assert "wrong-key" in ru.archive_problems(archive(), pinned=(other,))


def test_a_keyring_that_holds_no_key_is_refused(archive, monkeypatch):
    monkeypatch.setattr(ru, "keyring_fingerprints", lambda p: [])
    assert "key-unreadable" in ru.archive_problems(archive(), pinned=(FPR,))


def test_a_keyring_that_cannot_be_ASKED_is_not_a_pass(archive, monkeypatch):
    """gpg is not installed, so the keyring holds an unknown key. That is not
    a problem on a PC -- apt checks the signature itself -- but it must be
    said out loud, never counted as checked."""
    monkeypatch.setattr(ru, "keyring_fingerprints", lambda p: None)
    cfg = archive()
    assert ru.archive_problems(cfg, pinned=(FPR,)) == []
    assert "key-not-read" in ru.archive_notes(cfg, pinned=(FPR,))


# --- the release gate ------------------------------------------------------

def test_an_empty_pin_is_provisional_and_release_mode_refuses_it(archive):
    """ARCHIVE_FINGERPRINTS is empty while the owner's key does not exist.
    That is honest on a beta PC and unacceptable in an image: without a pin,
    a swapped keyring is invisible to everything in this repository."""
    cfg = archive()
    assert ru.archive_problems(cfg, pinned=()) == []
    assert "key-not-pinned" in ru.archive_notes(cfg, pinned=())
    assert "key-not-pinned" in ru.archive_problems(cfg, pinned=(), release=True)


def test_the_two_release_gates_are_independent(archive, monkeypatch):
    """A pinned key does not excuse a stand-in address, and a real address
    does not excuse an unpinned key. Each has to be answered on its own, or
    fixing one would quietly close the gate on the other."""
    monkeypatch.setattr(ru, "keyring_fingerprints", lambda p: [FPR])
    cfg = archive()                                   # .example mirrors
    assert ru.archive_problems(cfg, pinned=(FPR,), release=True) == ["provisional-address"]
    real = archive(mirrors="https://dl.rayelinux.com/apt/\tpriority:1\n")
    assert ru.archive_problems(real, pinned=(), release=True) == ["key-not-pinned"]


def test_a_provisional_address_never_reaches_an_image(archive):
    """Any leftover stand-in address -- .example, .invalid, .localhost,
    localhost, a bare IP -- is a beta that silently updates from nowhere."""
    for host in ("https://dl.rayelinux.example/apt/", "https://dl.raye.invalid/apt/",
                 "https://localhost/apt/", "https://127.0.0.1/apt/",
                 "https://dl.raye.test/apt/", "https://dl.raye.localhost/apt/"):
        cfg = archive(mirrors=f"{host}\tpriority:1\n")
        assert ru.archive_problems(cfg, release=True), f"{host} must not ship"
        assert "provisional-address" in ru.archive_problems(cfg, release=True), host


def test_a_real_address_passes_the_release_gate(archive, monkeypatch):
    monkeypatch.setattr(ru, "keyring_fingerprints", lambda p: [FPR])
    cfg = archive(mirrors="https://dl.rayelinux.com/apt/\tpriority:1\n")
    assert ru.archive_problems(cfg, pinned=(FPR,), release=True) == []


def test_the_pin_shipped_in_this_repository_is_either_empty_or_well_formed():
    """Whatever the owner types into ARCHIVE_FINGERPRINTS, a typo there must
    fail here and not on a tester's PC.

    ⚠️ The pin is empty today, so a plain `for f in ARCHIVE_FINGERPRINTS`
    would be a test that passes by running nothing -- the exact shape this
    project keeps paying for. Every assertion below holds whether the pin is
    empty or not.
    """
    pin = ru.ARCHIVE_FINGERPRINTS
    # a bare string would iterate as characters and pin nothing
    assert isinstance(pin, tuple), f"ARCHIVE_FINGERPRINTS must be a tuple, got {type(pin)}"
    for f in pin:
        assert ru.well_formed_fingerprint(f), f"ARCHIVE_FINGERPRINTS: {f!r}"
    # and the consequence, asserted whether it is empty or filled in
    cfg = {"source": ru.ARCHIVE_SOURCE, "exists": True, "uris": ["x"],
           "keyring": "", "mirrors": ["https://dl.rayelinux.com/apt/"]}
    assert "bad-fingerprint" not in ru.archive_problems(cfg, pinned=pin)
    assert "bad-fingerprint" in ru.archive_problems(cfg, pinned=tuple(pin) + ("oups",))


def test_every_problem_code_is_declared():
    """A code the documentation does not list is a code no reader handles."""
    seen = set()
    for release in (False, True):
        for pinned in ((), ("NOPE",), (FPR,)):
            seen |= set(ru.archive_problems({"source": None, "uris": [], "keyring": "",
                                             "mirrors": [], "exists": False},
                                            pinned=pinned, release=release))
    assert seen <= set(ru.ARCHIVE_PROBLEMS), sorted(seen - set(ru.ARCHIVE_PROBLEMS))
    assert seen, "this test must exercise at least one code"


# --- the command -----------------------------------------------------------

@pytest.fixture
def mode(monkeypatch):
    """RAYE_RELEASE, set deliberately.

    ⚠️ archive-check reads it from the environment, so a test that leaves it
    to whatever the runner happens to export changes its verdict with the
    runner. Caught by running the suite under `raye-tests --release` with
    RAYE_RELEASE=1 exported: two tests here went red, and neither the tool
    nor the tests were wrong -- the tests were simply not saying which mode
    they meant.
    """
    def pick(release):
        if release:
            monkeypatch.setenv("RAYE_RELEASE", "1")
        else:
            monkeypatch.delenv("RAYE_RELEASE", raising=False)
    return pick


def run_check(archive, extra=()):
    return ru.main(["raye-update", "archive-check", "--source", archive.source_path,
                    "--mirrors", archive.mirrors_path, *extra])


def test_archive_check_prints_what_it_found_and_says_no_when_it_is_wrong(archive, capsys, mode):
    mode(False)
    os.unlink(archive.key)
    rc = run_check(archive)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert "key-missing" in out["problems"]
    assert out["keyring"] == str(archive.key)


def test_archive_check_says_yes_on_a_coherent_configuration(archive, capsys, mode):
    mode(False)
    rc = run_check(archive)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["problems"] == []
    assert out["release"] is False
    # and says out loud what it could not check, rather than passing over it
    assert "key-not-pinned" in out["notes"]


def test_archive_check_is_strict_in_release_mode(archive, capsys, mode):
    """Same files, same command, opposite verdict: the stand-in address and
    the unpinned key are fine on a beta PC and must stop an image."""
    mode(True)
    rc = run_check(archive)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["release"] is True
    assert out["problems"] == ["provisional-address", "key-not-pinned"]
    assert out["notes"] == []


def test_archive_check_needs_no_root(archive, monkeypatch, mode):
    """It only reads /etc, so it must work from a session, like status: a
    check that needs a password is a check nobody runs."""
    mode(False)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert run_check(archive) == 0


def test_archive_check_refuses_an_argument_it_does_not_know(archive, mode):
    mode(False)
    assert run_check(archive, ["--trust-me"]) == 2
    assert ru.main(["raye-update", "archive-check", "--source"]) == 2


def test_the_keyring_reader_really_reads_a_keyring(tmp_path):
    """Not a mock: a throwaway key is made, read back, and destroyed with the
    temporary directory. Proves the parsing against real gpg output, or says
    gpg is missing -- it never passes by doing nothing."""
    if not any(os.access(os.path.join(p, "gpg"), os.X_OK)
               for p in os.environ.get("PATH", "").split(os.pathsep) if p):
        assert ru.keyring_fingerprints(str(tmp_path / "nope.gpg")) is None
        return
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    env = dict(os.environ, GNUPGHOME=str(home))
    made = subprocess.run(["gpg", "--batch", "--quiet", "--passphrase", "",
                           "--pinentry-mode", "loopback", "--quick-generate-key",
                           "essai jetable <essai@invalid>", "ed25519", "sign", "never"],
                          env=env, capture_output=True)
    assert made.returncode == 0, made.stderr[-300:]
    listed = subprocess.run(["gpg", "--batch", "--with-colons", "--list-keys"],
                            env=env, capture_output=True, text=True)
    want = [l.split(":")[9] for l in listed.stdout.splitlines() if l.startswith("fpr:")][:1]
    ring = tmp_path / "essai.gpg"
    with open(ring, "wb") as f:
        subprocess.run(["gpg", "--batch", "--export"], env=env, stdout=f, check=True)
    got = ru.keyring_fingerprints(str(ring))
    assert got == want, f"{got!r} != {want!r}"
    assert all(ru.well_formed_fingerprint(f) for f in got)
    assert ru.keyring_fingerprints(str(tmp_path / "pas-la.gpg")) == []


def test_a_gpg_that_cannot_RUN_is_not_an_empty_keyring(tmp_path, monkeypatch):
    """🚨 Two different answers used to be the same value.

    gpg exits 2 both for "that file holds no key" and for "I could not
    start", and keyring_fingerprints turned every non-zero into []. But []
    is an ANSWER about the archive, which archive_notes() acts on, while
    "could not ask" is None, which it never passes. So any gpg that failed
    for a reason of its own was reported as a keyring with no key in it.

    Measured 2026-11-07 inside the ISO build: this file's two keyring tests
    failed with `[] != ['<a real fingerprint>']` and there was nothing else
    to go on -- the reader had already turned gpg's refusal into a verdict
    about a file it had never looked at.

    The condition is easy to reach and needs no broken gpg: GNUPGHOME naming
    a directory that does not exist makes gpg refuse even --show-keys, which
    reads nothing but the file it is handed.
    """
    if not shutil.which("gpg"):
        pytest.skip("gpg is not installed; the distinction cannot be exercised")
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    env = dict(os.environ, GNUPGHOME=str(home))
    made = subprocess.run(["gpg", "--batch", "--quiet", "--passphrase", "",
                           "--pinentry-mode", "loopback", "--quick-generate-key",
                           "essai jetable <essai@invalid>", "ed25519", "sign", "never"],
                          env=env, capture_output=True)
    assert made.returncode == 0, made.stderr[-300:]
    ring = tmp_path / "essai.gpg"
    with open(ring, "wb") as f:
        subprocess.run(["gpg", "--batch", "--export"], env=env, stdout=f, check=True)
    assert ring.stat().st_size > 0, "the bench itself is wrong: nothing was exported"

    # the FILE is a real keyring holding a real key; only gpg is prevented
    # from starting. The honest answer is "I could not ask", never "empty".
    monkeypatch.setenv("GNUPGHOME", str(tmp_path / "ce-dossier-n-existe-pas"))
    assert ru.keyring_fingerprints(str(ring)) is None

    # and with gpg able to run again, the two file-level failures are still
    # answers: an empty file and a missing one both hold no key.
    monkeypatch.setenv("GNUPGHOME", str(home))
    vide = tmp_path / "vide.gpg"
    vide.write_bytes(b"")
    assert ru.keyring_fingerprints(str(vide)) == []
    assert ru.keyring_fingerprints(str(tmp_path / "pas-la-non-plus.gpg")) == []
    assert ru.keyring_fingerprints(str(ring)) != []


# --- the package build must actually run all of this -----------------------

def test_the_package_build_runs_every_test_file_in_this_folder():
    """A test the build does not run is a test the ISO can ignore.

    Found on another repository of this tree on 2026-11-06: debian/rules
    listed test folders one by one and had missed two of them, so 346 tests
    had never once been run by a package build. Nothing said so, because
    every test that DID run was green.

    Here debian/rules says `pytest … tests`, the whole folder, so a new file
    is picked up by existing it. This test fails the day someone narrows
    that down to a list of names.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    rules = os.path.join(here, "..", "debian", "rules")
    with open(rules) as f:
        body = f.read()
    line = [l.strip() for l in body.splitlines()
            if "pytest" in l or ("unittest" in l and "python3" in l)]
    assert line, "debian/rules does not run the tests at all"
    assert len(line) == 1, f"more than one test command, check them all: {line}"
    command = line[0]
    # the argument must be the folder, not a file and not a narrower list
    assert re.search(r"(?:^|\s)tests/?(?:\s|$)", command), \
        f"debian/rules must run the whole tests folder, it runs: {command}"
    assert ".py" not in command, \
        f"debian/rules names test files one by one: {command}"
    # and every file that exists is in fact collected by that folder
    files = sorted(f for f in os.listdir(here) if f.startswith("test_") and f.endswith(".py"))
    assert len(files) >= 2
    collected = subprocess.run(
        ["python3", "-m", "pytest", "-p", "no:cacheprovider", "--collect-only", "-q", "tests"],
        cwd=os.path.join(here, ".."), capture_output=True, text=True)
    for name in files:
        assert f"tests/{name}::" in collected.stdout, f"{name} is collected by nothing"


# --- the terminal wrapper --------------------------------------------------

def test_the_wrapper_does_not_ask_for_a_password_to_READ(tmp_path):
    """src/raye-update sends everything but a couple of commands through
    pkexec. archive-check only reads /etc, and a check that asks for a
    password is a check nobody runs -- so it must go straight through, like
    status. Run for real: the wrapper is shell, and a shell `case` with the
    right word in the wrong branch reads perfectly and does the opposite.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    wrapper = os.path.join(here, "..", "src", "raye-update")
    # a stand-in libexec that reports its arguments, and a pkexec that is a
    # failure if it is ever reached
    fake = tmp_path / "libexec"
    fake.write_text("#!/bin/sh\necho \"direct $*\"\n")
    fake.chmod(0o755)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "pkexec").write_text("#!/bin/sh\necho \"pkexec $*\"\n")
    (bin_ / "pkexec").chmod(0o755)
    # the wrapper skips pkexec when it is already root, and this suite may be
    # run by either -- so `id -u` is answered for it, and the routing is what
    # is under test rather than who happens to be running the tests
    (bin_ / "id").write_text("#!/bin/sh\necho 1000\n")
    (bin_ / "id").chmod(0o755)
    env = dict(os.environ, RAYE_UPDATE_LIBEXEC=str(fake),
               PATH=f"{bin_}:{os.environ.get('PATH', '')}")
    for command, expected in (("status", "direct"), ("archive-check", "direct"),
                              ("pause", "pkexec"), ("arm", "pkexec")):
        p = subprocess.run(["sh", wrapper, command], capture_output=True, text=True, env=env)
        got = p.stdout.split()[0] if p.stdout.split() else p.stderr[:60]
        assert got == expected, f"{command}: went through {got}, expected {expected}"


def test_a_keyring_holding_TWO_keys_gives_both(tmp_path):
    """What a key rotation looks like: the old key and the new one side by
    side in one keyring, so neither the PCs that have updated nor the ones
    that have not are locked out.

    Reading only the first fingerprint would report "wrong-key" against a
    perfectly good keyring on the day the pin moves to the new key -- and
    the obvious conclusion would be that the archive had been tampered with.
    Subkeys are not primary keys and must not be offered as a match.
    """
    if not shutil.which("gpg"):
        pytest.skip("gpg is not installed; the parsing cannot be exercised")
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    env = dict(os.environ, GNUPGHOME=str(home))
    for name in ("ancienne", "nouvelle"):
        made = subprocess.run(["gpg", "--batch", "--quiet", "--passphrase", "",
                               "--pinentry-mode", "loopback", "--quick-generate-key",
                               f"{name} jetable <{name}@invalid>", "ed25519", "sign", "never"],
                              env=env, capture_output=True)
        assert made.returncode == 0, made.stderr[-300:]
    ring = tmp_path / "deux.gpg"
    with open(ring, "wb") as f:
        subprocess.run(["gpg", "--batch", "--export"], env=env, stdout=f, check=True)

    shown = subprocess.run(["gpg", "--batch", "--with-colons", "--show-keys", str(ring)],
                           env=env, capture_output=True, text=True).stdout
    want, primary = [], False
    for line in shown.splitlines():
        if line.startswith(("pub:", "sub:")):
            primary = line.startswith("pub:")
        elif line.startswith("fpr:") and primary:
            want.append(line.split(":")[9])
    assert len(want) == 2, f"the bench itself is wrong: {want}"

    got = ru.keyring_fingerprints(str(ring))
    assert got == want, f"{got!r} != {want!r}"
    # and the pin matches whichever of the two it names
    cfg = {"source": "x", "exists": True, "uris": ["mirror+file:/x"],
           "keyring": str(ring), "mirrors": ["https://dl.rayelinux.com/apt/"]}
    for one in want:
        assert ru.archive_problems(cfg, pinned=(one,)) == [], f"{one} must match"
    assert "wrong-key" in ru.archive_problems(
        cfg, pinned=("1111222233334444555566667777888899990000",))
