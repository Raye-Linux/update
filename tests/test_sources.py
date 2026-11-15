# SPDX-License-Identifier: GPL-3.0-or-later
"""A repository that cannot be read must not take the others down with it.

Measured on apt 3.0.3 (the version of resolute), and the exit codes are not
what one would guess:

  the name does not resolve        -> apt prints W:, and exits 0
  the server answers, path is 404 -> apt prints E:, and exits 100
  the signature does not check    -> apt prints E:, and exits 100

So the two halves were wrong in opposite directions: the harmless case went
through in silence, and the case that happens when our own archive moves or
is published empty stopped every Ubuntu security update with it.
"""
from conftest import fixture, ru


# --- which repositories apt has, and which ones it could not read -----------

def test_the_repositories_come_from_apt_itself():
    """Not from reading sources.list ourselves: .sources files, options and
    mirror+file: all have their own spelling, and apt already knows them."""
    cmds = []

    def fake_run(cmd, timeout=0):
        cmds.append(cmd)
        return 0, fixture("apt-indextargets.txt"), ""

    ru.run, real = fake_run, ru.run
    try:
        uris = ru.configured_sources()
    finally:
        ru.run = real
    assert cmds == [["apt-get", "indextargets", "--no-release-info"]]
    # deduplicated, and the trailing slash dropped so a failure line matches
    assert uris == ["http://archive.ubuntu.com/ubuntu",
                    "http://security.ubuntu.com/ubuntu",
                    "mirror+file:/etc/apt/mirrors/raye.list"]


def test_no_repository_list_rather_than_a_guess(monkeypatch):
    """apt could not answer: we say we know nothing, we do not invent a list.
    An empty list switches the "nothing was reachable" guard off, which is
    the safe way round -- it can only make us keep going, never claim more."""
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=0: (100, "", "boom"))
    assert ru.configured_sources() == []


def test_a_failed_mirror_is_named_by_the_uri_apt_was_CONFIGURED_with():
    """The trap that would have made all of this quietly useless.

    With mirror+file:, apt prints TWO different URIs for the same failure:
      Err:2 https://dl.rayelinux.com/apt ...   <- the mirror it picked
      W: Failed to fetch mirror+file:/etc/apt/mirrors/raye.list/dists/...
    Only the second is a configured repository. Reading the Err: lines would
    never have matched anything, and the broken list would always be empty.
    """
    configured = ["http://archive.ubuntu.com/ubuntu",
                  "http://security.ubuntu.com/ubuntu",
                  "mirror+file:/etc/apt/mirrors/raye.list"]
    broken = ru.parse_failed_sources(fixture("apt-update-raye-offline.txt"), configured)
    assert broken == [{"uri": "mirror+file:/etc/apt/mirrors/raye.list", "problem": "offline"}]


def test_a_404_is_told_apart_from_an_unreachable_name():
    configured = ["http://archive.ubuntu.com/ubuntu",
                  "http://security.ubuntu.com/ubuntu",
                  "mirror+file:/etc/apt/mirrors/raye.list"]
    broken = ru.parse_failed_sources(fixture("apt-update-raye-missing.txt"), configured)
    assert broken == [{"uri": "mirror+file:/etc/apt/mirrors/raye.list", "problem": "missing"}]


def test_a_signature_that_does_not_check_is_its_own_problem():
    """It is not a network fault and it must not read like one: the server
    answered, and what it served could not be trusted."""
    configured = ["http://archive.ubuntu.com/ubuntu",
                  "mirror+file:/etc/apt/mirrors/raye.list"]
    broken = ru.parse_failed_sources(fixture("apt-update-raye-unsigned.txt"), configured)
    assert broken == [{"uri": "mirror+file:/etc/apt/mirrors/raye.list", "problem": "unsigned"}]


