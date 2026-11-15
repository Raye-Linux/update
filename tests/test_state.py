# SPDX-License-Identifier: GPL-3.0-or-later
"""The state file: schema version 2 (docs/state.md) and the old keys.

The files in fixtures/state/ are the ones the Start menu (SH-1) and Raye
Settings (ST-25) copy into their own tests: keep them valid."""
import glob
import json
import os

import pytest

from conftest import FIXTURES, ru

V2_FIXTURES = sorted(f for f in glob.glob(os.path.join(FIXTURES, "state", "*.json"))
                     if not os.path.basename(f).startswith("v1-"))


def test_there_is_a_fixture_for_every_phase_a_reader_shows():
    phases = {json.load(open(f))["phase"] for f in V2_FIXTURES}
    assert phases >= {"idle", "checking", "waiting", "downloading", "ready", "armed",
                      "paused", "error"}


@pytest.mark.parametrize("path", V2_FIXTURES, ids=os.path.basename)
def test_fixtures_are_valid(path):
    assert ru.validate(json.load(open(path))) == []


@pytest.mark.parametrize("path", V2_FIXTURES, ids=os.path.basename)
def test_fixtures_are_exactly_what_the_writer_writes(path):
    d = json.load(open(path))
    assert ru.to_json(ru.from_json(d)) == d


def test_default_state_is_valid():
    assert ru.validate(ru.to_json(ru.State())) == []


@pytest.mark.parametrize("change,problem", [
    ({"phase": "sleeping"}, "phase: unknown"),
    ({"version": 3}, "version: not 2"),
    ({"count": -1}, "count: negative"),
    ({"count": True}, "count: wrong type"),
    ({"needsRestart": 1}, "needsRestart: wrong type"),
    ({"waitReason": "tired"}, "waitReason: unknown"),
    ({"activeHours": {"start": 8, "end": 8}}, "activeHours: out of range"),
    ({"activeHours": {"start": 0, "end": 23}}, "activeHours: out of range"),
    ({"armed": {"then": "explode", "at": 0}}, "armed: bad value"),
    ({"error": {"code": "it broke"}}, "error: bad value"),
    ({"pending": [{"name": "a"}]}, "pending: bad entry"),
    ({"pending": [{"name": "a", "from": "", "to": "1", "origin": "", "kind": "toy"}]},
     "pending: unknown kind"),
    ({"firmware": [{"device": "x"}]}, "firmware: bad entry"),
    ({"lastInstall": {"time": 1}}, "lastInstall: bad value"),
])
def test_validate_names_what_is_wrong(change, problem):
    d = ru.to_json(ru.State())
    d.update(change)
    assert problem in ru.validate(d)


def test_validate_finds_missing_keys():
    d = ru.to_json(ru.State())
    del d["armed"], d["phase"]
    assert {"armed: missing", "phase: missing"} <= set(ru.validate(d))


def test_old_keys_follow_the_new_ones():
    st = ru.State(phase="armed", count=2, readyId="r1", lastCheck=5.0, pausedUntil=0,
                  pending=[{"name": "a", "from": "", "to": "1", "origin": "", "kind": "system"},
                           {"name": "b", "from": "", "to": "1", "origin": "", "kind": "system"}])
    d = ru.to_json(st)
    assert (d["state"], d["updates"], d["packages"], d["checked"], d["ready_id"]) == \
        ("ready", 2, ["a", "b"], 5.0, "r1")
    d = ru.to_json(ru.State(phase="waiting", waitReason="metered"))
    assert d["reason"] == "metered"
    d = ru.to_json(ru.State(phase="error", error={"code": "no-space", "detail": "full"}))
    assert (d["state"], d["reason"]) == ("error", "full")


def test_save_load_roundtrip_and_mode(paths):
    st = ru.State(phase="ready", count=1, readyId="1-3", needsRestart=True,
                  pending=[{"name": "a", "from": "1", "to": "2", "origin": "Ubuntu", "kind": "system"}])
    ru.save(st)
    assert oct(os.stat(paths["state"]).st_mode & 0o777) == "0o644"
    assert ru.load() == st
    assert os.listdir(os.path.dirname(paths["state"])) == ["state.json"]   # no temp file left


def test_load_survives_garbage(paths):
    os.makedirs(os.path.dirname(paths["state"]))
    open(paths["state"], "w").write("{not json")
    assert ru.load() == ru.State()
    open(paths["state"], "w").write("[1, 2]")
    assert ru.load() == ru.State()


def test_load_drops_a_broken_v2_file_but_keeps_the_owner_settings(paths):
    d = ru.to_json(ru.State(pausedUntil=99.0, activeHours={"start": 22, "end": 6},
                            autoRestart=True))
    d["phase"] = "nonsense"
    os.makedirs(os.path.dirname(paths["state"]))
    json.dump(d, open(paths["state"], "w"))
    st = ru.load()
    assert st.phase == "idle"
    assert (st.pausedUntil, st.activeHours, st.autoRestart) == (99.0, {"start": 22, "end": 6}, True)


def test_load_ignores_unknown_keys(paths):
    d = ru.to_json(ru.State(count=0))
    d["somethingNew"] = 1
    os.makedirs(os.path.dirname(paths["state"]))
    json.dump(d, open(paths["state"], "w"))
    assert ru.load() == ru.State()


def test_version_1_file_is_read_and_downloads_again():
    st = ru.from_json(json.load(open(os.path.join(FIXTURES, "state", "v1-ready.json"))))
    assert st.version == 2
    assert [p["name"] for p in st.pending] == ["firefox", "libc6"]
    assert st.count == 2 and st.lastCheck == 1793462520.5
    assert st.readyId == "" and st.phase == "idle"      # PackageKit's download is not ours
    assert ru.validate(ru.to_json(st)) == []


def test_version_1_pause_is_kept():
    st = ru.from_json({"state": "paused", "updates": 0, "packages": [], "paused_until": 9e9})
    assert (st.phase, st.pausedUntil) == ("paused", 9e9)


def test_set_error_cuts_the_detail_and_checks_the_code():
    st = ru.State()
    ru.set_error(st, "no-space", "x" * 1000)
    assert st.phase == "error" and st.error["code"] == "no-space" and len(st.error["detail"]) == 300
    ru.set_error(st, "made-up")
    assert st.error["code"] == "unknown"
