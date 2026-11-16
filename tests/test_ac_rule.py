"""Plugging in the charger starts the updates that waited for it."""
import os

RULE = os.path.join(os.path.dirname(__file__), "..", "data", "60-raye-update-ac.rules")


def test_charger_plugged_in_starts_the_waiting_install():
    text = open(RULE).read()
    line = [l for l in text.splitlines() if l and not l.startswith("#")]
    assert len(line) == 1
    rule = line[0]
    assert 'SUBSYSTEM=="power_supply"' in rule and 'ATTR{online}=="1"' in rule
    # the unit that checks the battery itself and does nothing when nothing waits
    assert "start raye-update-live@auto.service" in rule
    assert "--no-block" in rule, "udev must not wait for the install"


def test_the_rule_is_shipped():
    install = open(os.path.join(os.path.dirname(__file__), "..", "debian", "install")).read()
    assert "data/60-raye-update-ac.rules usr/lib/udev/rules.d" in install