def test_every_repository_down_is_every_repository_named():
    configured = ["http://archive.ubuntu.com/ubuntu",
                  "http://security.ubuntu.com/ubuntu",
                  "mirror+file:/etc/apt/mirrors/raye.list"]
    broken = ru.parse_failed_sources(fixture("apt-update-all-offline.txt"), configured)
    assert [b["uri"] for b in broken] == configured
    assert {b["problem"] for b in broken} == {"offline"}


def test_a_good_run_breaks_nothing():
    configured = ["http://archive.ubuntu.com/ubuntu",
                  "mirror+file:/etc/apt/mirrors/raye.list"]
    assert ru.parse_failed_sources(fixture("apt-update-clean.txt"), configured) == []
    assert ru.parse_failed_sources("", configured) == []


def test_a_repository_whose_name_merely_starts_the_same_is_not_blamed():
    """`/apt` is a prefix of `/apt-testing`, so matching on the prefix alone
    would report our stable archive as down because a testing archive next
    to it is -- and the owner would chase the wrong one.

    This case is on purpose one where the failing repository is NOT in the
    configured list: when it is, picking the longest match hides the missing
    boundary and the test proves nothing (it did, until this was measured).
    """
    configured = ["http://dl.rayelinux.com/apt"]
    text = ("W: Failed to fetch http://dl.rayelinux.com/apt-testing/dists/resolute/InRelease"
            "  Could not resolve 'dl.rayelinux.com'\n")
    assert ru.parse_failed_sources(text, configured) == []
    # and when both are configured, the failure goes to the right one
    both = ["http://dl.rayelinux.com/apt", "http://dl.rayelinux.com/apt-testing"]
    assert ru.parse_failed_sources(text, both) == \
        [{"uri": "http://dl.rayelinux.com/apt-testing", "problem": "offline"}]


def test_a_problem_apt_words_in_a_way_we_do_not_know():
    configured = ["http://archive.ubuntu.com/ubuntu"]
    text = "E: Failed to fetch http://archive.ubuntu.com/ubuntu/dists/x/InRelease  something new\n"
    assert ru.parse_failed_sources(text, configured) == \
        [{"uri": "http://archive.ubuntu.com/ubuntu", "problem": "unknown"}]
    assert all(b["problem"] in ru.SOURCE_PROBLEMS
               for b in ru.parse_failed_sources(text, configured))


def test_apt_is_pinned_to_a_language_we_can_read():
    """Everything above reads apt's own words. apt TRANSLATES them: under a
    French session "Failed to fetch" becomes "Impossible de recuperer", and
    every pattern here -- and classify_error with them -- would quietly stop
    matching, on the machines of the people Raye is for. The pin is in
    APT_ENV; this test is what stops someone removing it by accident."""
    assert ru.APT_ENV.get("LC_ALL", "").startswith("C")


def test_a_failure_is_noticed_even_when_we_cannot_say_whose_it_is():
    """Used when apt could not tell us which repositories it has: then we
    cannot count them, but we can still tell "something failed" from
    "everything was read"."""
    assert ru.any_source_failed(fixture("apt-update-raye-offline.txt"))
    assert ru.any_source_failed(fixture("apt-update-raye-missing.txt"))
    assert ru.any_source_failed(fixture("apt-update-raye-unsigned.txt"))
    assert ru.any_source_failed(fixture("apt-update-all-offline.txt"))
    assert not ru.any_source_failed(fixture("apt-update-clean.txt"))
    assert not ru.any_source_failed("")


# --- what list_updates() does with it ---------------------------------------

def apt_answers(monkeypatch, update, update_rc=0, simulate="", simulate_rc=0,
                targets=None):
    """Stands in for apt: the update run, then indextargets, then the simulation."""
    seen = []

    def fake_run(cmd, timeout=0):
        seen.append(cmd)
        if cmd[1] == "indextargets":
            return 0, targets if targets is not None else fixture("apt-indextargets.txt"), ""
        if "update" in cmd:
            return update_rc, update, ""
        return simulate_rc, simulate, ""

    monkeypatch.setattr(ru, "run", fake_run)
    return seen


