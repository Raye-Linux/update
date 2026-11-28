import os
import raye_update as ru


def supply(root, name, kind, **files):
    d = os.path.join(root, name)
    os.makedirs(d)
    for k, v in dict(type=kind, **files).items():
        with open(os.path.join(d, k), "w") as f:
            f.write(v + "\n")


def test_a_desktop_with_a_wireless_mouse_is_on_mains(tmp_path):
    root = str(tmp_path)
    supply(root, "hidpp_battery_0", "Battery", scope="Device", capacity="5")
    assert ru.battery_level(root) == (True, None)
    assert ru.why_wait(*ru.battery_level(root), False, False) == ""


def test_a_laptop_battery_still_counts(tmp_path):
    root = str(tmp_path)
    supply(root, "BAT0", "Battery", scope="System", capacity="10")
    supply(root, "AC", "Mains", online="0")
    supply(root, "hidpp_battery_0", "Battery", scope="Device", capacity="90")
    assert ru.battery_level(root) == (False, 10)


def test_a_battery_without_scope_counts_as_the_pc(tmp_path):
    root = str(tmp_path)
    supply(root, "BAT1", "Battery", capacity="50")
    supply(root, "ADP1", "Mains", online="1")
    assert ru.battery_level(root) == (True, 50)
