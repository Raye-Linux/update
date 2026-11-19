# SPDX-License-Identifier: GPL-3.0-or-later
"""Kernel security updates go in with the rest: Ubuntu's through its meta
package, the Raye kernel by name (each of its versions is a new package),
and the old ones left to apt's own rule, which keeps the newest two."""
from conftest import ru


def test_ubuntu_kernel_comes_with_the_upgrade():
    # the meta package pulls the new ABI in: --with-new-pkgs is what lets it
    assert ru.upgrade_args([]) == ["upgrade", "--with-new-pkgs"]
    out = ("Inst linux-generic-hwe-26.04 [7.0.0-42.42] (7.0.0-43.43 Ubuntu:26.04/resolute-security [amd64])\n"
           "Inst linux-image-7.0.0-43-generic (7.0.0-43.43 Ubuntu:26.04/resolute-security [amd64])\n")
    kinds = {p["name"]: p["kind"] for p in ru.parse_simulation(out)}
    assert kinds == {"linux-generic-hwe-26.04": "kernel", "linux-image-7.0.0-43-generic": "kernel"}
    assert all(ru.OFFLINE_PACKAGES.match(n) for n in kinds)


def test_newer_raye_kernel_is_added():
    images = ["linux-image-7.0.14-raye"]
    available = ["linux-image-7.0.14-raye", "linux-image-7.0.15-raye", "linux-image-7.0.9-raye",
                 "linux-image-7.0.15-raye-dbg", "linux-headers-7.0.15-raye"]
    assert ru.kernel_followups(images, [], available) == ["linux-image-7.0.15-raye"]
    # its headers too when the old ones are there (DKMS needs them)
    assert ru.kernel_followups(images, ["linux-headers-7.0.14-raye"], available) == \
        ["linux-image-7.0.15-raye", "linux-headers-7.0.15-raye"]
    assert ru.upgrade_args(["linux-image-7.0.15-raye"]) == \
        ["upgrade", "--with-new-pkgs", "linux-image-7.0.15-raye"]


def test_nothing_without_a_raye_kernel_or_a_newer_one():
    assert ru.kernel_followups([], [], ["linux-image-7.0.15-raye"]) == []
    assert ru.kernel_followups(["linux-image-7.0.15-raye"], [], ["linux-image-7.0.15-raye"]) == []
    # numbers, not letters: 7.0.9 is older than 7.0.14
    assert ru.kernel_followups(["linux-image-7.0.14-raye"], [], ["linux-image-7.0.9-raye"]) == []


class Apt:
    def __init__(self, installed, available, installable=True):
        self.installed, self.available, self.calls = installed, available, []
        self.installable = installable

    def run(self, cmd, timeout=0):
        self.calls.append(cmd)
        if cmd[0] == "dpkg-query":
            import fnmatch
            names = [n for n in self.installed if fnmatch.fnmatch(n, cmd[-1])]
            return 0, "".join("%s install ok installed\n" % n for n in names), ""
        if cmd[:2] == ["apt-cache", "pkgnames"]:
            return 0, "".join(n + "\n" for n in self.available if n.startswith(cmd[2])), ""
        if cmd[0] == "apt-get" and "install" in cmd:
            return (0 if self.installable else 100), "", ""
        return 0, "", ""


def test_reads_the_system(monkeypatch):
    apt = Apt(["linux-image-7.0.14-raye", "linux-image-7.0.0-43-generic"],
              ["linux-image-7.0.14-raye", "linux-image-7.0.16-raye"])
    monkeypatch.setattr(ru, "run", apt.run)
    monkeypatch.setattr(ru, "FOLLOW_RAYE_KERNEL", True)
    assert ru.raye_kernel_followups() == ["linux-image-7.0.16-raye"]
    # published but not installable yet: the upgrade goes on without it
    apt.installable = False
    assert ru.raye_kernel_followups() == []


def test_old_raye_kernels_left_to_apt(monkeypatch):
    # named on the command line they would stay "manual" forever; marked
    # automatic, apt's kernel rule keeps the running one and the newest two
    apt = Apt(["linux-image-7.0.14-raye", "linux-image-7.0.16-raye", "linux-headers-7.0.16-raye"], [])
    monkeypatch.setattr(ru, "run", apt.run)
    ru.settle_raye_kernels(["linux-image-7.0.16-raye"])
    marks = [c for c in apt.calls if c[:2] == ["apt-mark", "auto"]]
    assert marks == [["apt-mark", "auto", "linux-image-7.0.14-raye", "linux-image-7.0.16-raye",
                      "linux-headers-7.0.16-raye"]]
    apt.calls.clear()
    ru.settle_raye_kernels([])
    assert apt.calls == []


def test_unattended_removes_old_kernels():
    import pathlib
    conf = pathlib.Path(__file__).resolve().parents[2] / "iso/packages/raye-base/release/updates/52raye-unattended-upgrades"
    if conf.exists():
        assert 'Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";' in conf.read_text()