SIMULATION = ("Inst libssl3 [3.5.1-1] (3.5.2-1 Ubuntu:26.04/resolute-security [amd64])\n"
              "Inst curl [8.14.1-1] (8.14.2-1 Ubuntu:26.04/resolute-updates [amd64])\n"
              "Conf libssl3 (3.5.2-1 Ubuntu:26.04/resolute-security [amd64])\n")


def test_a_404_on_our_own_archive_does_not_stop_ubuntu(monkeypatch):
    """The work order, in one test. apt exits 100 here; the updates it DID
    read are still perfectly usable, so they must still be installed."""
    apt_answers(monkeypatch, fixture("apt-update-raye-missing.txt"), update_rc=100,
                simulate=SIMULATION)
    pending, broken, err = ru.list_updates()
    assert err == ""
    assert [p["name"] for p in pending] == ["libssl3", "curl"]
    assert broken == [{"uri": "mirror+file:/etc/apt/mirrors/raye.list", "problem": "missing"}]


def test_our_archive_being_offline_does_not_stop_ubuntu_either(monkeypatch):
    apt_answers(monkeypatch, fixture("apt-update-raye-offline.txt"), simulate=SIMULATION)
    pending, broken, err = ru.list_updates()
    assert err == "" and len(pending) == 2
    assert [b["problem"] for b in broken] == ["offline"]


def test_a_machine_that_reached_NOTHING_is_not_up_to_date(monkeypatch):
    """The silent failure this work also had to fix. apt exits 0 and the
    simulation answers "0 upgraded", so the old code set phase=idle and
    stamped lastCheck: a laptop with no network was told it was up to date.
    Reaching nothing is not knowing nothing is wrong."""
    apt_answers(monkeypatch, fixture("apt-update-all-offline.txt"),
                simulate="0 upgraded, 0 newly installed, 0 to remove.\n")
    pending, broken, err = ru.list_updates()
    assert pending is None
    assert len(broken) == 3
    assert ru.classify_error(err) == "no-internet"


def test_the_package_lock_still_waits_instead_of_erroring(monkeypatch):
    seen = apt_answers(monkeypatch, "E: Could not get lock /var/lib/dpkg/lock-frontend",
                       update_rc=100)
    pending, broken, err = ru.list_updates()
    assert pending is None and ru.is_lock_error(err)
    # the question was never reached, so it is not answered "nothing is broken"
    assert broken is None
    # and nothing else was asked: another install runs, we come back later
    assert [c[1] for c in seen] == ["update"]


def test_a_busy_lock_does_not_erase_the_repository_we_knew_was_down(monkeypatch):
    """"not asked" and "nothing is wrong" are different answers, and writing
    the second over the first would make the warning blink in and out."""
    apt_answers(monkeypatch, "E: Could not get lock /var/lib/dpkg/lock-frontend",
                update_rc=100)
    monkeypatch.setattr(ru, "save_run", lambda s: None)
    known = [{"uri": "mirror+file:/etc/apt/mirrors/raye.list", "problem": "offline"}]
    state = ru.check(ru.State(brokenSources=list(known)), now=1000)
    assert state.phase == "waiting" and state.waitReason == "busy"
    assert state.brokenSources == known


def test_apt_failing_for_something_other_than_a_repository_is_still_an_error(monkeypatch):
    """No repository explains this exit code, so it must not be waved through
    just because the simulation happens to answer."""
    apt_answers(monkeypatch, "E: Malformed entry 3 in list file /etc/apt/sources.list",
                update_rc=100, simulate=SIMULATION)
    pending, broken, err = ru.list_updates()
    assert pending is None and broken == []
    assert "Malformed" in err


def test_a_simulation_that_fails_is_an_error_even_with_every_source_fine(monkeypatch):
    apt_answers(monkeypatch, fixture("apt-update-clean.txt"),
                simulate="The following packages have unmet dependencies:", simulate_rc=100)
    pending, broken, err = ru.list_updates()
    assert pending is None and broken == []
    assert ru.classify_error(err) == "broken"


