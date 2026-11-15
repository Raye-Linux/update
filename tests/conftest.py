# SPDX-License-Identifier: GPL-3.0-or-later
import os
import sys

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "src"))
import raye_update as ru  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """State file, /system-update and its target, game flag, all under tmp_path."""
    p = {"state": str(tmp_path / "lib" / "state.json"),
         "link": str(tmp_path / "system-update"),
         "target": str(tmp_path / "lib" / "offline"),
         "game": str(tmp_path / "game-active")}
    monkeypatch.setattr(ru, "STATE", p["state"])
    monkeypatch.setattr(ru, "OFFLINE_LINK", p["link"])
    monkeypatch.setattr(ru, "OFFLINE_TARGET", p["target"])
    monkeypatch.setattr(ru, "GAME_FLAG", p["game"])
    monkeypatch.setattr(ru, "FIRMWARE_CACHE", str(tmp_path / "firmware"))
    # never the machine's own apt cache
    monkeypatch.setattr(ru, "ARCHIVES", str(tmp_path / "archives"))
    monkeypatch.setattr(ru, "PINS_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(ru, "LIVE_LOCK", str(tmp_path / "run" / "live.lock"))
    monkeypatch.setattr(ru, "SNAPPER_CONFIG", str(tmp_path / "no-snapper"))
    monkeypatch.setattr(ru, "RESTORE_POINT", str(tmp_path / "lib" / "restore-point"))
    p["boot"] = str(tmp_path / "boot")
    monkeypatch.setattr(ru, "BOOT_DIR", p["boot"])
    return p


@pytest.fixture
def no_tools(monkeypatch):
    """Nothing outside runs: records plymouth and power calls instead."""
    calls = {"plymouth": [], "power": [], "run": []}
    monkeypatch.setattr(ru, "plymouth", lambda *a: calls["plymouth"].append(a))
    monkeypatch.setattr(ru, "power", lambda then: calls["power"].append(then))
    return calls
