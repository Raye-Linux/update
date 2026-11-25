# SPDX-License-Identifier: GPL-3.0-or-later
"""Raye Insider: the update channel is one apt source that Raye Update owns.

Stable is the archive's own suite (raye.sources) and no file of ours; every
other channel adds one suite of the same archive, same mirrors, same key.
"""
import os

import pytest

import raye_update as ru

SOURCE = """\
Types: deb
URIs: mirror+file:/etc/apt/mirrors/raye.list
Suites: resolute
Components: main
Architectures: amd64
Signed-By: /usr/share/keyrings/raye-archive-keyring.gpg
"""


@pytest.fixture
def insider(tmp_path, monkeypatch):
    src = tmp_path / "raye.sources"
    src.write_text(SOURCE)
    out = tmp_path / "sources.list.d" / "raye-insider.sources"
    out.parent.mkdir()
    monkeypatch.setattr(ru, "ARCHIVE_SOURCE", str(src))
    monkeypatch.setattr(ru, "INSIDER_SOURCE", str(out))
    monkeypatch.delenv("RAYE_INSIDER_URIS", raising=False)
    monkeypatch.delenv("RAYE_INSIDER_KEYRING", raising=False)
    return out


def fields(path):
    return ru.parse_deb822(path.read_text())


def test_the_four_channels_and_their_suites():
    assert ru.CHANNELS == ("stable", "public-beta", "beta", "dev")
    assert ru.CHANNEL_SUITES == {"public-beta": "resolute-public-beta",
                                 "beta": "resolute-beta", "dev": "resolute-dev"}


def test_a_pc_with_no_file_of_ours_is_on_stable(insider):
    assert ru.current_channel() == "stable"


def test_a_channel_adds_its_suite_with_the_archives_mirrors_and_key(insider):
    assert ru.set_channel("beta") == "stable"
    f = fields(insider)
    assert f["suites"] == "resolute-beta"
    assert f["uris"] == "mirror+file:/etc/apt/mirrors/raye.list"
    assert f["signed-by"] == "/usr/share/keyrings/raye-archive-keyring.gpg"
    assert f["components"] == "main"
    assert oct(os.stat(insider).st_mode & 0o777) == "0o644"
    assert ru.current_channel() == "beta"


def test_changing_channel_replaces_the_suite_never_adds_a_second(insider):
    ru.set_channel("public-beta")
    assert ru.set_channel("dev") == "public-beta"
    assert fields(insider)["suites"] == "resolute-dev"
    assert insider.read_text().count("Suites:") == 1


def test_leaving_goes_back_to_stable_by_removing_our_file(insider):
    ru.set_channel("dev")
    assert ru.set_channel("stable") == "dev"
    assert not insider.exists()
    assert ru.current_channel() == "stable"
    # leaving twice is no error
    assert ru.set_channel("stable") == "stable"


def test_an_unknown_channel_is_refused_and_nothing_is_written(insider):
    with pytest.raises(ValueError):
        ru.set_channel("nightly")
    with pytest.raises(ValueError):
        ru.set_channel("beta\nSuites: evil")
    assert not insider.exists()


def test_a_file_someone_edited_by_hand_reads_as_the_suite_it_names(insider):
    insider.write_text(SOURCE.replace("resolute", "resolute-public-beta"))
    assert ru.current_channel() == "public-beta"
    insider.write_text(SOURCE.replace("resolute", "somewhere-else"))
    assert ru.current_channel() == "unknown"


def test_no_channel_without_the_archive_source(insider, tmp_path, monkeypatch):
    monkeypatch.setattr(ru, "ARCHIVE_SOURCE", str(tmp_path / "none.sources"))
    with pytest.raises(ru.InsiderError):
        ru.set_channel("beta")
    assert not insider.exists()


def test_a_test_tree_may_stand_in_for_the_mirrors(insider, tmp_path, monkeypatch):
    key = tmp_path / "test.gpg"
    key.write_bytes(b"\x99")
    monkeypatch.setenv("RAYE_INSIDER_URIS", "file:/var/tmp/insider-tree")
    monkeypatch.setenv("RAYE_INSIDER_KEYRING", str(key))
    ru.set_channel("dev")
    f = fields(insider)
    assert f["uris"] == "file:/var/tmp/insider-tree"
    assert f["signed-by"] == str(key)


@pytest.mark.parametrize("uri", ["http://dl.rayelinux.com/apt/", "ftp://x/", "file:rel",
                                 "https://a b/", "https://x/\nSuites: y"])
def test_a_test_tree_must_be_https_or_a_local_folder(insider, tmp_path, monkeypatch, uri):
    key = tmp_path / "test.gpg"
    key.write_bytes(b"\x99")
    monkeypatch.setenv("RAYE_INSIDER_URIS", uri)
    monkeypatch.setenv("RAYE_INSIDER_KEYRING", str(key))
    with pytest.raises(ru.InsiderError):
        ru.set_channel("dev")
    assert not insider.exists()


def test_channel_status_is_json_for_the_settings_page(insider, capsys):
    ru.set_channel("public-beta")
    assert ru.main(["raye-update", "channel"]) == 0
    import json
    out = json.loads(capsys.readouterr().out)
    assert out["channel"] == "public-beta"
    assert out["suite"] == "resolute-public-beta"
    assert out["channels"] == list(ru.CHANNELS)


def test_setting_a_channel_needs_root(insider, monkeypatch, capsys):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert ru.main(["raye-update", "channel", "set", "beta"]) == 1
    assert not insider.exists()


def test_set_then_checks_for_updates_and_says_what_stays(insider, monkeypatch, capsys):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    started = []
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=3600: (started.append(cmd), (0, "", ""))[1])
    monkeypatch.setattr(ru, "CHECK_ASKED", str(insider.parent / "asked"))
    assert ru.main(["raye-update", "channel", "set", "dev"]) == 0
    assert ["systemctl", "start", "--no-block", "raye-update.service"] in started
    assert ru.main(["raye-update", "channel", "set", "stable"]) == 0
    out = capsys.readouterr().out
    # leaving never downgrades: the newer packages stay until Stable catches up
    assert "until Stable" in out
    assert not insider.exists()


def test_set_with_a_bad_channel_says_how(insider, monkeypatch, capsys):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert ru.main(["raye-update", "channel", "set", "nightly"]) == 2
    assert ru.main(["raye-update", "channel", "set"]) == 2


def test_the_wrapper_reads_the_channel_without_a_password(tmp_path):
    """src/raye-update: `channel` goes straight through, `channel set` through pkexec."""
    import subprocess
    wrapper = os.path.join(os.path.dirname(__file__), "..", "src", "raye-update")
    log = tmp_path / "log"
    for name in ("pkexec", "libexec"):
        f = tmp_path / name
        f.write_text(f'#!/bin/sh\necho "{name} $*" >> {log}\n')
        f.chmod(0o755)
    env = {**os.environ, "RAYE_UPDATE_LIBEXEC": str(tmp_path / "libexec"),
           "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    subprocess.run([wrapper, "channel"], env=env, check=True)
    subprocess.run([wrapper, "channel", "status"], env=env, check=True)
    subprocess.run([wrapper, "channel", "set", "beta"], env=env, check=True)
    lines = log.read_text().splitlines()
    assert lines[:2] == ["libexec channel", "libexec channel status"]
    if os.geteuid() == 0:
        assert lines[2] == "libexec channel set beta"
    else:
        assert lines[2] == f"pkexec {tmp_path / 'libexec'} channel set beta"