def test_without_a_repository_list_the_updates_we_did_read_are_still_reported(monkeypatch):
    """indextargets could not answer, so we cannot count the repositories.
    We found updates all the same, so they are real: report them."""
    apt_answers(monkeypatch, fixture("apt-update-all-offline.txt"), simulate=SIMULATION,
                targets="")
    pending, broken, err = ru.list_updates()
    assert err == "" and len(pending) == 2 and broken == []


def test_without_a_repository_list_and_nothing_found_we_do_not_claim_freshness(monkeypatch):
    """The hole left by leaning on indextargets alone, and it is the old
    silent failure coming back in through a side door: apt could not say
    which repositories it has, every one of them failed, and the simulation
    answers "0 upgraded". Finding nothing may simply mean nothing was read,
    so we do not call that up to date."""
    apt_answers(monkeypatch, fixture("apt-update-all-offline.txt"),
                simulate="0 upgraded, 0 newly installed, 0 to remove.\n", targets="")
    pending, broken, err = ru.list_updates()
    assert pending is None
    assert ru.classify_error(err) == "no-internet"


def test_without_a_repository_list_a_clean_run_with_nothing_found_is_up_to_date(monkeypatch):
    """And the guard above must not cry wolf: nothing failed, nothing found,
    that machine really is up to date."""
    apt_answers(monkeypatch, fixture("apt-update-clean.txt"),
                simulate="0 upgraded, 0 newly installed, 0 to remove.\n", targets="")
    pending, broken, err = ru.list_updates()
    assert pending == [] and broken == [] and err == ""


# --- and what the state says ------------------------------------------------

def test_check_lists_the_updates_and_names_the_repository_that_is_down(monkeypatch):
    apt_answers(monkeypatch, fixture("apt-update-raye-missing.txt"), update_rc=100,
                simulate=SIMULATION)
    monkeypatch.setattr(ru, "list_firmware", lambda: [])
    monkeypatch.setattr(ru, "save_run", lambda s: None)
    state = ru.check(ru.State(), now=1000)
    assert state.phase == "idle" and state.count == 2
    assert state.lastCheck == 1000          # we did read something, and we say so
    assert state.brokenSources == [{"uri": "mirror+file:/etc/apt/mirrors/raye.list",
                                    "problem": "missing"}]


def test_check_keeps_the_repository_named_when_it_gives_up(monkeypatch):
    """The error message must be able to say WHICH repository, not just
    "something went wrong"."""
    apt_answers(monkeypatch, fixture("apt-update-all-offline.txt"), simulate="")
    monkeypatch.setattr(ru, "save_run", lambda s: None)
    state = ru.check(ru.State(lastCheck=500), now=1000)
    assert state.phase == "error" and state.error["code"] == "no-internet"
    assert state.lastCheck == 500           # never stamped on a check that read nothing
    assert len(state.brokenSources) == 3


def test_a_repository_going_down_is_said_once_and_not_every_six_hours(monkeypatch):
    """The timer checks four times a day: a line each time would bury the one
    that matters. A line when it changes, and one when it comes back."""
    said = []
    monkeypatch.setattr(ru, "log", lambda m: said.append(m))
    monkeypatch.setattr(ru, "list_firmware", lambda: [])
    monkeypatch.setattr(ru, "save_run", lambda s: None)

    apt_answers(monkeypatch, fixture("apt-update-raye-offline.txt"), simulate=SIMULATION)
    state = ru.check(ru.State(), now=1000)
    assert len(said) == 1 and "raye.list" in said[0] and "offline" in said[0]

    ru.check(state, now=2000)                 # still down: nothing new to say
    assert len(said) == 1

    apt_answers(monkeypatch, fixture("apt-update-clean.txt"), simulate=SIMULATION)
    ru.check(state, now=3000)                 # back: worth a line
    assert len(said) == 2 and "again" in said[1]


