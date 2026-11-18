# SPDX-License-Identifier: GPL-3.0-or-later
"""The apt options Raye Update relies on, against the real apt: a small
archive and an apt root of their own under tmp_path, dpkg replaced by
/bin/true. Nothing of the machine running the tests is read or changed."""
import email.utils
import hashlib
import os
import shutil

import pytest

from conftest import ru

pytestmark = pytest.mark.skipif(
    not shutil.which("apt-get") or not os.path.exists("/usr/lib/apt/methods/copy"),
    reason="needs apt")

# name, version, depends, phased percentage
PACKAGES = [
    ("alpha", "2", "", None),
    ("beta", "2", "", None),
    ("gamma", "2", "", 10),          # Ubuntu gives it to 10 % of the machines so far
    ("delta", "2", "gamma (>= 2)", None),
]


def make_archive(repo):
    os.makedirs(repo)
    paras = []
    for name, version, depends, phased in PACKAGES:
        # apt checks the size and the checksum, never what is inside
        data = f"{name} {version}\n".encode() * 64
        filename = f"{name}_{version}_all.deb"
        with open(os.path.join(repo, filename), "wb") as f:
            f.write(data)
        lines = [f"Package: {name}", f"Version: {version}", "Architecture: all",
                 "Maintainer: Raye <raye@example.org>", f"Filename: ./{filename}",
                 f"Size: {len(data)}", f"SHA256: {hashlib.sha256(data).hexdigest()}",
                 "Description: test"]
        if depends:
            lines.append(f"Depends: {depends}")
        if phased is not None:
            lines.append(f"Phased-Update-Percentage: {phased}")
        paras.append("\n".join(lines))
    packages = ("\n\n".join(paras) + "\n").encode()
    with open(os.path.join(repo, "Packages"), "wb") as f:
        f.write(packages)
    with open(os.path.join(repo, "Release"), "w") as f:
        f.write(f"Date: {email.utils.formatdate(usegmt=True)}\nSHA256:\n"
                f" {hashlib.sha256(packages).hexdigest()} {len(packages)} Packages\n")


@pytest.fixture
def real_apt(tmp_path, paths, monkeypatch):
    repo, root = str(tmp_path / "repo"), tmp_path / "root"
    make_archive(repo)
    for d in ("etc/apt/preferences.d", "var/lib/dpkg", "var/lib/apt/lists/partial",
              "var/cache/apt/archives/partial", "log"):
        (root / d).mkdir(parents=True)
    (root / "var/lib/dpkg/status").write_text("".join(
        f"Package: {n}\nStatus: install ok installed\nVersion: 1\nArchitecture: all\n"
        f"Maintainer: Raye <raye@example.org>\nDescription: test\n\n" for n, *_ in PACKAGES))
    # copy:, not file:, so apt "downloads" into the cache like it would over the network
    (root / "etc/apt/sources.list").write_text(f"deb [trusted=yes] copy:{repo} ./\n")
    opts = ["-o", f"Dir={root}", "-o", f"Dir::State::status={root}/var/lib/dpkg/status",
            "-o", f"Dir::Etc={root}/etc/apt", "-o", "Dir::Etc::sourceparts=-",
            "-o", "Dir::Etc::parts=-", "-o", f"Dir::Log={root}/log",
            "-o", "APT::Architecture=amd64", "-o", "Debug::NoLocking=1",
            "-o", "Dir::Bin::dpkg=/bin/true",
            "-o", "APT::Machine-ID=0123456789abcdef0123456789abcdef"]
    monkeypatch.setattr(ru, "APT_OPTS", ru.APT_OPTS + opts)
    monkeypatch.setattr(ru, "ARCHIVES", str(root / "var/cache/apt/archives"))
    real_run = ru.run
    # dpkg itself never runs: it would act on the machine, not on this root
    monkeypatch.setattr(ru, "run", lambda cmd, timeout=3600: (0, "", "")
                        if cmd[0].startswith("dpkg") else real_run(cmd, timeout))
    for name, value in (("battery_level", lambda: (True, None)), ("is_metered", lambda: False),
                        ("is_gaming", lambda: False), ("free_bytes", lambda p: 50 << 30),
                        ("list_firmware", lambda: []), ("obsolete_plan", lambda coming=(): []),
                        ("offline_names", lambda pending: set()),
                        ("dpkg_half_configured", lambda: False)):
        monkeypatch.setattr(ru, name, value)
    rc, out, err = ru.run(["apt-get", "update", *ru.APT_OPTS])
    assert rc == 0, out + err
    return {"repo": repo, "archives": str(root / "var/cache/apt/archives")}


def names(pending):
    return sorted(p["name"] for p in pending)


def test_a_phased_update_waits_and_takes_what_needs_it_along(real_apt):
    pending, broken, err = ru.list_updates()
    assert (names(pending), broken, err) == (["alpha", "beta"], [], "")


def test_a_file_the_mirror_lost_leaves_the_rest_ready(real_apt):
    os.unlink(os.path.join(real_apt["repo"], "beta_2_all.deb"))
    st = ru.download(ru.check(ru.State()))
    assert (st.phase, st.error, names(st.pending)) == ("ready", None, ["alpha"])
    assert os.listdir(real_apt["archives"]).count("alpha_2_all.deb") == 1


def test_the_install_takes_what_is_downloaded_and_leaves_the_rest(real_apt):
    st = ru.download(ru.check(ru.State()))
    assert names(st.pending) == ["alpha", "beta"]
    # a file gone from the cache since the download (or a package that
    # turned up since): without --fix-missing apt refuses the whole install
    os.unlink(os.path.join(real_apt["archives"], "beta_2_all.deb"))
    plain = ru.run(["apt-get", *ru.APT_OPTS, "-y", "--no-download", *ru.UPGRADE])
    assert plain[0] != 0 and "Unable to fetch some archives" in plain[1] + plain[2]
    ok, out = ru.install_packages(lambda pct: None)
    assert ok, out
    assert ru.cached_set(st.pending) == [p for p in st.pending if p["name"] == "alpha"]