def test_a_repository_that_comes_back_stops_being_reported(monkeypatch):
    apt_answers(monkeypatch, fixture("apt-update-clean.txt"), simulate=SIMULATION)
    monkeypatch.setattr(ru, "list_firmware", lambda: [])
    monkeypatch.setattr(ru, "save_run", lambda s: None)
    before = ru.State(brokenSources=[{"uri": "mirror+file:/etc/apt/mirrors/raye.list",
                                      "problem": "offline"}])
    assert ru.check(before, now=1000).brokenSources == []


def test_the_broken_list_survives_the_state_file(paths):
    broken = [{"uri": "mirror+file:/etc/apt/mirrors/raye.list", "problem": "missing"}]
    ru.save(ru.State(brokenSources=broken), paths["state"])
    assert ru.load(paths["state"]).brokenSources == broken


def test_a_state_file_written_before_this_change_still_reads(paths):
    """An older raye-update wrote no brokenSources: that is "none known",
    never a reason to throw the whole file away."""
    import json
    import os
    d = ru.to_json(ru.State(count=3, phase="idle"))
    del d["brokenSources"]
    os.makedirs(os.path.dirname(paths["state"]), exist_ok=True)
    with open(paths["state"], "w") as f:
        json.dump(d, f)
    state = ru.load(paths["state"])
    assert state.count == 3 and state.brokenSources == []


def test_a_broken_list_that_makes_no_sense_is_refused():
    for bad in ([{"uri": 7, "problem": "offline"}],
                [{"uri": "x", "problem": "whatever"}],
                [{"uri": "x"}],
                ["mirror+file:/etc/apt/mirrors/raye.list"],
                "offline"):
        d = ru.to_json(ru.State())
        d["brokenSources"] = bad
        assert ru.validate(d), f"accepted {bad!r}"


def test_a_version_2_file_without_the_key_is_still_valid():
    """docs/state.md: a reader treats a missing key as its default. This key
    was added after the first version 2 files were written, so requiring it
    would have made every state file on disk invalid at the upgrade -- and
    from_json throws an invalid file away, losing a set already downloaded."""
    d = ru.to_json(ru.State(count=2, phase="ready", readyId="abc", needsRestart=True))
    del d["brokenSources"]
    assert ru.validate(d) == []


def test_an_unsigned_repository_does_not_read_as_unknown():
    """Whatever the message says, the owner must be told the repository is at
    fault -- not left with "unknown"."""
    assert ru.classify_error("E: The repository 'x resolute InRelease' is not signed.") == "server"
    assert ru.classify_error("W: GPG error: x resolute InRelease: NO_PUBKEY 9F2A") == "server"


# --- every "The repository '...'" message apt 3.0.3 can print ---------------
#
# The four fixtures below are real captures, not hand-written: each header
# says which apt run produced it. They exist because the first version of
# this work was proved by a fixture written by hand, and that fixture used
# the one message apt does NOT print when our archive is unpublished. The
# suite was green and the bug was whole.

# The five templates, taken from apt itself. To re-derive them:
#   strings /usr/lib/x86_64-linux-gnu/libapt-pkg.so.* | grep "The repository '%s"
REPO_MESSAGES = {
    "is not signed.": "unsigned",
    "is no longer signed.": "unsigned",
    "provides only weak security information.": "unsigned",
    "does not have a Release file.": "missing",
    "no longer has a Release file.": "missing",
}


def test_every_repository_message_apt_can_print_is_recognised():
    """Not one of the five may come out as "unknown".

    apt has five ways of saying "I refused this repository", and four of them
    were unmatched: each produced an empty broken list, which made the whole
    check an error and took every Ubuntu security update down with it.
    """
    configured = ["mirror+file:/etc/apt/mirrors/raye.list"]
    for tail, expected in REPO_MESSAGES.items():
        text = (f"E: The repository 'mirror+file:/etc/apt/mirrors/raye.list "
                f"resolute InRelease' {tail}\n")
        got = ru.parse_failed_sources(text, configured)
        assert got == [{"uri": configured[0], "problem": expected}], \
            f"apt says {tail!r} and we answer {got!r}"
        assert ru.any_source_failed(text), f"{tail!r} must count as a failure"


def test_apt_has_not_grown_a_sixth_repository_message():
    """Goes red the day apt learns a new way to refuse a repository.

    The check above is a list written here; this one asks apt. When libapt
    cannot be found the question is simply not asked -- the test above still
    runs, so this file never becomes a green that checked nothing.
    """
    import glob
    import re
    import subprocess
    libs = glob.glob("/usr/lib/*/libapt-pkg.so.*") + glob.glob("/usr/lib/libapt-pkg.so.*")
    found = set()
    for lib in libs:
        out = subprocess.run(["strings", lib], capture_output=True, text=True).stdout
        found |= set(re.findall(r"The repository '%s'\s+(.*)", out))
    if not found:
        return                      # no libapt here: nothing to compare against
    assert found <= set(REPO_MESSAGES), \
        f"apt can also say {sorted(found - set(REPO_MESSAGES))!r}: classify it"


def test_an_unpublished_archive_is_named_and_does_not_stop_ubuntu(monkeypatch):
    """THE path every beta tester walks: dl.rayelinux.com does not resolve and
    the GitHub mirror answers 404, so apt has no Release file at all.

    Measured on 2026-11-06: apt exits 100 and prints neither "Failed to fetch"
    nor "is not signed", so the repository was not named, the check became an
    error with the code "unknown" ("Something went wrong"), and the Ubuntu
    security updates it HAD read were thrown away with it.
    """
    apt_answers(monkeypatch, fixture("apt-update-raye-unpublished.txt"),
                update_rc=100, simulate=SIMULATION)
    pending, broken, err = ru.list_updates()
    assert err == ""
    assert [p["name"] for p in pending] == ["libssl3", "curl"]
    assert broken == [{"uri": "mirror+file:/etc/apt/mirrors/raye.list",
                       "problem": "missing"}]


def test_an_archive_that_STOPS_being_signed_is_a_trust_problem(monkeypatch):
    """It was signed at the last check and is not any more. That is the shape
    of a downgrade attack, so it must read as "unsigned" and never as a
    nameless mystery -- and the updates already read stay installable."""
    apt_answers(monkeypatch, fixture("apt-update-raye-no-longer-signed.txt"),
                update_rc=100, simulate=SIMULATION)
    pending, broken, err = ru.list_updates()
    assert err == "" and len(pending) == 2
    assert broken == [{"uri": "mirror+file:/etc/apt/mirrors/raye.list",
                       "problem": "unsigned"}]


def test_an_archive_that_loses_its_release_file_is_named(monkeypatch):
    apt_answers(monkeypatch, fixture("apt-update-raye-no-longer-release.txt"),
                update_rc=100, simulate=SIMULATION)
    pending, broken, err = ru.list_updates()
    assert err == "" and len(pending) == 2
    assert broken == [{"uri": "mirror+file:/etc/apt/mirrors/raye.list",
                       "problem": "missing"}]


def test_weak_security_information_is_not_trusted(monkeypatch):
    """The signature checks out, but the index offers only MD5. apt refuses
    it for security reasons, so "unsigned" is the honest word: what it served
    could not be trusted."""
    apt_answers(monkeypatch, fixture("apt-update-raye-weak.txt"),
                update_rc=100, simulate=SIMULATION)
    pending, broken, err = ru.list_updates()
    assert err == "" and len(pending) == 2
    assert broken == [{"uri": "mirror+file:/etc/apt/mirrors/raye.list",
                       "problem": "unsigned"}]


def test_a_repository_failure_is_never_classified_unknown():
    """When EVERY repository fails this way, classify_error picks the message
    the owner reads. "Something went wrong" says nothing; the servers not
    answering is what happened."""
    for tail in REPO_MESSAGES:
        text = f"E: The repository 'https://dl.rayelinux.com/apt resolute Release' {tail}\n"
        assert ru.classify_error(text) == "server", f"{tail!r} must not be a mystery"


def test_the_Err_lines_are_still_ignored():
    """The Err:/Ign: lines of the unpublished capture name the mirror apt
    happened to pick (raye-linux.github.io), which is not a configured
    repository. Only the W:/E: lines may be read, or nothing would ever
    match again."""
    text = fixture("apt-update-raye-unpublished.txt")
    assert "Err:5 https://raye-linux.github.io/packages" in text
    broken = ru.parse_failed_sources(text, ["mirror+file:/etc/apt/mirrors/raye.list"])
    assert [b["uri"] for b in broken] == ["mirror+file:/etc/apt/mirrors/raye.list"]


# --- "I could not check" is not "nothing to install" ------------------------

def test_a_check_that_could_not_read_everything_does_not_claim_it_did(monkeypatch):
    """The lie this key exists to stop.

    The Raye archive is unreadable and Ubuntu has nothing: phase is idle and
    count is 0, exactly as on a machine that really is up to date. A reader
    looking at phase and count alone says "You're up to date" to someone
    whose Raye updates were never looked at. One field, written once here,
    rather than four reader repositories each deriving it their own way.
    """
    apt_answers(monkeypatch, fixture("apt-update-raye-unpublished.txt"),
                update_rc=100, simulate="0 upgraded, 0 newly installed, 0 to remove.\n")
    state = ru.check(ru.State(), now=1000.0)
    assert state.phase == "idle" and state.count == 0      # looks like freshness
    assert ru.checked_everything(state) is False           # and is not
    assert ru.to_json(state)["checkedEverything"] is False


def test_a_clean_check_with_nothing_found_really_is_up_to_date(monkeypatch):
    apt_answers(monkeypatch, fixture("apt-update-clean.txt"),
                simulate="0 upgraded, 0 newly installed, 0 to remove.\n")
    state = ru.check(ru.State(), now=1000.0)
    assert state.phase == "idle" and state.count == 0
    assert ru.checked_everything(state) is True
    assert ru.to_json(state)["checkedEverything"] is True


def test_a_check_that_failed_outright_claims_nothing_either(monkeypatch):
    """A check that never completed read nothing, even when no single
    repository can be blamed for it (a broken dependency tree, a simulation
    that failed). An empty broken list must not read as "all clear"."""
    apt_answers(monkeypatch, "E: Unable to correct problems, you have held broken packages.\n",
                update_rc=100)
    state = ru.check(ru.State(), now=1000.0)
    assert state.phase == "error" and state.brokenSources == []
    assert ru.checked_everything(state) is False


def test_the_updates_are_still_offered_while_one_archive_is_unreadable(monkeypatch):
    """checkedEverything says "do not call this freshness"; it must never be
    read as "hide the updates". The set stays whole and installable."""
    apt_answers(monkeypatch, fixture("apt-update-raye-unpublished.txt"),
                update_rc=100, simulate=SIMULATION)
    state = ru.check(ru.State(), now=1000.0)
    assert ru.checked_everything(state) is False
    assert state.count == 2 and state.lastCheck == 1000.0


def test_a_state_file_without_the_key_means_everything_was_checked():
    """Files written before this key existed must keep their meaning: they
    were written by a client that only ever had the all-clear case."""
    d = ru.to_json(ru.State())
    del d["checkedEverything"]
    assert ru.validate(d) == []


def test_a_checkedEverything_that_is_not_a_boolean_is_refused():
    d = ru.to_json(ru.State())
    d["checkedEverything"] = "oui"
    assert "checkedEverything: wrong type" in ru.validate(d)
