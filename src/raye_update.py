#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Raye Update: system updates in the background, installed on restart.

    raye-update status                 print the current state
    raye-update auto                   check, and download if now is a good moment
    raye-update check                  only look for updates
    raye-update pause N                no checking or downloading for N weeks (1 to 5)
    raye-update resume                 end a pause now
    raye-update arm --then restart|shutdown [--at TIME] [--now]
                                       install the downloaded set at the next start-up
    raye-update disarm                 undo arm
    raye-update active-hours START END hours (0-23) when the PC never restarts on its own
    raye-update auto-restart on|off    restart on its own outside active hours
    raye-update restart-due            what raye-update-restart.timer runs
    raye-update install-offline        what raye-update-install.service runs at start-up
    raye-update install-live auto|now  what raye-update-live@.service runs
    raye-update live-ended             what it runs once stopped, killed or not
    raye-update progress               taskbar progress while downloading (user side)
    raye-update archive-check          is Raye's own archive wired up coherently?
                                       (no root; RAYE_RELEASE makes it strict)

Updates are downloaded and prepared, never armed: nothing installs until
someone picks "Update and restart" (Start, the Ctrl+Alt+Del screen,
notification or Raye Settings), which runs `arm`. A crash or a reboot from the power
button therefore never starts an install. The install runs at the next
start-up, before anyone signs in, with Plymouth showing the progress; apt
takes a snapshot right before it.

A pause stops this tool only. Security fixes keep coming through
unattended-upgrades, the way Windows keeps its malware definitions
coming while updates are paused.

The state lives in /var/lib/raye-update/state.json, readable by everyone.
docs/state.md describes every key.
"""

import contextlib
import hashlib
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field, fields

STATE = "/var/lib/raye-update/state.json"
OFFLINE_LINK = "/system-update"
OFFLINE_TARGET = "/var/lib/raye-update/offline"
GAME_FLAG = "/run/raye/game-active"
ARCHIVES = "/var/cache/apt/archives"
FIRMWARE_CACHE = "/var/cache/raye-update/firmware"
RAYE_TPM = "/usr/libexec/raye-tpm"   # not in PATH
SNAPPER_CONFIG = "/etc/snapper/configs/root"
# number of the "before" restore point of the install under way
RESTORE_POINT = "/var/lib/raye-update/restore-point"
SNAPPER = ["snapper", "--no-dbus", "-c", "root"]
# what changes PCR 7 when it installs: the signed boot loaders, and
# secureboot-db, whose postinst writes the dbx into the firmware right away
# (raye-tpm-watch.path is not running in system-update.target). Ubuntu's
# kernels are checked with the same key every time, so they leave it alone.
BOOT_CHAIN = re.compile(r"^(shim-signed|grub-efi-(amd64|arm64)-signed|secureboot-db)$")
# Packages that need a real restart after the install at start-up, not
# just going on to the desktop: a new kernel or its modules, the boot
# loaders, and drivers whose kernel module is already loaded (the new
# libraries would not match it).
RESTART_PACKAGES = re.compile(r"^(linux-(image|modules|signed)|raye-kernel|shim-signed|"
                              r"grub-efi-(amd64|arm64)|nvidia-|libnvidia-|.*-dkms$)")
# What never installs while the PC is in use, only at the next start-up:
# what runs now would keep the old copy, or load a mix of old and new.
# The kernel, its modules and the boot loaders; drivers whose module is
# loaded and microcode; libc (old processes load new NSS modules), pid 1
# and the bus a session cannot outlive; and the desktop itself: Qt, KDE
# Frameworks, Plasma and KWin load plugins long after they started, and
# the compositor keeps the graphics driver it started with. Not on it: a GRUB
# theme (read at the next start only), Raye's own QML modules (loaded by
# apps, never by the shell) and the admin prompt (a user service, started
# again after its update, see USER_UNITS).
OFFLINE_PACKAGES = re.compile(
    r"^(linux-(image|modules|signed|generic|lowlatency|virtual|firmware)|shim-|grub-(?!theme-)|grub2-|"
    r"nvidia-|libnvidia-|.*-dkms$|intel-microcode$|amd64-microcode$|firmware-|"
    r"libc6(-i386)?$|libc-bin$|systemd$|systemd-sysv$|libsystemd0$|libsystemd-shared$|"
    r"udev$|libudev1$|dbus|libdbus-1|"
    r"libqt6|qt6-|qml6-module-(?!org-raye-)|libkf6|kf6-|kio6?$|kded6|kglobalacceld|kactivitymanagerd|"
    r"plasma-|libplasma|kwin|libkwin|kscreen|libkscreen|kscreenlocker|libkdecorations|"
    r"layer-shell-qt|powerdevil|polkit-kde|xdg-desktop-portal-kde|kde-style-|breeze$|sddm|"
    r"mesa-|libgl1-mesa|libegl-mesa|libglx-mesa|libgbm1|libgallium|libdrm|xwayland$|"
    r"libtaskmanager|libnotificationmanager|libkworkspace|libklookandfeel|libkmpris|"
    r"libbatterycontrol|libklipper|libkpipewire|liblayershellqt|libkwayland|libpowerdevil|"
    r"libkglobalacceld|libksysguard|libksgrd|libprocess(core|ui)|milou$|libinput10$|"
    r"raye-theme$|raye-shell$|raye-style$|xdg-desktop-portal-raye$)")
# The same, by source package: a library Ubuntu splits out of Plasma or
# KWin next year has a name nobody listed above, and the session loads it
# all the same.
OFFLINE_SOURCES = re.compile(
    r"^(glibc|systemd|dbus|mesa|libdrm|libinput|qt6-.*|kf6-.*|plasma-.*|libplasma|kwin|"
    r"kpipewire|layer-shell-qt|kwayland|powerdevil|libksysguard|milou|kglobalacceld|"
    r"kdecoration|kscreenlocker|kscreen|libkscreen|kactivitymanagerd|breeze|polkit-kde-agent-1|"
    r"xdg-desktop-portal-kde)$")
# the pins of each install in use, and of each simulation of one, go to a
# file of their own in here; apt reads it as its preferences
PINS_DIR = "/run/raye-update"
# "Check for updates" was pressed: the check that runs next is a person's
CHECK_ASKED = "/run/raye-update/check-asked"
# how long a person's check waits for an apt lock another install holds
ASKED_LOCK_WAIT = 600
ASKED_LOCK_NAP = 10
LIVE_LOCK = "/run/raye-update/live.lock"
BOOT_DIR = "/boot"
MAX_FIRMWARE_BYTES = 256 << 20
SETTINGS_APP = "application://org.raye.settings.desktop"

MIN_BATTERY = 40
WEEK = 7 * 24 * 3600
MAX_PAUSE_WEEKS = 5
MAX_ACTIVE_SPAN = 18          # hours, as on Windows
MAX_SCHEDULE = 7 * 24 * 3600  # a picked restart time is at most a week ahead
MAX_ATTEMPTS = 3              # boots that may try to finish one install
SPARE_BYTES = 1 << 30         # free space kept on top of what apt needs

PHASES = ("idle", "checking", "waiting", "downloading", "ready", "armed",
          "installing", "paused", "error")
WAIT_REASONS = ("", "game", "metered", "battery", "busy")
NATIVE_ARCH = {"x86_64": "amd64", "aarch64": "arm64"}.get(os.uname().machine, os.uname().machine)
# where one update is while updates install in use
PACKAGE_STATES = ("queued", "installing", "installed", "failed")
KINDS = ("kernel", "driver", "security", "app", "system")
ERROR_CODES = ("no-internet", "server", "no-space", "broken", "install-failed",
               "firmware-failed", "unknown")

# why one repository could not be read, while the others still could
SOURCE_PROBLEMS = ("offline", "missing", "unsigned", "invalid", "unknown")

APT_ENV = {"DEBIAN_FRONTEND": "noninteractive", "APT_LISTCHANGES_FRONTEND": "none",
           "NEEDRESTART_SUSPEND": "1", "LC_ALL": "C.UTF-8"}
# Ubuntu hands a phased update to a share of the machines, more each day,
# and draws each source package on its own: a Plasma point release would
# land a few packages at a time over a week. Raye takes it once Ubuntu gives
# it to everyone, all of it at once.
PHASING = ["-o", "APT::Get::Never-Include-Phased-Updates=true"]
APT_OPTS = ["-q", "-o", "DPkg::Lock::Timeout=120",
            "-o", "Dpkg::Options::=--force-confdef",
            "-o", "Dpkg::Options::=--force-confold", *PHASING]
# installs only take what is downloaded: a package that turned up since, or
# whose file is missing, waits for the next round instead of failing them
FROM_CACHE = ["--no-download", "--fix-missing"]
# never removes a package: new dependencies (a new kernel ABI) come in,
# anything that would need a removal waits for a person to decide
UPGRADE = ["upgrade", "--with-new-pkgs"]
# Each Raye kernel version is a package of its own (linux-image-7.0.15-raye)
# that no meta package points at, so a plain upgrade never sees it: on a PC
# that has one, the newest is named with the upgrade. Off in the tests.
FOLLOW_RAYE_KERNEL = True
_RAYE_IMAGE = re.compile(r"^linux-image-(\d+(?:\.\d+)*)(?:-[\w.]+)?-raye$")
# The one exception: packages an earlier Raye installed itself and a later
# one replaced. They go at the start-up install, or at the next check when
# that install had nothing to put in their place, once what replaces them
# is in, and only when apt would remove nothing else. Kept short and checked
# by hand: each line is a package Raye put there, never one a person chose.
OBSOLETE = (
    # 0.3 ran Windows programs on WineHQ's build; raye-wine took over in 0.5
    {"needs": "raye-wine", "add": ("raye-wine-compat",),
     "remove": ("winehq-stable", "wine-stable", "wine-stable-amd64", "wine-stable-i386")},
    # the 1.0 Beta image brought Synaptic; the Store does its work since
    # Beta 2. "image_only": a copy a person installed (apt's history names
    # it) stays.
    {"needs": "raye-desktop", "add": (), "remove": ("synaptic",), "image_only": True},
    # Raye Synth (Beta 2) brought fluidsynth, which recommends Qsynth: an
    # upgrade put it in too. Raye Synth needs no window, and Qsynth's own
    # starts on JACK, which Raye does not run. "raye_brought": it goes only
    # when apt's history shows Raye Update put it there, never when a person
    # installed it or something that brought it.
    {"needs": "raye-desktop", "add": (), "remove": ("qsynth",), "raye_brought": True},
)
# apt's own record of what was installed and by which command; an image
# ships it empty, so a package named in it was put there after the install
APT_HISTORY = "/var/log/apt"
_HISTORY_LINE = re.compile(r"^(Install|Reinstall): (.*)$")


@dataclass
class State:
    version: int = 2
    phase: str = "idle"
    waitReason: str = ""
    pending: list = field(default_factory=list)
    count: int = 0
    downloadedBytes: int = 0
    totalBytes: int = 0
    needsRestart: bool = False
    readyId: str = ""
    pausedUntil: float = 0
    activeHours: dict = field(default_factory=lambda: {"start": 8, "end": 17})
    autoRestart: bool = False
    armed: object = None          # {"then": "restart"|"shutdown", "at": time}
    lastCheck: float = 0
    lastInstall: object = None    # {"time", "count", "firmware", "ok"}
    kernelChange: bool = False
    firmware: list = field(default_factory=list)
    error: object = None          # {"code", "detail"}
    installAttempts: int = 0
    brokenSources: list = field(default_factory=list)   # [{"uri", "problem"}]
    installPercent: int = 0       # while updates install in use
    checkPercent: int = 0         # while the lists of updates are read
    bytesPerSecond: int = 0       # while downloading
    secondsLeft: int = 0          # while downloading, 0 when it cannot be told


# --- the state file ---------------------------------------------------------

def checked_everything(state):
    """True only when the last check read every repository apt is configured with.

    🚨 A reader must not say "you are up to date" while this is false. With
    the Raye archive unreadable and Ubuntu having nothing to offer, `phase`
    is `idle` and `count` is 0 -- byte for byte what a machine that really is
    up to date looks like. "Nothing to install" would then be a statement
    about what we managed to see, dressed up as a statement about the PC.

    It is derived, not stored: one `not` away from `brokenSources`. It is
    written into the file anyway because four reader repositories would
    otherwise each derive it on their own, and the day one of them forgets
    the `phase == "error"` half, nothing says so.
    """
    return not state.brokenSources and state.phase != "error"


def to_json(state):
    """The file contents: version 2 keys plus the old keys of version 1."""
    d = asdict(state)
    d["checkedEverything"] = checked_everything(state)
    legacy = state.phase
    if legacy in ("armed", "installing"):
        legacy = "ready"
    if state.phase in ("ready", "installing") and not state.needsRestart and not state.armed:
        # old readers offer a restart for "ready": these install in use
        legacy = "idle"
    if state.phase == "waiting" or (state.phase == "ready" and state.waitReason):
        reason = state.waitReason
    elif state.phase == "error" and state.error:
        reason = state.error.get("detail", "") or state.error.get("code", "")
    else:
        reason = ""
    d.update({"state": legacy, "updates": state.count,
              "packages": [p.get("name", "") for p in state.pending],
              "reason": reason, "checked": state.lastCheck,
              "ready_id": state.readyId, "paused_until": state.pausedUntil})
    return d


def from_json(d):
    """A State from the file contents, version 1 or 2; unknown keys ignored."""
    if not isinstance(d, dict):
        return State()
    if d.get("version", 1) < 2:
        # version 1 prepared its downloads through PackageKit: download again
        paused = float(d.get("paused_until", 0) or 0)
        return State(phase="paused" if paused else "idle",
                     pending=[{"name": n, "from": "", "to": "", "origin": "", "kind": "system"}
                              for n in d.get("packages", []) if isinstance(n, str)],
                     count=int(d.get("updates", 0) or 0),
                     lastCheck=float(d.get("checked", 0) or 0),
                     pausedUntil=paused)
    known = {f.name for f in fields(State)}
    state = State(**{k: v for k, v in d.items() if k in known})
    state.version = 2
    if validate(to_json(state)):
        return State(pausedUntil=_num(d.get("pausedUntil")),
                     activeHours=_hours_or_default(d.get("activeHours")),
                     autoRestart=d.get("autoRestart") is True,
                     lastInstall=d.get("lastInstall") if isinstance(d.get("lastInstall"), dict) else None)
    return state


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _hours_or_default(h):
    if isinstance(h, dict) and valid_active_hours(h.get("start"), h.get("end")):
        return {"start": h["start"], "end": h["end"]}
    return {"start": 8, "end": 17}


def _is(v, kind):
    if isinstance(v, bool):
        return kind is bool
    if kind == "number":
        return isinstance(v, (int, float))
    return kind is not bool and isinstance(v, kind)


def validate(d):
    """Problems of a version 2 state dict, as a list of strings (empty: valid)."""
    bad = []

    def want(key, kind):
        if key not in d:
            bad.append(f"{key}: missing")
            return False
        if not _is(d[key], kind):
            bad.append(f"{key}: wrong type")
            return False
        return True

    if not isinstance(d, dict):
        return ["not an object"]
    if want("version", int) and d["version"] != 2:
        bad.append("version: not 2")
    if want("phase", str) and d["phase"] not in PHASES:
        bad.append("phase: unknown")
    if want("waitReason", str) and d["waitReason"] not in WAIT_REASONS:
        bad.append("waitReason: unknown")
    if want("pending", list):
        for p in d["pending"]:
            if not isinstance(p, dict) or not all(_is(p.get(k), str) for k in
                                                  ("name", "from", "to", "origin", "kind")):
                bad.append("pending: bad entry")
            elif p["kind"] not in KINDS:
                bad.append("pending: unknown kind")
            elif "restart" in p and not _is(p["restart"], bool):
                bad.append("pending: bad restart")
            elif "state" in p and p["state"] not in PACKAGE_STATES:
                bad.append("pending: unknown state")
    for key in ("count", "downloadedBytes", "totalBytes", "installAttempts"):
        if want(key, int) and d[key] < 0:
            bad.append(f"{key}: negative")
    for key in ("needsRestart", "autoRestart", "kernelChange"):
        want(key, bool)
    want("readyId", str)
    for key in ("pausedUntil", "lastCheck"):
        want(key, "number")
    if want("activeHours", dict) and not valid_active_hours(d["activeHours"].get("start"),
                                                            d["activeHours"].get("end")):
        bad.append("activeHours: out of range")
    if "armed" not in d:
        bad.append("armed: missing")
    elif d["armed"] is not None:
        a = d["armed"]
        if not isinstance(a, dict) or a.get("then") not in ("restart", "shutdown") \
                or not _is(a.get("at"), "number"):
            bad.append("armed: bad value")
    if "lastInstall" not in d:
        bad.append("lastInstall: missing")
    elif d["lastInstall"] is not None:
        li = d["lastInstall"]
        if not isinstance(li, dict) or not _is(li.get("ok"), bool) \
                or not all(_is(li.get(k), "number") for k in ("time", "count", "firmware")):
            bad.append("lastInstall: bad value")
    if want("firmware", list):
        for f in d["firmware"]:
            if not isinstance(f, dict) or not all(_is(f.get(k), str) for k in
                                                  ("device", "deviceId", "from", "to", "summary")) \
                    or not _is(f.get("size"), int) \
                    or not all(_is(f.get(k, ""), str) for k in ("sha256", "location")) \
                    or not _is(f.get("needsPower", False), bool):
                bad.append("firmware: bad entry")
    # added after the first version 2 files were written: a file without it
    # means "none known", never a reason to throw the whole file away (a
    # ready, downloaded set would be forgotten and fetched all over again)
    if "brokenSources" in d and _is(d["brokenSources"], list):
        for s in d["brokenSources"]:
            if not isinstance(s, dict) or not _is(s.get("uri"), str) or not s.get("uri"):
                bad.append("brokenSources: bad entry")
            elif s.get("problem") not in SOURCE_PROBLEMS:
                bad.append("brokenSources: unknown problem")
    elif "brokenSources" in d:
        bad.append("brokenSources: wrong type")
    # derived, and added with brokenSources: a file without it was written by
    # a client that only ever had the all-clear case, so its absence means
    # "everything was checked" and must not throw the file away
    if "installPercent" in d and (not _is(d["installPercent"], int)
                                  or not 0 <= d["installPercent"] <= 100):
        bad.append("installPercent: bad value")
    # progress keys, added in 0.6: optional
    if "checkPercent" in d and (not _is(d["checkPercent"], int) or not 0 <= d["checkPercent"] <= 100):
        bad.append("checkPercent: bad value")
    for key in ("bytesPerSecond", "secondsLeft"):
        if key in d and (not _is(d[key], int) or d[key] < 0):
            bad.append(f"{key}: bad value")
    if "checkedEverything" in d and not _is(d["checkedEverything"], bool):
        bad.append("checkedEverything: wrong type")
    if "error" not in d:
        bad.append("error: missing")
    elif d["error"] is not None:
        e = d["error"]
        if not isinstance(e, dict) or e.get("code") not in ERROR_CODES \
                or not _is(e.get("detail", ""), str):
            bad.append("error: bad value")
    return bad


def save(state, path=None):
    path = path or STATE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".state-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(to_json(state), f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def locked(wait=True):
    """Held while a command changes the file, so two writers never cross.
    wait=False: yields False at once when someone else holds it."""
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    fd = os.open(os.path.join(os.path.dirname(STATE), ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if wait:
                raise
            yield False
            return
        yield True
    finally:
        os.close(fd)


OWNER_KEYS = ("armed", "pausedUntil", "activeHours", "autoRestart")


def save_run(state):
    """Save from a long run (check, download): the keys a person sets
    (arm, pause, active hours) may have changed meanwhile, the file wins."""
    with locked():
        disk = load()
        for key in OWNER_KEYS:
            setattr(state, key, getattr(disk, key))
        if state.pausedUntil and not is_paused(state):
            state.pausedUntil = 0          # the pause is over
        if disk.armed and disk.phase == "armed":
            state.phase = "armed"
        elif is_paused(state) and state.phase not in ("error",):
            state.phase = "paused"
        save(state)


def load(path=None):
    path = path or STATE
    try:
        with open(path) as f:
            return from_json(json.load(f))
    except (OSError, ValueError, TypeError):
        return State()


def set_error(state, code, detail=""):
    state.phase = "error"
    state.waitReason = ""
    state.error = {"code": code if code in ERROR_CODES else "unknown",
                   "detail": (detail or "")[-300:]}


# --- reading apt ------------------------------------------------------------

_INST = re.compile(r"^Inst (\S+)(?: \[([^\]]*)\])? \((\S+) (.*?)(?: \[[^\]]*\])?\)")


def classify(name, origins):
    """kernel, driver, security, app or system, for the history and Settings."""
    base = name.split(":", 1)[0]
    # raye-kernel only points at a kernel: counted once, with that kernel
    if re.match(r"^linux-(image|modules|headers|generic|signed|raye)", base):
        return "kernel"
    if re.match(r"^(nvidia-|libnvidia-|linux-firmware|firmware-|intel-microcode|amd64-microcode)", base):
        return "driver"
    if any(o.endswith("-security") for o in origins):
        return "security"
    if origins and not any(o.split(":", 1)[0] in ("Ubuntu", "Raye") for o in origins):
        return "app"
    return "system"


def _kernel_key(name):
    m = _RAYE_IMAGE.match(name)
    return tuple(int(n) for n in re.findall(r"\d+", name[len("linux-image-"):])) if m else None


def kernel_followups(images, headers, available):
    """The newer Raye kernel to install with the upgrade: [] when this PC
    has none, or already has the newest. Its headers come too when the
    ones of the kernel in use are there (DKMS builds against them)."""
    mine = [n for n in images if _kernel_key(n)]
    if not mine:
        return []
    newest = max(_kernel_key(n) for n in mine)
    newer = [n for n in available if _kernel_key(n) and _kernel_key(n) > newest and n not in images]
    if not newer:
        return []
    image = max(newer, key=_kernel_key)
    out = [image]
    header = "linux-headers-" + image[len("linux-image-"):]
    if any(h.startswith("linux-headers-") and h.endswith("-raye") for h in headers) and header in available:
        out.append(header)
    return out


def _installed(pattern):
    rc, out, _ = run(["dpkg-query", "-W", "-f", "${Package} ${Status}\n", pattern])
    if rc != 0:
        return []
    return [line.split()[0] for line in out.splitlines() if line.endswith(" installed")]


def raye_kernel_followups():
    """kernel_followups() for this PC, from dpkg and the apt indexes."""
    if not FOLLOW_RAYE_KERNEL:
        return []
    # once linux-raye is published, kernel updates come through it, and
    # raye-kernel-setup installs it only after the key that signs it is in:
    # by name, the kernel of the 1.0 release would go in on a PC of the
    # beta, signed with a key it does not have yet
    if run(["apt-cache", "show", "linux-raye"])[0] == 0:
        return []
    images = _installed("linux-image-*-raye")
    if not images:
        return []
    available = []
    for prefix in ("linux-image-", "linux-headers-"):
        rc, out, _ = run(["apt-cache", "pkgnames", prefix])
        if rc == 0:
            available += [n for n in out.split() if n.endswith("-raye")]
    followups = kernel_followups(images, _installed("linux-headers-*-raye"), available)
    # one that cannot install (not all published yet) would fail the whole
    # upgrade: it waits for the next check instead
    if followups and run(["apt-get", "-s", *APT_OPTS, "install", *followups])[0] != 0:
        log(f"raye-update: {' '.join(followups)} cannot install yet, it waits")
        return []
    return followups


def upgrade_args(extra=None):
    """`upgrade --with-new-pkgs`, and the newer Raye kernel when there is one."""
    return [*UPGRADE, *(raye_kernel_followups() if extra is None else extra)]


def settle_raye_kernels(followups):
    """A kernel named on the command line is marked as asked for, and would
    never be removed. Marked automatic, the Raye kernels follow apt's kernel
    rule (the one in use and the newest two stay) and unattended-upgrades
    takes the older ones away."""
    if not followups:
        return
    mine = _installed("linux-image-*-raye") + _installed("linux-headers-*-raye")
    if mine:
        run(["apt-mark", "auto", *mine])


def parse_simulation(output):
    """Pending packages from `apt-get -s upgrade` ("Inst name [old] (new origins [arch])")."""
    pending = []
    for line in output.splitlines():
        m = _INST.match(line)
        if not m:
            continue
        name, old, new, origin_text = m.group(1), m.group(2) or "", m.group(3), m.group(4)
        origins = [o.strip() for o in origin_text.split(",") if o.strip()]
        security = [o for o in origins if o.endswith("-security")]
        pending.append({"name": name, "from": old, "to": new,
                        "origin": (security or origins or [""])[0],
                        "kind": classify(name, origins)})
    return pending


def parse_print_uris(output):
    """Bytes still to download, from `apt-get --print-uris -qq`."""
    total = 0
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].startswith("'"):
            try:
                total += int(parts[2])
            except ValueError:
                pass
    return total


_STATUS = re.compile(r"^(dlstatus|pmstatus|pmerror|pmconffile|media-change):(.*?):(\d+(?:\.\d+)?):(.*)$")


def parse_status(line):
    """(kind, item, percent, message) of an APT::Status-Fd line, or None."""
    m = _STATUS.match(line.rstrip("\n"))
    if not m:
        return None
    return m.group(1), m.group(2), float(m.group(3)), m.group(4)


def classify_error(text):
    """A plain-words code for what apt printed when it failed."""
    t = text or ""
    if re.search(r"No space left on device|don't have enough free space", t):
        return "no-space"
    if re.search(r"Temporary failure resolving|Could not resolve|Network is unreachable|"
                 r"Could not connect|Connection timed out|No route to host", t):
        return "no-internet"
    if re.search(r"Failed to fetch|Hash Sum mismatch|404\s+Not Found|50\d |"
                 r"Some index files failed|Release file .* is not valid|"
                 r"is not signed|GPG error|NO_PUBKEY|"
                 # any of apt's five "The repository '...'" refusals: the
                 # servers did not give us something we could use, which is
                 # what "server" says. "Something went wrong" says nothing.
                 r"The repository '", t):
        return "server"
    if re.search(r"unmet dependencies|held broken packages|Unable to correct problems", t):
        return "broken"
    return "unknown"


# Which repositories apt has, and which ones it could not read.
#
# Measured on apt 3.0.3: a name that does not resolve is a W: and apt exits 0,
# while a 404 or a signature that does not check is an E: and apt exits 100.
# So the exit code alone cannot tell "our archive is down, Ubuntu is fine"
# from "this machine reached nothing at all" -- and those two need opposite
# answers. apt does name every repository it failed on, so we read that.
_FETCH_FAILED = re.compile(r"^[WE]: (?:Failed to fetch|GPG error:)\s+(\S+)(.*)$", re.M)
# EVERY "The repository '...'" message, not only the one about a missing
# signature. apt 3.0.3 has five, and reading only "is not signed" left four
# of them nameless -- which made the whole check an error and threw away the
# Ubuntu security updates apt HAD read. The list and the one-line command
# that re-derives it from libapt are in tests/test_sources.py#REPO_MESSAGES.
_REPO_REFUSED = re.compile(r"^[WE]: The repository '(\S+)[^']*'\s+(.*)$", re.M)
_RELEASE_BAD = re.compile(r"^[WE]: Release file for (\S+) (is not valid.*|is expired.*)$", re.M)
_REPO_PATTERNS = (_FETCH_FAILED, _REPO_REFUSED, _RELEASE_BAD)


def source_problem(reason):
    """A plain-words code for why apt could not read one repository."""
    r = reason or ""
    if re.search(r"Could not resolve|Temporary failure resolving|Could not connect|"
                 r"Network is unreachable|Connection (?:timed out|failed|refused)|"
                 r"No route to host|Unable to connect", r):
        return "offline"
    # "does not have a Release file" is the server answering with nothing at
    # all -- the unpublished archive every beta tester meets. It has to be
    # caught HERE: the "invalid" test below looks for "Release file" and would
    # otherwise swallow it and call a missing archive a damaged one.
    if re.search(r"40[34]\b|Not Found|File not found|"
                 r"(?:does not have|no longer has) a Release file", r):
        return "missing"
    # a repository that STOPS being signed is the shape of a downgrade attack,
    # and an index that offers only weak checksums was refused by apt for
    # security reasons: both are "what it served could not be trusted"
    if re.search(r"is not signed|no longer signed|weak security information|"
                 r"NO_PUBKEY|EXPKEYSIG|KEYEXPIRED|public key is not available|"
                 r"signature|keyring", r):
        return "unsigned"
    if re.search(r"Hash Sum mismatch|Size mismatch|is not valid|isn't valid|"
                 r"is expired|Release file", r):
        return "invalid"
    return "unknown"


def configured_sources():
    """The repository URIs apt is configured with, asked of apt itself.

    Never read from sources.list by hand: deb822 .sources files, per-entry
    options and mirror+file: each have their own spelling, and apt already
    knows them all. It answers from the configuration alone, so it still
    answers when every download has just failed.
    """
    rc, out, _ = run(["apt-get", "indextargets", "--no-release-info"], timeout=120)
    if rc != 0:
        return []
    uris = []
    for line in out.splitlines():
        if line.startswith("Repo-URI:"):
            uri = line.split(":", 1)[1].strip().rstrip("/")
            if uri and uri not in uris:
                uris.append(uri)
    return uris


def any_source_failed(text):
    """True when apt named a repository it could not read, whichever it was.

    Needed when apt could not tell us which repositories it has: we cannot
    count them then, but we can still tell "something failed" apart from
    "everything was read".
    """
    return any(p.search(text or "") for p in _REPO_PATTERNS)


def parse_failed_sources(text, configured):
    """[{"uri", "problem"}] for each configured repository apt could not read.

    Only the W:/E: lines are read, never the `Err:` lines of the progress:
    with mirror+file: those name the mirror apt happened to pick
    (https://dl.rayelinux.com/apt), which is not a configured repository,
    while the W: line names the one that is
    (mirror+file:/etc/apt/mirrors/raye.list). Reading the Err: lines would
    match nothing at all, for ever, without a word.
    """
    found = {}
    for pattern in _REPO_PATTERNS:
        for uri, reason in pattern.findall(text or ""):
            # the longest configured URI that this failure belongs to
            owner = max((c for c in configured if uri == c or uri.startswith(c + "/")),
                        key=len, default=None)
            if owner is None:
                continue
            problem = source_problem(reason)
            if owner not in found or (found[owner] == "unknown" and problem != "unknown"):
                found[owner] = problem
    return [{"uri": c, "problem": found[c]} for c in configured if c in found]


# --- the Raye archive: is its address and its key coherent? -----------------
#
# Neither the address nor the key is chosen here. The source file and the
# keyring ship with raye-archive-keyring, and the signing key belongs to the
# owner. What this client owns is the one question it is placed to ask, four
# times a day, on every PC: does the installed configuration hang together?
#
# It matters because the three ways of getting it wrong -- an address with no
# key, a key with no address, a key the archive is not signed with -- all end
# as one quiet "unsigned" line and a beta that never receives a fix. Measured
# on apt 3.0.3: all three print "The repository '...' is not signed", exit
# 100, and nothing else.
ARCHIVE_SOURCE = "/etc/apt/sources.list.d/raye.sources"
ARCHIVE_MIRRORS = "/etc/apt/mirrors/raye.list"

# ⏳ PROVISIONAL, and empty on purpose: the key that will sign the archive
# does not exist yet -- the owner makes it and keeps it. The day it does,
# its 40-hex fingerprint goes HERE, on this one line, and nothing else in
# this repository has to change: archive_problems() then refuses any keyring
# that does not carry it. While it is empty nothing is pinned, so a swapped
# keyring is invisible -- which is why release mode refuses an empty pin and
# no image can ship without this line filled in.
ARCHIVE_FINGERPRINTS = ()

ARCHIVE_PROBLEMS = ("no-source", "no-address", "insecure-address", "no-key",
                    "key-missing", "key-unreadable", "bad-fingerprint",
                    "wrong-key", "key-not-pinned", "provisional-address")

# Hosts that only ever stand in for the real one. A beta that updates from
# any of these updates from nowhere, and says nothing.
_PROVISIONAL_HOST = re.compile(
    r"^(?:localhost$|[\d.]+$|\[?[0-9a-fA-F:]+\]?$)|"
    r"\.(?:example|invalid|test|localhost|local)$", re.I)


def parse_deb822(text):
    """{field: value} of a one-stanza deb822 file, field names lowercased."""
    out = {}
    for line in (text or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        out[name.strip().lower()] = value.strip()
    return out


def archive_config(source=None, mirrors=None):
    """What is installed for the Raye archive, read and nothing more.

    `mirrors` overrides where the mirror list is read from; left out, it is
    the path inside the `mirror+file:` URI, which is what apt itself uses.
    """
    source = source or ARCHIVE_SOURCE
    try:
        with open(source) as f:
            fields = parse_deb822(f.read())
    except OSError:
        return {"source": source, "exists": False, "uris": [], "keyring": "", "mirrors": []}
    uris = fields.get("uris", "").split()
    keyring = fields.get("signed-by", "").strip()
    through_list = [u for u in uris if u.startswith("mirror+file:")]
    found = []
    if through_list or mirrors:
        path = mirrors or through_list[0].split(":", 1)[1]
        try:
            with open(path) as f:
                for line in f:
                    line = line.split("#", 1)[0].strip()
                    if line:
                        found.append(line.split()[0])
        except OSError:
            found = []
    else:
        found = list(uris)
    return {"source": source, "exists": True, "uris": uris,
            "keyring": keyring, "mirrors": found}


def well_formed_fingerprint(value):
    """A full 40-hex OpenPGP v4 fingerprint, spaces as gpg prints them allowed.

    Nothing shorter: a short key id can be collided on, and apt pins by the
    keyring file anyway -- this pin exists to notice a keyring that was
    swapped, so half of one would notice half a swap.
    """
    if not isinstance(value, str):
        return False
    return bool(re.fullmatch(r"[0-9a-fA-F]{40}", value.replace(" ", "")))


def keyring_fingerprints(path):
    """The fingerprints of the PRIMARY keys a keyring holds.

    All of them, not the first: a key rotation puts the old key and the new
    one in one keyring so that neither the PCs that have updated nor the ones
    that have not are locked out. Reading only the first would report
    "wrong-key" against a perfectly good keyring the day the pin moves.

    Subkeys are skipped: an `fpr:` record belongs to the `pub:` or `sub:`
    above it, and a subkey is not what the archive is pinned to.

    [] when the file holds no key -- which includes a file that is not there
    at all -- and None when the question could not be ASKED. None is never a
    pass: archive_notes() says so.

    🚨 `rc != 0` used to mean [], and that broke the distinction this very
    docstring draws. gpg exits 2 both for "that file holds no key" and for
    "I could not start", so a gpg that never ran was reported as a keyring
    that holds no key -- and "no key" is an ANSWER about the archive, while
    "could not ask" is not. Measured 2026-11-07, and it is easy to reach:
    with GNUPGHOME naming a directory that does not exist, gpg refuses even
    `--show-keys`, which reads nothing but the file it is handed:

        gpg: Fatal: /nope: directory does not exist!      rc=2, no output

    So the two failures are told apart by what gpg blames, and the split is
    a whitelist: a message about the FILE gives [], anything else gives None.
    Anything unrecognised is therefore "could not ask", which is the safe
    side -- the opposite default is what made this lie.
    """
    if not shutil.which("gpg"):
        return None
    rc, out, err = run(["gpg", "--batch", "--with-colons", "--show-keys", path], timeout=30)
    if rc != 0:
        # measured messages: "no valid OpenPGP data found." for an empty or
        # malformed file, "can't open '<path>': No such file or directory"
        # for one that is not there. Both are answers about the file.
        if "no valid OpenPGP data" in err or "can't open" in err:
            return []
        last = err.strip().splitlines()[-1] if err.strip() else "(gpg said nothing)"
        log(f"raye-update: gpg could not read the keyring {path}: rc={rc} {last}")
        return None
    found, primary = [], False
    for line in out.splitlines():
        if line.startswith(("pub:", "sub:")):
            primary = line.startswith("pub:")
        elif line.startswith("fpr:") and primary:
            parts = line.split(":")
            if len(parts) > 9 and parts[9]:
                found.append(parts[9])
    return found


def _archive_findings(cfg, pinned=None, release=False):
    """(problems, notes). A note is something that could not be checked."""
    pinned = ARCHIVE_FINGERPRINTS if pinned is None else pinned
    problems, notes = [], []
    if not cfg.get("exists"):
        return ["no-source"], notes
    if not cfg.get("uris") or not cfg.get("mirrors"):
        problems.append("no-address")
    for m in cfg.get("mirrors") or []:
        # anything but https is refused, not only http: an unknown scheme
        # passing in silence is how every other hole in this file got in
        if not re.match(r"(?:\w+\+)?https://", m):
            problems.append("insecure-address")
        host = re.sub(r"^\w+(?:\+\w+)?://", "", m).split("/")[0].split("@")[-1]
        if _PROVISIONAL_HOST.search(host.rsplit(":", 1)[0] if host.count(":") == 1 else host):
            (problems if release else notes).append("provisional-address")
    keyring = cfg.get("keyring") or ""
    if not keyring:
        problems.append("no-key")
    elif not os.path.exists(keyring):
        problems.append("key-missing")
    elif not os.path.getsize(keyring):
        problems.append("key-unreadable")
    bad_pin = [f for f in pinned if not well_formed_fingerprint(f)]
    if bad_pin:
        problems.append("bad-fingerprint")
    elif not pinned:
        # nothing is pinned, so nothing here can notice a swapped keyring
        (problems if release else notes).append("key-not-pinned")
    elif keyring and os.path.exists(keyring) and os.path.getsize(keyring):
        held = keyring_fingerprints(keyring)
        if held is None:
            notes.append("key-not-read")
            if release:
                problems.append("key-not-pinned")
        elif not held:
            problems.append("key-unreadable")
        elif not any(h.replace(" ", "").lower() in
                     {f.replace(" ", "").lower() for f in pinned} for h in held):
            problems.append("wrong-key")
    seen = []
    for p in problems:
        if p not in seen:
            seen.append(p)
    return seen, sorted(set(notes))


def archive_problems(cfg, pinned=None, release=False):
    """Why the installed archive configuration cannot work; [] when it can."""
    return _archive_findings(cfg, pinned, release)[0]


def archive_notes(cfg, pinned=None, release=False):
    """What could NOT be checked. Never counted as a pass, never a failure
    on a PC -- apt verifies the signature itself whatever we managed to ask."""
    return _archive_findings(cfg, pinned, release)[1]


# Raye Insider: the update channel. Stable is the archive's own suite in
# raye.sources, a conffile of raye-archive-keyring that this file never
# writes; every other channel is ONE more suite of the same archive -- same
# mirrors, same key -- in a file that only Raye Update writes. apt then takes
# the newest version of each package from either suite, so leaving a channel
# never downgrades anything: what came from it stays until Stable catches up.
INSIDER_SOURCE = "/etc/apt/sources.list.d/raye-insider.sources"
CHANNELS = ("stable", "public-beta", "beta", "dev")
CHANNEL_SUITES = {"public-beta": "resolute-public-beta",
                  "beta": "resolute-beta", "dev": "resolute-dev"}
# a test tree may stand in for the mirrors (root only, never a setting):
# https, or a local folder given by its absolute path
_INSIDER_URI = re.compile(r"^(?:(?:mirror\+file|file):/[^\s]*|https://[^\s/]+(?:/[^\s]*)?)$")


class InsiderError(Exception):
    pass


def current_channel(source=None):
    """The channel this PC is on: stable without our file, unknown when the
    file names a suite that is not one of ours."""
    try:
        with open(source or INSIDER_SOURCE) as f:
            suite = parse_deb822(f.read()).get("suites", "").strip()
    except OSError:
        return "stable"
    for channel, name in CHANNEL_SUITES.items():
        if suite == name:
            return channel
    return "unknown"


def insider_source_text(channel, cfg=None):
    """The apt source of a channel: its suite on the archive's mirrors."""
    if channel not in CHANNEL_SUITES:
        raise ValueError(channel)
    cfg = cfg or archive_config()
    if not cfg.get("exists") or not cfg.get("uris") or not cfg.get("keyring"):
        raise InsiderError(f"no Raye archive source in {cfg.get('source')}")
    uris, keyring = " ".join(cfg["uris"]), cfg["keyring"]
    test_uris = os.environ.get("RAYE_INSIDER_URIS")
    if test_uris is not None:
        if not _INSIDER_URI.match(test_uris):
            raise InsiderError(f"a test tree is https or an absolute folder: {test_uris!r}")
        test_key = os.environ.get("RAYE_INSIDER_KEYRING", "")
        if not (test_key.startswith("/") and os.path.isfile(test_key)
                and not re.search(r"\s", test_key)):
            raise InsiderError("a test tree needs RAYE_INSIDER_KEYRING, an existing keyring")
        uris, keyring = test_uris, test_key
    return ("# Raye Insider channel, written by Raye Update. Change it in\n"
            "# Settings > Raye Update > Raye Insider (or: raye-update channel set stable).\n"
            f"Types: deb\nURIs: {uris}\nSuites: {CHANNEL_SUITES[channel]}\n"
            "Components: main\nArchitectures: amd64\n"
            f"Signed-By: {keyring}\n")


def set_channel(channel, source=None):
    """Moves this PC to `channel`; returns the one it was on."""
    if channel not in CHANNELS:
        raise ValueError(channel)
    path = source or INSIDER_SOURCE
    before = current_channel(path)
    if channel == "stable":
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return before
    text = insider_source_text(channel)       # raises before anything is written
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".raye-insider-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return before


LEAVE_NOTE = ("Back on Stable. Nothing is downgraded: the newer versions you got stay "
              "until Stable catches up, and they keep getting their fixes from Stable then.")


def channel_command(args):
    """`raye-update channel [status]` for anyone, `channel set NAME` as root."""
    if args in ([], ["status"]):
        channel = current_channel()
        print(json.dumps({"channel": channel, "suite": CHANNEL_SUITES.get(channel, ""),
                          "channels": list(CHANNELS), "source": INSIDER_SOURCE}, indent=2))
        return 0
    if len(args) not in (2, 3) or args[0] != "set" or args[1] not in CHANNELS \
            or args[2:] not in ([], ["--no-check"]):
        print("raye-update: channel [status] | channel set "
              + "|".join(CHANNELS) + " [--no-check]", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("raye-update: needs root", file=sys.stderr)
        return 1
    try:
        before = set_channel(args[1])
    except InsiderError as e:
        print(f"raye-update: {e}", file=sys.stderr)
        return 1
    log(f"raye-update: update channel {before} -> {args[1]}")
    if args[1] == "stable" and before != "stable":
        print(LEAVE_NOTE)
    if args[2:] != ["--no-check"]:
        # a new channel is a new set of updates: look for them now, as a
        # person asked (check-now)
        try:
            os.makedirs(os.path.dirname(CHECK_ASKED), exist_ok=True)
            with open(CHECK_ASKED, "w") as f:
                f.write(f"{time.time():.0f}\n")
        except OSError:
            pass
        run(["systemctl", "start", "--no-block", "raye-update.service"], timeout=60)
    return 0


def is_lock_error(text):
    return bool(re.search(r"Could not get lock|Unable to acquire the dpkg frontend lock|"
                          r"is held by process", text or ""))


def ready_key(pending, firmware):
    """Changes whenever the set of updates changes."""
    items = sorted(f"{p['name']}={p['to']}" for p in pending)
    items += sorted(f"fw:{f['deviceId']}={f['to']}" for f in firmware)
    return hashlib.sha1("\n".join(items).encode()).hexdigest()[:12] if items else ""


# --- the machine ------------------------------------------------------------

def battery_level(root="/sys/class/power_supply"):
    """(on_ac, lowest battery percent or None) from sysfs."""
    on_ac, levels = False, []
    try:
        entries = os.listdir(root)
    except OSError:
        return True, None
    for name in entries:
        base = os.path.join(root, name)
        try:
            kind = open(os.path.join(base, "type")).read().strip()
        except OSError:
            continue
        if kind == "Mains":
            try:
                on_ac = on_ac or open(os.path.join(base, "online")).read().strip() == "1"
            except OSError:
                pass
        elif kind == "Battery":
            # a wireless mouse, keyboard, headset or controller reports its own
            # battery (scope "Device"): only the PC's battery counts, or a
            # desktop with a low mouse waited for a charger it does not have
            try:
                if open(os.path.join(base, "scope")).read().strip().lower() == "device":
                    continue
            except OSError:
                pass
            try:
                levels.append(int(open(os.path.join(base, "capacity")).read().strip()))
            except (OSError, ValueError):
                pass
    if not levels:
        return True, None          # a desktop: always on mains
    return on_ac, min(levels)


def why_wait(on_ac, battery, metered, gaming):
    """The reason not to download now, or "" when it is a good moment."""
    if gaming:
        return "game"
    if metered:
        return "metered"
    if not on_ac and battery is not None and battery < MIN_BATTERY:
        return "battery"
    return ""


def is_metered():
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "GENERAL.METERED", "device", "show"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(line.split(":", 1)[-1].startswith("yes") for line in out.splitlines())


def is_gaming(flag=None):
    """A game is running if Game mode set its flag, or GameMode says so."""
    if os.path.exists(flag or GAME_FLAG):
        return True
    try:
        out = subprocess.run(["gamemoded", "-s"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "is active" in out


def free_bytes(path):
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_bavail * st.f_frsize


# --- running tools ----------------------------------------------------------

def run(cmd, timeout=3600):
    """(returncode, stdout, stderr) of a command; 127 when it is missing."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, **APT_ENV})
    except FileNotFoundError as e:
        return 127, "", str(e)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    return p.returncode, p.stdout, p.stderr


def run_with_status(cmd, on_status, timeout=7200, keep=40):
    """Run an apt command with APT::Status-Fd on a pipe; on_status(kind, item, percent, msg)
    for every status line. Returns (returncode, the last `keep` lines of output,
    all of it with keep=None)."""
    r, w = os.pipe()
    full = cmd[:1] + ["-o", f"APT::Status-Fd={w}"] + cmd[1:]
    try:
        p = subprocess.Popen(full, pass_fds=(w,), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             env={**os.environ, **APT_ENV})
    except OSError as e:
        os.close(r)
        os.close(w)
        return 127, str(e)
    os.close(w)
    tail = []
    # held while a line is handed over; once apt is done nothing more is:
    # a late line put an old percentage back on the boot screen
    handing, done = threading.Lock(), []

    def drain():
        for raw in p.stdout:
            tail.append(raw.decode(errors="replace"))
            if keep:
                del tail[:-keep]

    def follow():
        # a daemon started by a maintainer script may keep the pipe open,
        # so the end of the process decides, not the end of the pipe
        with os.fdopen(r, errors="replace") as status:
            for line in status:
                parsed = parse_status(line)
                with handing:
                    if done:
                        return
                    if parsed:
                        on_status(*parsed)
    out_thread = threading.Thread(target=drain, daemon=True)
    status_thread = threading.Thread(target=follow, daemon=True)
    out_thread.start()
    status_thread.start()
    try:
        rc = p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        rc = p.wait()
    # what apt wrote before it ended still counts, if it is read in time
    status_thread.join(2)
    with handing:
        done.append(True)
    out_thread.join(5)
    return rc, "".join(tail)


def plymouth(*args):
    try:
        subprocess.run(["plymouth", *args], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def plymouth_running():
    """The boot screen is up. Not on level 1 of the graphics ladder
    (plymouth.enable=0), nor when plymouthd died."""
    try:
        return subprocess.run(["plymouth", "--ping"], capture_output=True,
                              timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# Steps that hold apt's number for minutes: the start-up image rebuilt for
# each kernel, a driver built for it, the boot menu written again.
_SLOW_ITEM = re.compile(r"^(initramfs-tools|linux-(image|headers|modules|signed)|raye-kernel|"
                        r"[\w.+-]*dkms|grub|shim-signed|raye-system)")


def slow_item(item):
    return bool(_SLOW_ITEM.match((item or "").split(":", 1)[0]))


# the console of the start-up, when there is no boot screen to draw on
TEXT_TTY = "/dev/tty1"
LOCALE_FILES = ("/etc/default/locale", "/etc/locale.conf")
TEXT_WORDS = {
    "en": {"updates": "Working on updates  {n}%", "note": "Don't turn off your PC",
           "slow": "Preparing the new version of the system. This can take a few minutes."},
    "fr": {"updates": "Mise à jour en cours  {n}%", "note": "N'éteignez pas votre PC",
           "slow": "Préparation de la nouvelle version du système. "
                   "Cela peut prendre quelques minutes."},
    "es": {"updates": "Trabajando en las actualizaciones  {n}%", "note": "No apagues el PC",
           "slow": "Preparando la nueva versión del sistema. Esto puede tardar unos minutos."},
    "de": {"updates": "Updates werden bearbeitet  {n}%", "note": "Schalten Sie den PC nicht aus",
           "slow": "Die neue Version des Systems wird vorbereitet. Dies kann einige Minuten dauern."},
    "pt": {"updates": "Trabalhando nas atualizações  {n}%", "note": "Não desligue o PC",
           "slow": "Preparando a nova versão do sistema. Isso pode levar alguns minutos."},
    "it": {"updates": "Aggiornamenti in corso  {n}%", "note": "Non spegnere il PC",
           "slow": "Preparazione della nuova versione del sistema. Può richiedere qualche minuto."},
}


def system_language(files=None):
    for path in files or LOCALE_FILES:
        try:
            with open(path) as f:
                for line in f:
                    m = re.match(r'\s*LANG\s*=\s*"?([a-z]{2})', line)
                    if m:
                        return m.group(1) if m.group(1) in TEXT_WORDS else "en"
        except OSError:
            continue
    return "en"


class OfflineScreen:
    """What the install at start-up shows: the boot screen's words through
    plymouth, or the same words on the console when there is no boot screen
    (level 1 of the graphics ladder): a quiet start with the cursor off left
    the console blank for the whole install."""

    def __init__(self):
        self.text = not plymouth_running()
        self.words = TEXT_WORDS[system_language()] if self.text else None
        self.percent = 0
        self.slow = False

    def progress(self, pct):
        self.percent = int(pct)
        plymouth("system-update", f"--progress={self.percent}")
        self.draw()

    def item(self, item):
        self.set_slow(slow_item(item))

    def set_slow(self, on):
        if on == self.slow:
            return
        self.slow = on
        plymouth("display-message", "--text=raye:slow-step" if on else "--text=raye:slow-step-done")
        self.draw()

    def draw(self):
        if not self.text:
            return
        w = self.words
        body = "\033[H\033[2J\n\n    " + w["updates"].format(n=self.percent) + "\n\n    " \
            + (w["slow"] if self.slow else w["note"]) + "\n"
        try:
            with open(TEXT_TTY, "a") as f:     # a tty: "a" or "w" alike
                f.write(body)
        except OSError:
            pass                    # never in the way of the install


# --- the switch to the Raye kernel (Raye Linux 1.0) ---------------------------

KERNEL_SETUP = "/usr/libexec/raye-kernel-setup"
# starts that tried the switch: three, then it is left to Settings
KERNEL_SWITCH_TRIES = "/var/lib/raye-update/kernel-switch-tries"
MAX_KERNEL_SWITCH_TRIES = 3
# how long the code stays up for a key before the start goes on
KERNEL_KEY_WAIT = 120
KERNEL_WORDS = {
    "en": {
        "code": "\n\n    Raye Linux 1.0: one last step, the Raye kernel\n\n"
                "    Your code:   {code}\n\n"
                "    After the restart, a blue screen shows for 10 seconds:\n"
                "      1. Press any key.\n"
                "      2. Choose \"Enroll MOK\", then \"Continue\", then \"Yes\".\n"
                "      3. Type the code above and press Enter.\n"
                "      4. Choose \"Reboot\".\n\n"
                "    If you miss it, nothing breaks: Raye starts as usual.\n\n"
                "    Press any key to restart now.\n"
                "    Without a key, Raye goes on in {minutes} minutes and asks again at the next start.\n",
        "installed": "\n\n    Raye Linux 1.0: switching to the Raye kernel.\n\n    Restarting...\n",
    },
    "fr": {
        "code": "\n\n    Raye Linux 1.0 : une dernière étape, le noyau Raye\n\n"
                "    Votre code :   {code}\n\n"
                "    Après le redémarrage, un écran bleu s'affiche 10 secondes :\n"
                "      1. Appuyez sur une touche.\n"
                "      2. Choisissez « Enroll MOK », puis « Continue », puis « Yes ».\n"
                "      3. Tapez le code ci-dessus et appuyez sur Entrée.\n"
                "      4. Choisissez « Reboot ».\n\n"
                "    Si vous le manquez, rien ne casse : Raye démarre normalement.\n\n"
                "    Appuyez sur une touche pour redémarrer maintenant.\n"
                "    Sans touche, Raye continue dans {minutes} minutes et vous le redemandera au prochain démarrage.\n",
        "installed": "\n\n    Raye Linux 1.0 : passage au noyau Raye.\n\n    Redémarrage...\n",
    },
    "es": {
        "code": "\n\n    Raye Linux 1.0: un último paso, el kernel de Raye\n\n"
                "    Tu código:   {code}\n\n"
                "    Después del reinicio, se muestra una pantalla azul durante 10 segundos:\n"
                "      1. Presiona cualquier tecla.\n"
                "      2. Elige \"Enroll MOK\", luego \"Continue\" y luego \"Yes\".\n"
                "      3. Escribe el código de arriba y presiona Entrar.\n"
                "      4. Elige \"Reboot\".\n\n"
                "    Si te lo pierdes, no se rompe nada: Raye se inicia como siempre.\n\n"
                "    Presiona cualquier tecla para reiniciar ahora.\n"
                "    Sin tecla, Raye continúa en {minutes} minutos y te lo vuelve a pedir en el próximo inicio.\n",
        "installed": "\n\n    Raye Linux 1.0: cambiando al kernel de Raye.\n\n    Reiniciando...\n",
    },
    "de": {
        "code": "\n\n    Raye Linux 1.0: ein letzter Schritt, der Raye-Kernel\n\n"
                "    Ihr Code:   {code}\n\n"
                "    Nach dem Neustart wird 10 Sekunden lang ein blauer Bildschirm angezeigt:\n"
                "      1. Drücken Sie eine beliebige Taste.\n"
                "      2. Wählen Sie \"Enroll MOK\", dann \"Continue\", dann \"Yes\".\n"
                "      3. Geben Sie den Code oben ein, und drücken Sie die Eingabetaste.\n"
                "      4. Wählen Sie \"Reboot\".\n\n"
                "    Wenn Sie ihn verpassen, geht nichts kaputt: Raye startet wie gewohnt.\n\n"
                "    Drücken Sie eine beliebige Taste, um jetzt neu zu starten.\n"
                "    Ohne Taste fährt Raye in {minutes} Minuten fort und fragt beim nächsten Start erneut.\n",
        "installed": "\n\n    Raye Linux 1.0: Wechsel zum Raye-Kernel.\n\n    Neustart...\n",
    },
    "pt": {
        "code": "\n\n    Raye Linux 1.0: uma última etapa, o kernel do Raye\n\n"
                "    Seu código:   {code}\n\n"
                "    Depois da reinicialização, uma tela azul aparece por 10 segundos:\n"
                "      1. Pressione qualquer tecla.\n"
                "      2. Escolha \"Enroll MOK\", depois \"Continue\" e depois \"Yes\".\n"
                "      3. Digite o código acima e pressione Enter.\n"
                "      4. Escolha \"Reboot\".\n\n"
                "    Se você perder, nada quebra: o Raye inicia como de costume.\n\n"
                "    Pressione qualquer tecla para reiniciar agora.\n"
                "    Sem tecla, o Raye continua em {minutes} minutos e pergunta de novo na próxima inicialização.\n",
        "installed": "\n\n    Raye Linux 1.0: mudando para o kernel do Raye.\n\n    Reiniciando...\n",
    },
    "it": {
        "code": "\n\n    Raye Linux 1.0: un ultimo passaggio, il kernel di Raye\n\n"
                "    Il tuo codice:   {code}\n\n"
                "    Dopo il riavvio, viene mostrata una schermata blu per 10 secondi:\n"
                "      1. Premi un tasto qualsiasi.\n"
                "      2. Scegli \"Enroll MOK\", poi \"Continue\", poi \"Yes\".\n"
                "      3. Digita il codice qui sopra e premi Invio.\n"
                "      4. Scegli \"Reboot\".\n\n"
                "    Se lo perdi, non si rompe nulla: Raye si avvia come al solito.\n\n"
                "    Premi un tasto qualsiasi per riavviare ora.\n"
                "    Senza tasto, Raye prosegue tra {minutes} minuti e lo chiede di nuovo al prossimo avvio.\n",
        "installed": "\n\n    Raye Linux 1.0: passaggio al kernel di Raye.\n\n    Riavvio in corso...\n",
    },
}


def show_on_console(text):
    """The words on the start-up console, the boot screen gone."""
    run(["chvt", "1"], timeout=10)
    try:
        with open(TEXT_TTY, "a") as f:
            f.write("\033[H\033[2J" + text)
    except OSError:
        pass


def wait_for_key(seconds):
    """True when a key is pressed on the console within `seconds`."""
    import select
    import termios
    import tty
    try:
        fd = os.open(TEXT_TTY, os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return False
    try:
        old = termios.tcgetattr(fd)
    except termios.error:
        os.close(fd)
        return False
    try:
        tty.setcbreak(fd)
        termios.tcflush(fd, termios.TCIFLUSH)       # a key pressed earlier is not an answer
        ready, _, _ = select.select([fd], [], [], seconds)
        return bool(ready)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        os.close(fd)


def _kernel_switch_tries():
    try:
        with open(KERNEL_SWITCH_TRIES) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _count_kernel_switch_try(n):
    try:
        os.makedirs(os.path.dirname(KERNEL_SWITCH_TRIES), exist_ok=True)
        with open(KERNEL_SWITCH_TRIES, "w") as f:
            f.write(f"{n}\n")
    except OSError:
        pass


def kernel_switch():
    """At start-up, before the sign-in screen: switch this PC to the Raye
    kernel when raye-kernel-setup says it is due. With Secure Boot on, the
    key is queued and the console shows the code for the blue screen of the
    restart; a key restarts, no key withdraws the request and the start goes
    on (never a loop of restarts nobody sees). Without Secure Boot the kernel
    goes in at once and the PC restarts into its trial boot. Returns what
    happened: not-due, trial, given-up, offline, failed, code, left,
    installed."""
    if not os.path.exists(KERNEL_SETUP):
        return "not-due"
    rc, out, _ = run([KERNEL_SETUP, "switch-due"], timeout=120)
    if rc == 2 and out.strip() == "restart":
        # the kernel went in on this start (after the blue screen): its
        # trial boot is the next one, restart into it now
        plymouth("quit")
        show_on_console(KERNEL_WORDS[system_language()]["installed"])
        power("restart")
        return "trial"
    if rc != 0 or out.strip() != "yes":
        return "not-due"
    tries = _kernel_switch_tries()
    if tries >= MAX_KERNEL_SWITCH_TRIES:
        return "given-up"
    _, status, _ = run([KERNEL_SETUP, "status"], timeout=120)
    if status.strip() == "no-secure-boot" and run(["nm-online", "-q", "-t", "60"], timeout=90)[0] != 0:
        # the kernel may have to be downloaded: the next start, not a try
        log("raye-update: no network for the Raye kernel, next start")
        return "offline"
    _count_kernel_switch_try(tries + 1)
    rc, out, err = run([KERNEL_SETUP, "enroll"], timeout=1800)
    if rc != 0:
        log(f"raye-update: the switch to the Raye kernel did not start: {(out + err)[-300:]}")
        return "failed"
    words = KERNEL_WORDS[system_language()]
    code = out.strip()
    plymouth("quit")
    if re.fullmatch(r"[0-9]{6}", code):
        show_on_console(words["code"].format(code=" ".join(code), minutes=KERNEL_KEY_WAIT // 60))
        if wait_for_key(KERNEL_KEY_WAIT):
            power("restart")
            return "code"
        # nobody there: no blue screen at a restart nobody watches
        run(["mokutil", "--revoke-import"], timeout=60)
        log("raye-update: nobody pressed a key for the Raye key code, asked again next start")
        return "left"
    show_on_console(words["installed"])
    power("restart")
    return "installed"


def power(then):
    """Restart or turn off the machine. --no-block: the reboot stops the unit
    that called it, which must be allowed to end first. False when systemd
    refused (an inhibitor, such as the one apt holds while dpkg works): the
    refusal goes to the journal instead of being lost."""
    r = subprocess.run(["systemctl", "--no-block", "poweroff" if then == "shutdown" else "reboot"],
                       capture_output=True, text=True)
    if r is not None and getattr(r, "returncode", 0) != 0:
        log(f"raye-update: the restart was refused: {(r.stderr or r.stdout or '').strip()[-300:]}")
        return False
    return True


# --- check and download -----------------------------------------------------

def update_lists(on_percent=None, timeout=3600):
    """apt-get update: (returncode, stdout, stderr). With on_percent, the
    percentage apt reports goes to it as the lists come in."""
    cmd = ["apt-get", "update", *APT_OPTS]
    if on_percent is None:
        return run(cmd, timeout=timeout)
    done = [0]

    def on_status(kind, item, percent, msg):
        # apt counts files it learns about as it goes: never go back
        if kind == "dlstatus" and int(percent) > done[0]:
            done[0] = min(100, int(percent))
            on_percent(done[0])
    rc, out = run_with_status(cmd, on_status, timeout=timeout, keep=None)
    return rc, out, ""


def list_updates(on_percent=None, timeout=3600):
    """(pending, broken sources, error text or "").

    `pending` is None when nothing could be worked out; `broken` is None when
    the question was never reached (so what we already knew is kept).

    One repository that cannot be read no longer stops the others: apt keeps
    and uses the indexes it does have, so Ubuntu's security updates still
    come through while our own archive is down. The authority on "can we
    work out the list" is therefore the simulation, not the index refresh.

    The one case we refuse to answer is the opposite one: when EVERY
    repository failed we know nothing, and saying "up to date" to a machine
    that reached nothing is worse than saying nothing at all.
    """
    rc, out, err = update_lists(on_percent, timeout)
    text = (out or "") + "\n" + (err or "")
    if rc != 0 and is_lock_error(text):
        return None, None, err or out        # another install: simply come back later
    configured = configured_sources()
    broken = parse_failed_sources(text, configured)
    if configured and len(broken) >= len(configured):
        return None, broken, err or out
    if rc != 0 and not broken:
        # no repository explains this: let it be seen rather than waved
        # through because the simulation happens to answer
        return None, broken, err or out
    rc, out, err = run(["apt-get", "-s", *APT_OPTS, *upgrade_args()])
    if rc != 0:
        return None, broken, err or out
    pending = parse_simulation(out)
    if not configured and not pending and any_source_failed(text):
        # apt could not even say which repositories it has, so the count
        # above could not run: finding nothing may simply mean that nothing
        # was read, and that must not come out as "up to date"
        return None, broken, text
    return pending, broken, ""


def installed_packages(names):
    """The names among `names` dpkg has installed."""
    if not names:
        return set()
    _, out, _ = run(["dpkg-query", "-W", "-f=${Package} ${db:Status-Abbrev}\n", *sorted(names)],
                    timeout=120)
    found = set()
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith("ii"):
            found.add(parts[0].split(":", 1)[0])
    return found


def obsolete_plan(coming=()):
    """apt install arguments that swap out what Raye no longer uses, [] when
    there is nothing to do. `coming`: names the pending set brings in."""
    args = []
    for entry in OBSOLETE:
        have = installed_packages({entry["needs"], *entry["add"], *entry["remove"]})
        if entry["needs"] not in have and entry["needs"] not in coming:
            continue
        gone = [n for n in entry["remove"] if n in have]
        if gone and entry.get("image_only"):
            chosen = installed_by_a_person(gone)
            gone = [n for n in gone if n not in chosen]
        if gone and entry.get("raye_brought"):
            brought = brought_by_raye(gone)
            gone = [n for n in gone if n in brought]
        if gone:
            args += [n for n in entry["add"] if n not in have] + [n + "-" for n in gone]
    return args


def installed_by_a_person(names):
    """The names among `names` that apt's history shows installed or put
    back after the system was installed. A history that cannot be read
    counts as naming them all: in doubt, nothing is removed."""
    import gzip
    found = set()
    try:
        files = sorted(f for f in os.listdir(APT_HISTORY) if f.startswith("history.log"))
    except OSError:
        return set(names)
    for name in files:
        path = os.path.join(APT_HISTORY, name)
        try:
            opener = gzip.open if name.endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except (OSError, EOFError, ValueError):
            return set(names)
        for line in lines:
            m = _HISTORY_LINE.match(line)
            if not m:
                continue
            # "synaptic:amd64 (0.91.3), libept1.7:amd64 (1.2.2, automatic)"
            for item in re.split(r"\),\s*", m.group(2)):
                pkg = item.strip().split(" ", 1)[0].split(":", 1)[0]
                if pkg in names:
                    found.add(pkg)
    return found


def brought_by_raye(names):
    """The names among `names` that apt's history shows only coming in as
    a dependency (", automatic") of an install no person asked for (no
    Requested-By line: Raye Update runs as root on its own). One record of
    a person's install, or a history that cannot be read, keeps a name out:
    in doubt, nothing is removed."""
    import gzip
    raye, person = set(), set()
    try:
        files = sorted(f for f in os.listdir(APT_HISTORY) if f.startswith("history.log"))
    except OSError:
        return set()
    for name in files:
        path = os.path.join(APT_HISTORY, name)
        try:
            opener = gzip.open if name.endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except (OSError, EOFError, ValueError):
            return set()
        for block in re.split(r"\n\s*\n", text):
            asked = re.search(r"^Requested-By:", block, re.M) is not None
            for line in block.splitlines():
                m = _HISTORY_LINE.match(line)
                if not m:
                    continue
                for item in re.split(r"\),\s*", m.group(2)):
                    pkg = item.strip().split(" ", 1)[0].split(":", 1)[0]
                    if pkg not in names:
                        continue
                    if asked or "automatic" not in item or m.group(1) == "Reinstall":
                        person.add(pkg)
                    else:
                        raye.add(pkg)
    return raye - person


def parse_removals(output):
    """Names `apt-get -s` would remove ("Remv name [version]")."""
    return [line.split()[1].split(":", 1)[0] for line in output.splitlines()
            if line.startswith("Remv ") and len(line.split()) > 1]


def remove_obsolete():
    """Swaps out what OBSOLETE names, after the upgrade. True when there
    was something to do and it went in."""
    args = obsolete_plan()
    if not args:
        return False
    allowed = {n for e in OBSOLETE for n in e["remove"]}
    rc, out, err = run(["apt-get", "-s", *APT_OPTS, "--no-download", "install", *args])
    removals = parse_removals(out or "")
    if rc != 0 or not set(removals) <= allowed:
        log("raye-update: left the old packages alone, apt would remove more: %s"
            % " ".join(sorted(set(removals) - allowed) or [(err or out or "")[-200:]]))
        return False
    rc, out, err = run(["apt-get", *APT_OPTS, "-y", "--no-download", "install", *args])
    if rc != 0:
        log("raye-update: could not remove the old packages: %s" % (out + err)[-400:])
        return False
    return True


def clean_downloads(keep=(), archives=None):
    """Delete the downloaded packages once they are installed, the way
    apt-get clean would, but leave the ones named in `keep`: updates still
    waiting for the next start-up need their files then (--no-download).
    Under apt's own lock on the cache, so a download running in another
    apt is never touched. Returns how many files went."""
    archives = archives or ARCHIVES
    keep = {n.split(":", 1)[0] for n in keep}
    try:
        lock = open(os.path.join(archives, "lock"), "a")
    except OSError:
        return 0
    removed = 0
    with lock:
        try:
            fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("raye-update: the package cache is in use, it is cleaned next time")
            return 0
        for name in os.listdir(archives):
            if not name.endswith(".deb") or name.split("_", 1)[0] in keep:
                continue
            try:
                os.unlink(os.path.join(archives, name))
                removed += 1
            except OSError:
                pass
    return removed


PROC = "/proc"
OLD_WINE = "/opt/wine-stable/"


def old_wine_running():
    for pid in os.listdir(PROC):
        try:
            if pid.isdigit() and os.readlink(f"{PROC}/{pid}/exe").startswith(OLD_WINE):
                return True
        except OSError:
            pass
    return False


def swap_left_over(swap=True):
    """The swap a start-up install could not do. The 0.3 release downloaded
    the set that brought raye-wine in and knew nothing of raye-wine-compat,
    so the 0.5 install found it missing. Fetch it now; swap in use unless
    a restart is due anyway or a program of the old build still runs."""
    args = obsolete_plan()
    if not args:
        return False
    run(["apt-get", *APT_OPTS, "-y", "-d", "install", *args])
    if not swap or old_wine_running():
        return False
    return remove_obsolete()


def bytes_to_download():
    # the same options as the simulation: a phased update counted here and
    # left out there would be "still to download" forever
    rc, out, _ = run(["apt-get", "--print-uris", *APT_OPTS, "-qq", *upgrade_args()])
    return parse_print_uris(out) if rc == 0 else 0


def cached_set_plan():
    """What apt would install at start-up from what is downloaded. The
    lists may have been read again since the pending set was saved."""
    rc, out, _ = run(["apt-get", "-s", *APT_OPTS, *FROM_CACHE, *upgrade_args()])
    return parse_simulation(out) if rc == 0 else []


def cached_set(pending):
    """The entries of `pending` apt can install from what is downloaded,
    None when apt cannot say."""
    rc, out, _ = run(["apt-get", "-s", *APT_OPTS, *FROM_CACHE, *upgrade_args()])
    if rc != 0:
        return None
    have = {(p["name"], p["to"]) for p in parse_simulation(out)}
    return [p for p in pending if (p["name"], p["to"]) in have]


clock = time.monotonic


class SpeedMeter:
    """Download speed and time left, written into the state as it goes.
    Averaged, so one slow second does not make the time left jump."""

    WINDOW = 1.0

    def __init__(self, state):
        self.state = state
        self.mark = None        # (time, bytes) of the last measure
        self.speed = 0.0

    def update(self, done):
        now = clock()
        if self.mark is None:
            self.mark = (now, done)
            return
        elapsed = now - self.mark[0]
        if elapsed < self.WINDOW:
            return
        rate = max(0, done - self.mark[1]) / elapsed
        self.speed = rate if not self.speed else 0.5 * self.speed + 0.5 * rate
        self.mark = (now, done)
        left = max(0, self.state.totalBytes - done)
        self.state.bytesPerSecond = int(self.speed)
        # a stalled download has no honest time left
        self.state.secondsLeft = int(round(left / self.speed)) if self.speed >= 1 else 0

    def stop(self):
        self.state.bytesPerSecond = self.state.secondsLeft = 0


def parse_fwupd(text):
    """Firmware updates from `fwupdmgr get-updates --json`."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    found = []
    for dev in data.get("Devices", []) if isinstance(data, dict) else []:
        if not isinstance(dev, dict):
            continue
        releases = dev.get("Releases") or []
        if not isinstance(releases, list) or not releases or not isinstance(releases[0], dict):
            continue
        rel = releases[0]          # fwupd lists the newest first
        size = rel.get("Size", 0)
        sums = rel.get("Checksum") or []
        sha256 = next((c.lower() for c in sums if isinstance(c, str)
                       and re.fullmatch(r"[0-9a-fA-F]{64}", c)), "")
        urls = [u for u in rel.get("Locations") or [] if isinstance(u, str)
                and u.startswith("https://")]
        flags = [f for f in (dev.get("Flags") or []) + (dev.get("Problems") or []) if isinstance(f, str)]
        found.append({"device": str(dev.get("Name", "")), "deviceId": str(dev.get("DeviceId", "")),
                      "from": str(dev.get("Version", "")), "to": str(rel.get("Version", "")),
                      "summary": str(rel.get("Summary", "")),
                      "size": size if isinstance(size, int) and not isinstance(size, bool) else 0,
                      "sha256": sha256, "location": urls[0] if urls else "",
                      "needsPower": any(f.startswith("require-ac") for f in flags)})
    return [f for f in found if f["deviceId"] and f["to"] and not firmware_current(f["from"], f["to"])]


def firmware_current(have, offered):
    """The device already runs the offered version or a newer one. fwupd
    still lists the dbx it just wrote when it cannot read its version back."""
    if have == offered:
        return True
    a, b = re.findall(r"\d+", have), re.findall(r"\d+", offered)
    if not a or not b or re.sub(r"\d+", "", have) != re.sub(r"\d+", "", offered):
        return False
    return [int(x) for x in a] >= [int(x) for x in b]


def firmware_file(fw, cache=None):
    """Where the cabinet of a firmware update is kept once downloaded."""
    return os.path.join(cache or FIRMWARE_CACHE, fw.get("sha256", "") + ".cab")


def sha256_of(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
    except OSError:
        return ""
    return h.hexdigest()


def firmware_ready(fw, cache=None):
    """The cabinet is on disk and matches the checksum from the metadata."""
    return bool(fw.get("sha256")) and sha256_of(firmware_file(fw, cache)) == fw["sha256"]


def fetch(url, dest, limit=MAX_FIRMWARE_BYTES):
    """Download url to dest with fwupd's own downloader (the LVFS only
    serves fwupd); False on any failure."""
    work = dest + ".part"
    shutil.rmtree(work, ignore_errors=True)
    try:
        os.makedirs(work)
        try:
            p = subprocess.run(["fwupdmgr", "download", url], cwd=work, capture_output=True,
                               text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired):
            return False
        got = [os.path.join(work, f) for f in os.listdir(work)]
        if p.returncode != 0 or len(got) != 1 or os.path.getsize(got[0]) > limit:
            return False
        os.replace(got[0], dest)
        return True
    except OSError:
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


def download_firmware(firmware, cache=None, get=None):
    """Fetch the cabinets now: the install at start-up has no network.
    Returns True when every cabinet is on disk and checked."""
    cache, get = cache or FIRMWARE_CACHE, get or fetch
    os.makedirs(cache, mode=0o755, exist_ok=True)
    keep = {os.path.basename(firmware_file(f, cache)) for f in firmware}
    for name in os.listdir(cache):
        if name not in keep:
            with contextlib.suppress(OSError):
                os.unlink(os.path.join(cache, name))
    ok = True
    for fw in firmware:
        if firmware_ready(fw, cache):
            continue
        dest = firmware_file(fw, cache)
        if not fw.get("sha256") or not fw.get("location") or not get(fw["location"], dest) \
                or not firmware_ready(fw, cache):
            with contextlib.suppress(OSError):
                os.unlink(dest)
            ok = False
    return ok


def list_firmware():
    """Firmware updates, [] when fwupd is missing, has none, or fails."""
    run(["fwupdmgr", "refresh", "--no-metadata-check"], timeout=300)
    rc, out, _ = run(["fwupdmgr", "get-updates", "--json"], timeout=300)
    return parse_fwupd(out) if rc == 0 else []   # 2: nothing to do


def settle(state, pending, firmware, cached=False):
    """Mark which updates wait for the restart. An install in use that
    already failed on this very set leaves all of it for the restart: no new
    try, and no new pair of restore points. Returns the key of the set."""
    mark_restart(pending, cached)
    key = ready_key(pending, firmware)
    if key and key == (state.lastInstall or {}).get("restartSet"):
        for p in pending:
            p["restart"] = True
    return key


# the check says how far it got while it reads the lists (off in the tests,
# where apt is a fake that knows nothing of a status pipe)
CHECK_PROGRESS = True


def nap(seconds):
    time.sleep(seconds)


def take_check_asked(now=None):
    """True once after "Check for updates" was pressed (check-now): the mark
    is used up. A mark older than a person would wait is not trusted."""
    try:
        age = (time.time() if now is None else now) - os.stat(CHECK_ASKED).st_mtime
        os.unlink(CHECK_ASKED)
    except OSError:
        return False
    return age < ASKED_LOCK_WAIT + 3600


def check(state, now=None, lists_timeout=3600, firmware=True, asked=False):
    """Read the lists and work out the pending set. firmware=False keeps the
    firmware already known (a look right before an install or a restart:
    fwupd can take minutes and has nothing to do with apt's versions).
    asked: a person pressed "Check for updates" and watches the page: an apt
    lock another install holds (a fresh system's first start) is waited for,
    still "Checking", instead of "Waiting" until the timer comes back."""
    state.phase, state.waitReason, state.error = "checking", "", None
    state.checkPercent = 0
    save_run(state)
    def show(pct):
        state.checkPercent = pct
        save_run(state)
    try:
        pending, broken, err = list_updates(show if CHECK_PROGRESS else None, lists_timeout)
        waited = 0
        while asked and pending is None and is_lock_error(err) and waited < ASKED_LOCK_WAIT:
            nap(ASKED_LOCK_NAP)
            waited += ASKED_LOCK_NAP
            pending, broken, err = list_updates(show if CHECK_PROGRESS else None, lists_timeout)
    finally:
        state.checkPercent = 0
    if pending is None and not is_lock_error(err) and dpkg_half_configured():
        # an install cut short (power loss): finish it, then ask apt again
        repair_dpkg()
        pending, broken, err = list_updates()
    if broken is not None:
        if broken != state.brokenSources:
            # the timer checks four times a day: say it when it changes, so
            # the line that matters is not buried under identical ones
            for s in broken:
                log(f"raye-update: {s['uri']} could not be read ({s['problem']})")
            for s in state.brokenSources:
                if not any(b["uri"] == s["uri"] for b in broken):
                    log(f"raye-update: {s['uri']} can be read again")
        state.brokenSources = broken
    if pending is None:
        if is_lock_error(err):
            state.phase, state.waitReason = "waiting", "busy"
        else:
            set_error(state, classify_error(err), err)
        return state
    firmware = list_firmware() if firmware else state.firmware
    key = settle(state, pending, firmware)
    if not key:
        state.totalBytes = state.downloadedBytes = 0
    if key != state.readyId:
        state.readyId, state.needsRestart = "", False
        state.downloadedBytes = 0
        # the size is known before anything downloads, so a set that waits
        # (metered network, battery) can already say how big it is
        state.totalBytes = bytes_to_download() if pending else 0
    state.pending, state.count, state.firmware = pending, len(pending), firmware
    if state.readyId:
        state.needsRestart = restart_left(state)
    state.kernelChange = any(p["kind"] == "kernel" for p in pending)
    state.lastCheck = now if now is not None else time.time()
    state.phase = "ready" if state.readyId else "idle"
    return state


def download(state, gaming=None, asked=False, timeout=None):
    """Download the pending set if now is a good moment; ready when done.
    asked: a person is waiting for it (Install now, Restart now), so a game
    or a metered network does not make it wait; a low battery still does."""
    key = ready_key(state.pending, state.firmware)
    if not key:
        state.phase, state.readyId, state.needsRestart = "idle", "", False
        state.totalBytes = state.downloadedBytes = 0
        return state
    on_ac, battery = battery_level()
    reason = why_wait(on_ac, battery, False if asked else is_metered(),
                      False if asked else (is_gaming() if gaming is None else gaming))
    if reason:
        state.phase, state.waitReason = "waiting", reason
        return state
    total = bytes_to_download() if state.pending else 0
    free = free_bytes(ARCHIVES)
    if free is not None and total and free < total + SPARE_BYTES:
        set_error(state, "no-space", f"{total} bytes needed, {free} free")
        return state
    state.totalBytes, state.downloadedBytes = total, 0
    if total:
        rc, out = _fetch(state, total, timeout)
        if rc != 0 and not is_lock_error(out) and classify_error(out) != "no-space" \
                and newer_since_the_check(state, timeout or 3600):
            # a file the archive replaced since the lists were read (a newer
            # version came out, the old one left): read the lists again and
            # take the newest, once
            log("raye-update: a newer version came out during the download, it is taken instead")
            total = bytes_to_download()
            state.totalBytes = total
            rc, out = _fetch(state, total, timeout) if total else (0, "")
        if rc != 0 and not keep_what_arrived(state, out):
            return state
        state.downloadedBytes = total
    swap = obsolete_plan({p["name"].split(":", 1)[0] for p in state.pending})
    if swap:
        # what replaces the old packages, ready for the offline install
        run(["apt-get", *APT_OPTS, "-y", "-d", "install", *swap])
    if state.firmware and not download_firmware(state.firmware):
        # the packages are ready; the firmware is tried again at the next check
        log("raye-update: a firmware update could not be downloaded")
    state.phase, state.waitReason, state.error = "ready", "", None
    state.readyId = ready_key(state.pending, state.firmware)
    state.needsRestart = restart_left(state)
    return state


def _fetch(state, total, timeout):
    """apt-get -d of the upgrade, its progress in the state. (rc, output)."""
    state.phase, state.waitReason = "downloading", ""
    state.downloadedBytes = 0
    save_run(state)
    last = [0.0, 0.0]
    meter = SpeedMeter(state)

    def on_status(kind, item, percent, msg):
        if kind != "dlstatus":
            return
        state.downloadedBytes = min(total, int(total * percent / 100))
        meter.update(state.downloadedBytes)
        now = clock()
        if percent - last[0] >= 2 or now - last[1] >= 2:   # do not flood the readers
            last[0], last[1] = percent, now
            save_run(state)
    try:
        cmd = ["apt-get", *APT_OPTS, "-y", "-d", *upgrade_args()]
        # a time limit only for a look right before a restart
        return run_with_status(cmd, on_status, timeout=timeout) if timeout else \
            run_with_status(cmd, on_status)
    finally:
        meter.stop()


def newer_since_the_check(state, timeout=3600):
    """After a download that failed: read the lists again. True when they
    now name other versions than the pending set (which then becomes the
    new one); False when nothing changed, or they could not be read."""
    pending, _broken, _err = list_updates(timeout=timeout)
    if pending is None:
        return False
    if {(p["name"], p["to"]) for p in pending} == {(p["name"], p["to"]) for p in state.pending}:
        return False
    state.pending, state.count = pending, len(pending)
    state.readyId = ""
    settle(state, pending, state.firmware)
    state.kernelChange = any(p["kind"] == "kernel" for p in pending)
    return True


def keep_what_arrived(state, out):
    """After a download that failed: True when what did arrive makes a set
    to install, which then becomes the pending set. A file the mirror does not
    have yet (a 404 while it syncs, a version replaced since the lists were
    read) must not keep back everything else, nor show an error. The rest is
    found again at the next check. False when the state now says why not."""
    if is_lock_error(out):
        state.phase, state.waitReason = "waiting", "busy"
        return False
    code = classify_error(out)
    have = None if code == "no-space" else cached_set(state.pending)
    if not have:
        set_error(state, code, out)
        return False
    missed = [p["name"] for p in state.pending if p not in have]
    log(f"raye-update: {len(missed)} updates did not download, they are tried again later: "
        + ", ".join(missed[:12]) + (f" ({out.strip().splitlines()[-1][-200:]})" if out.strip() else ""))
    state.pending, state.count = have, len(have)
    settle(state, have, state.firmware, cached=True)
    state.kernelChange = any(p["kind"] == "kernel" for p in have)
    return True


# --- installing while the PC is in use ----------------------------------------

def restart_left(state):
    """Something in the set only installs at the next start-up. Entries
    without the key come from before it existed, when everything did."""
    return bool(state.firmware) or any(p.get("restart", True) for p in state.pending)


def package_sources(names):
    """Source package of each name, as apt's candidate has it. Names apt
    does not know are left out."""
    if not names:
        return {}
    # the options after the names: the same apt the simulations ask
    _, out, _ = run(["apt-cache", "show", "--no-all-versions", *sorted(names), *APT_OPTS],
                    timeout=120)
    sources = {}
    for para in (out or "").split("\n\n"):
        name = source = ""
        for line in para.splitlines():
            if line.startswith("Package: "):
                name = line.split()[1]
            elif line.startswith("Source: "):
                source = line.split()[1]
        if name:
            sources.setdefault(name, source or name)
    return sources


def offline_names(pending):
    """The names in the set that only install at the next start-up."""
    def base(name):
        return name.split(":", 1)[0]
    offline = {p["name"] for p in pending if OFFLINE_PACKAGES.match(base(p["name"]))}
    sources = package_sources({base(p["name"]) for p in pending if p["name"] not in offline})
    offline |= {p["name"] for p in pending
                if OFFLINE_SOURCES.match(sources.get(base(p["name"]), ""))}
    return offline


DPKG_STATUS = "/var/lib/dpkg/status"


def is_raye(p):
    """An update from the Raye archive."""
    return p.get("origin", "").startswith("Raye:")


def _base(name):
    return name.split(":", 1)[0]


def kept_back(output):
    """Names apt keeps back, from the text of an upgrade simulation."""
    names, inside = [], False
    for line in output.splitlines():
        if line.startswith("The following packages have been kept back"):
            inside = True
            continue
        if inside:
            if line.startswith(" "):
                names += line.split()
            else:
                inside = False
    return names


def whole_raye_sources(pending, offline, cached=False, extra=()):
    """`offline`, and with it every other update of the Raye archive built
    from the same source as one that cannot install now (it needs a
    restart, or apt keeps it back). Raye's binary packages of one source
    are made to work together: raye-desktop 1.0 named a helper that only
    raye-system 1.0 has, and taking the first without the second left a
    PC without a sign-in screen (2026-11-19). Ubuntu's packages are left
    as they are, phasing and all. `extra`: what the upgrade also names (the
    newer Raye kernel), so it is seen as part of the plan."""
    offline = set(offline)
    raye = {_base(p["name"]) for p in pending if is_raye(p)}
    if not raye:
        return offline
    sources = package_sources(raye)
    for _ in range(4):
        waiting = {sources[_base(p["name"])] for p in pending
                   if p["name"] in offline and _base(p["name"]) in sources}
        try:
            with pins_file(pending, offline) as pins:
                rc, out, _ = run(["apt-get", "-s", *APT_OPTS, *pins,
                                  *(FROM_CACHE if cached else ()), *UPGRADE, *extra])
        except OSError:
            return offline
        if rc != 0:
            return offline          # live_set finds nothing either
        moving = {p["name"] for p in parse_simulation(out)}
        waiting |= {sources[_base(p["name"])] for p in pending
                    if p["name"] not in moving and _base(p["name"]) in sources}
        back = [n for n in kept_back(out) if _base(n) not in sources]
        if back:
            waiting |= {s for s in package_sources({_base(n) for n in back}).values()
                        if s in sources.values()}
        more = {p["name"] for p in pending
                if sources.get(_base(p["name"])) in waiting} - offline
        if not more:
            return offline
        offline |= more
    return offline


def dpkg_installed(path=None):
    """name -> (version, source, source version) of what dpkg has installed."""
    try:
        with open(path or DPKG_STATUS) as f:
            text = f.read()
    except OSError:
        return {}
    found = {}
    for para in text.split("\n\n"):
        fields = {}
        for line in para.splitlines():
            if line and not line[0].isspace() and ":" in line:
                key, _, value = line.partition(":")
                fields[key] = value.strip()
        name, status = fields.get("Package"), fields.get("Status", "")
        if not name or not status.endswith(" installed") or name in found:
            continue
        version = fields.get("Version", "")
        source, _, rest = fields.get("Source", name).partition(" ")
        source_version = rest.strip("() ") or version
        found[name] = (version, source, source_version)
    return found


def split_raye_sources(pending, installed=None):
    """Raye sources installed at two versions at once: one of their
    packages newer than another one that waits for a restart. Only an
    install in use that took part of a source does that (the Raye Update of
    0.5 did, on the way to 1.0 Beta). A package an older version left
    behind (older than the rest) does not count."""
    installed = dpkg_installed() if installed is None else installed
    split = set()
    for p in pending:
        if not is_raye(p) or not p.get("restart", True):
            continue
        mine = installed.get(_base(p["name"]))
        if not mine:
            continue
        for name, (_version, source, source_version) in installed.items():
            if source == mine[1] and name != _base(p["name"]) \
                    and version_newer(source_version, mine[2]):
                split.add(source)
    return split


# Raye releases known to break a PC (1.0.1: the taskbar froze some desktops,
# mouse and keyboard included). When one is installed and a newer version
# waits, nobody can be asked to click: the fix is armed at once and the PC
# restarts on its own a few minutes later to install it.
BROKEN_RELEASES = {"raye-shell": ("1.0.1",)}
BROKEN_RESTART_DELAY = 300


def broken_installed(pending, installed=None):
    """Names of the Raye packages installed at a release known to break a
    PC, for which a newer version waits in `pending`."""
    installed = dpkg_installed() if installed is None else installed
    out = set()
    for p in pending:
        if not is_raye(p):
            continue
        name = _base(p["name"])
        bad = BROKEN_RELEASES.get(name, ())
        mine = installed.get(name)
        if mine and mine[0] in bad and version_newer(p.get("to", ""), mine[0]):
            out.add(name)
    return out


def arm_broken(state, now, link=None, target=None):
    """Arm the set with a restart in BROKEN_RESTART_DELAY seconds when a
    broken release is installed and its fix is downloaded. True when armed."""
    broken = broken_installed(state.pending)
    if not broken or not state.readyId or state.armed:
        return False
    if arm(state, "restart", now + BROKEN_RESTART_DELAY, now, link, target) != 0:
        return False
    log("raye-update: " + ", ".join(sorted(broken))
        + " is a release that breaks some PCs: its fix installs at a restart in a few minutes")
    return True


def version_newer(a, b):
    """True when Debian version a is newer than b (dpkg's own comparison)."""
    if a == b:
        return False
    try:
        return subprocess.run(["dpkg", "--compare-versions", a, "gt", b],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def live_pins(pending, base="/etc/apt/preferences", offline=None):
    """apt preferences that keep every update that needs a restart where it
    is, so a plain upgrade takes the rest, and holds back whatever would
    drag one of them in."""
    if offline is None:
        offline = offline_names(pending)
    try:
        with open(base) as f:
            text = f.read().rstrip("\n") + "\n\n"
    except OSError:
        text = ""
    for p in pending:
        if p["name"] not in offline:
            continue
        if p["from"]:
            text += f"Package: {p['name']}\nPin: version {p['from']}\nPin-Priority: 1001\n\n"
        else:
            text += f"Package: {p['name']}\nPin: version *\nPin-Priority: -1\n\n"
    return text


@contextlib.contextmanager
def pins_file(pending, offline):
    """apt options for a preferences file nobody else writes; removed after.
    A check and an install in use can run at the same time."""
    os.makedirs(PINS_DIR, mode=0o755, exist_ok=True)
    fd, path = tempfile.mkstemp(dir=PINS_DIR, prefix="pins-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(live_pins(pending, offline=offline))
        yield ["-o", f"Dir::Etc::Preferences={path}"]
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def live_set(pending, pins=None, offline=None, cached=False):
    """Names of the pending updates that can install now, without a
    restart: what apt upgrades once everything that needs one is pinned.
    Empty when that cannot be worked out. `cached`: only from what is
    downloaded, as the install itself does; a check plans with everything."""
    if offline is None:
        offline = whole_raye_sources(pending, offline_names(pending), cached)
    if all(p["name"] in offline for p in pending):
        return set()
    if pins is None:
        try:
            with pins_file(pending, offline) as own:
                return live_set(pending, own, offline, cached)
        except OSError:
            return set()
    rc, out, _ = run(["apt-get", "-s", *APT_OPTS, *pins, *(FROM_CACHE if cached else ()), *UPGRADE])
    if rc != 0 or re.search(r"^Remv ", out, re.M):
        return set()
    sim = parse_simulation(out)
    wanted = {(p["name"], p["to"]) for p in pending}
    # a pin that did not hold, or something that was not in the set: the
    # whole set waits for the restart rather than guess
    if any(p["name"] in offline or (p["name"], p["to"]) not in wanted for p in sim):
        return set()
    return {p["name"] for p in sim}


def mark_restart(pending, cached=False):
    live = live_set(pending, cached=cached)
    for p in pending:
        p["restart"] = p["name"] not in live
    return pending


def live_waiting(state):
    """Downloaded updates that can install now, without a restart."""
    return bool(state.readyId) and not state.armed and state.phase == "ready" \
        and any(not p.get("restart", True) for p in state.pending)


@contextlib.contextmanager
def live_lock(path=None):
    """Held for the whole install in use; None when another one holds it."""
    path = path or LIVE_LOCK
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield None
            return
        yield fd
    finally:
        os.close(fd)


def live_running(path=None):
    with live_lock(path) as held:
        return held is None


def repair_dpkg():
    run(["dpkg", "--configure", "-a", "--force-confdef", "--force-confold"])


def install_live(state, asked=False, gaming=None, now=None):
    """Install, while the PC is in use, the downloaded updates that need no
    restart; the rest stays ready for the next start-up. A person asking
    (the Install button) skips the wait for a game or a metered network,
    never the one for a low battery."""
    if not live_waiting(state):
        return state
    on_ac, battery = battery_level()
    reason = why_wait(on_ac, battery, False if asked else is_metered(),
                      False if asked else (is_gaming() if gaming is None else gaming))
    if reason:
        log(f"raye-update: updates that need no restart wait ({reason})")
        # kept with phase ready: readers say why Install now did nothing
        state.waitReason = reason
        return state
    state.waitReason = ""
    if dpkg_half_configured():
        # an install cut short: apt cannot work anything out before this
        repair_dpkg()
    if not catch_up_live(state, asked, gaming, now):
        return state
    if not live_waiting(state):
        return state
    offline = whole_raye_sources(state.pending, offline_names(state.pending), cached=True)
    with pins_file(state.pending, offline) as pins:
        # the install reads the very file its simulation read
        return _install_live(state, pins, offline, now)


# A check this recent is the one that started the install: not read again.
CATCH_UP_FRESH = 300


def _copy(state):
    return from_json(to_json(state))


def _restore(state, saved):
    for key, value in vars(saved).items():
        setattr(state, key, value)


def catch_up_live(state, asked=False, gaming=None, now=None):
    """Right before an install in use: read the lists again and download
    what came out since the download, so the install takes the newest
    version and never one it knows is old. False when another apt is
    reading the lists (the install waits for its own turn).

    The install only takes what is downloaded AND what the lists name
    (--no-download): a newer version that could not be downloaded leaves
    that package for later, never its older file in its place."""
    now = time.time() if now is None else now
    if now - (state.lastCheck or 0) < CATCH_UP_FRESH:
        return True
    saved = _copy(state)
    check(state, now, firmware=False)
    if state.phase == "waiting":
        _restore(state, saved)
        state.waitReason = "busy"
        return False
    if state.phase == "error":
        # apt keeps the lists it had: the download is the newest it knows
        log("raye-update: the lists could not be read again, the downloaded set installs")
        _restore(state, saved)
        return True
    if state.pending and (not state.readyId or bytes_to_download()):
        download(state, gaming=gaming, asked=asked)
        if state.phase == "waiting":
            # a newer version cannot be downloaded now: what is here and
            # still the newest installs, the rest waits for its download
            have = cached_set(state.pending) or []
            state.pending, state.count = have, len(have)
            settle(state, have, state.firmware, cached=True)
            state.kernelChange = any(p["kind"] == "kernel" for p in have)
            state.readyId = ready_key(have, state.firmware)
            state.phase = "ready" if state.readyId else "idle"
            state.needsRestart = restart_left(state)
    return True


def clear_marks(state):
    """The per-update steps belong to one install in use, never to a set at rest."""
    for p in state.pending:
        p.pop("state", None)


def _entry_of(pending, item):
    """The pending entry apt names in a status line. apt adds the native
    architecture ("firefox:amd64") where the simulation did not ("firefox");
    a foreign one is named the same way in both ("libgl1:i386")."""
    base, _, arch = item.partition(":")
    exact = [p for p in pending if p["name"] == item]
    native = [p for p in pending if p["name"] == base] if arch in ("", NATIVE_ARCH) else []
    return (exact or native or [None])[0]


def _install_live(state, pins, offline, now):
    live = live_set(state.pending, pins, offline, cached=True)
    if not live:
        mark_restart(state.pending, cached=True)
        state.needsRestart = restart_left(state)
        return state
    state.phase, state.installPercent = "installing", 0
    for p in state.pending:
        if p["name"] in live:
            p["state"] = "queued"
    save_run(state)
    point = restore_point_before(now)
    last = [0, 0.0]

    def on_status(kind, item, percent, msg):
        entry = _entry_of(state.pending, item) if kind in ("pmstatus", "pmerror") else None
        moved = False
        if entry is not None and "state" in entry:
            new = "failed" if kind == "pmerror" else \
                "installed" if msg.startswith("Installed ") else "installing"
            if entry["state"] != new and entry["state"] not in ("installed", "failed"):
                entry["state"], moved = new, True
        if kind == "pmstatus":
            state.installPercent = max(state.installPercent, min(100, int(percent)))
        # do not flood the readers; a package that is done is always told
        done = moved and entry["state"] in ("installed", "failed")
        if done or ((moved or state.installPercent - last[0] >= 2) and clock() - last[1] >= 0.1):
            last[0], last[1] = state.installPercent, clock()
            save_run(state)
    if point:
        APT_ENV["DISABLE_APT_SNAPSHOT"] = "yes"
    try:
        rc, out = run_with_status(["apt-get", *APT_OPTS, *pins, "-y", *FROM_CACHE,
                                   *UPGRADE], on_status)
    finally:
        APT_ENV.pop("DISABLE_APT_SNAPSHOT", None)
    if rc != 0 and is_lock_error(out):
        # another install holds dpkg: nothing changed, try again next time
        restore_point_after(point)
        clear_marks(state)
        state.phase, state.installPercent = "ready", 0
        return state
    if rc != 0:
        log(f"raye-update: updates could not install in use, they wait for a restart: {out[-400:]}")
        repair_dpkg()
    restore_point_after(point)
    clear_marks(state)
    # what the cache can still install: the restart takes it. What turned up
    # since the download is not part of it; the next check finds it.
    rc_sim, sim, _ = run(["apt-get", "-s", *APT_OPTS, *FROM_CACHE, *upgrade_args()])
    left = parse_simulation(sim) if rc_sim == 0 else [p for p in state.pending if p["name"] not in live]
    for p in left:
        p["restart"] = True     # the next start-up installs all of it
    done = max(0, state.count - len(left))
    state.pending, state.count = left, len(left)
    state.kernelChange = any(p["kind"] == "kernel" for p in left)
    state.installPercent = 0
    gone = sorted(n for n in live if n not in {p["name"] for p in left})
    state.lastInstall = {"time": now if now is not None else time.time(), "count": done,
                         "firmware": 0, "ok": rc == 0, "packages": gone}
    if rc == 0:
        installed = {n.split(":", 1)[0] for n in gone}
        try:
            relaunch_updated_apps(installed)
        except Exception as e:     # never let this fail the install
            log(f"raye-update: apps were not opened again: {e}")
        try:
            restart_user_units(installed)
        except Exception as e:
            log(f"raye-update: services were not started again: {e}")
        waiting = {p["name"].split(":", 1)[0] for p in left}
        if waiting:
            # what swaps out old packages is downloaded with them for the restart
            waiting |= {a for a in obsolete_plan(waiting) if not a.endswith("-")}
        clean_downloads(keep=waiting)
    if left or state.firmware:
        state.phase, state.readyId = "ready", ready_key(left, state.firmware)
        state.needsRestart = True
        if rc != 0:
            state.lastInstall["restartSet"] = state.readyId
    else:
        state.phase, state.readyId, state.needsRestart = "idle", "", False
        state.totalBytes = state.downloadedBytes = 0
    return state


# Raye's own apps that keep nothing unsaved: once updated in use, an open one
# is closed and opened again, so the new version works at once instead of
# after the next sign-in. Apps where a person writes (Notepad...) are never
# closed: they could lose work.
RELAUNCH = {
    "raye-files": ("dolphin",),
    "raye-settings": ("raye-settings", "systemsettings"),
    # the account pages of Settings import it
    "qml6-module-org-raye-account": ("raye-settings", "systemsettings"),
    "raye-store": ("raye-store", "plasma-discover"),
    "raye-taskmanager": ("raye-taskmanager",),
}

# what an app opens on when it comes back: Settings was closed while the
# person watched Raye Update, so it reopens there
RELAUNCH_ARGS = {"raye-settings": ("--page", "update")}


def relaunch_command(program):
    return [f"/usr/bin/{program}", *RELAUNCH_ARGS.get(program, ())]


def running_apps(updated, proc="/proc"):
    """{(uid, program)} of the apps of RELAUNCH open in a person's session
    whose package was just updated, with the pids to close."""
    wanted = {exe for name in updated for exe in RELAUNCH.get(name.split(":", 1)[0], ())}
    found = {}
    if not wanted:
        return found
    try:
        pids = [d for d in os.listdir(proc) if d.isdigit()]
    except OSError:
        return found
    for pid in pids:
        try:
            uid = os.stat(os.path.join(proc, pid)).st_uid
            exe = os.readlink(os.path.join(proc, pid, "exe"))
        except OSError:
            continue
        # an updated binary still running reads "(deleted)" after its path
        program = os.path.basename(exe.removesuffix(" (deleted)"))
        if uid >= 1000 and program in wanted:
            found.setdefault((uid, program), []).append(int(pid))
    return found


def relaunch_updated_apps(updated, proc="/proc", kill=os.kill, runner=None,
                          alive=None, sleep=time.sleep, user_of=None):
    """Close the updated Raye apps that are open and open them again in the
    same session. Returns what was opened again, as (user, program)."""
    runner = runner or run
    alive = alive or (lambda pid: os.path.exists(os.path.join(proc, str(pid))))
    if user_of is None:
        import pwd
        user_of = lambda uid: pwd.getpwuid(uid).pw_name
    done = []
    for (uid, program), pids in sorted(running_apps(updated, proc).items()):
        for pid in pids:
            try:
                kill(pid, 15)
            except OSError:
                pass
        for _ in range(50):
            if not any(alive(pid) for pid in pids):
                break
            sleep(0.1)
        try:
            user = user_of(uid)
        except KeyError:
            continue
        rc, _, err = runner(["systemd-run", f"--machine={user}@.host", "--user", "--collect",
                             "--quiet", "--", *relaunch_command(program)], timeout=30)
        if rc == 0:
            done.append((user, program))
        else:
            log(f"raye-update: could not open {program} again for {user}: {err[-200:]}")
    return done


# user services started again once their package installed in use: the old
# copy keeps running otherwise until the next sign-in
USER_UNITS = {
    "raye-polkit-agent": ("raye-polkit-agent.service",),
    "raye-account": ("raye-accountd.service",),
}


def session_users(run_dir="/run/user", name_of=None):
    """Names of the people signed in (they have a /run/user/<uid>)."""
    if name_of is None:
        import pwd
        name_of = lambda uid: pwd.getpwuid(uid).pw_name
    names = []
    for entry in sorted(_listdir(run_dir)):
        if entry.isdigit() and int(entry) >= 1000:
            try:
                names.append(name_of(int(entry)))
            except KeyError:
                pass
    return names


def restart_user_units(updated, runner=None, users=None):
    """try-restart, in each session, the user services of the packages just
    updated (only the ones running). Returns the users it asked."""
    units = sorted({u for n in updated for u in USER_UNITS.get(n.split(":", 1)[0], ())})
    if not units:
        return []
    runner = runner or run
    users = session_users() if users is None else users
    for user in users:
        rc, _, err = runner(["systemctl", f"--machine={user}@.host", "--user", "try-restart",
                             *units], timeout=60)
        if rc != 0:
            log(f"raye-update: could not start {' '.join(units)} again for {user}: {err[-200:]}")
    return list(users)


def start_live(asked=False):
    """In a unit of its own: an upgrade of this package must not stop it."""
    unit = "raye-update-live@now.service" if asked else "raye-update-live@auto.service"
    run(["systemctl", "start", "--no-block", unit], timeout=60)


# --- pause, active hours, restarts -------------------------------------------

def pause(state, weeks, now=None):
    """Pause for 1 to MAX_PAUSE_WEEKS weeks; False if out of range."""
    if not isinstance(weeks, int) or isinstance(weeks, bool) or not 1 <= weeks <= MAX_PAUSE_WEEKS:
        return False
    state.pausedUntil = (now if now is not None else time.time()) + weeks * WEEK
    if state.phase != "armed":
        state.phase, state.waitReason = "paused", ""
    return True


def resume(state):
    state.pausedUntil = 0
    if state.phase == "paused":
        state.phase = "ready" if state.readyId else "idle"


def is_paused(state, now=None):
    return state.pausedUntil > (now if now is not None else time.time())


def valid_active_hours(start, end):
    if not all(isinstance(h, int) and not isinstance(h, bool) and 0 <= h <= 23 for h in (start, end)):
        return False
    return start != end and (end - start) % 24 <= MAX_ACTIVE_SPAN


def in_active_hours(hour, start, end):
    """True when the local hour is inside [start, end), the span may cross midnight."""
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def restart_decision(state, now, hour, gaming, on_ac=True):
    """Why the machine should restart now on its own ("scheduled", "auto"), or None.

    Never during a game; a picked time wins over active hours (someone chose
    it); the automatic restart needs the owner's consent, a downloaded set,
    a time outside active hours and mains power."""
    if gaming:
        return None
    if state.armed and state.armed.get("at") and state.armed["at"] <= now:
        return "scheduled"
    if state.autoRestart and state.phase in ("ready", "armed") and state.readyId \
            and on_ac and not in_active_hours(hour, state.activeHours["start"], state.activeHours["end"]):
        return "auto"
    return None


# --- arming the install at start-up -----------------------------------------

def offline_owner(link=None, target=None):
    """'raye' when /system-update is ours, 'other' when another tool armed it, None."""
    link, target = link or OFFLINE_LINK, target or OFFLINE_TARGET
    if not os.path.lexists(link):
        return None
    try:
        return "raye" if os.readlink(link) == target else "other"
    except OSError:
        return "other"


def arm(state, then, at=0, now=None, link=None, target=None):
    """Arm the downloaded set. 0 done, 3 nothing downloaded, 4 another tool armed."""
    link, target = link or OFFLINE_LINK, target or OFFLINE_TARGET
    # an install in use may be running: what it does not get to, the
    # start-up installs, so "Restart now" during it is fine
    in_use = state.phase == "installing" and not state.armed
    if not state.readyId or not (state.phase in ("ready", "armed", "paused") or in_use):
        return 3
    if offline_owner(link, target) == "other":
        return 4
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w") as f:
        f.write(state.readyId + "\n")
    tmp = link + ".raye-update"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, link)
    state.armed = {"then": then, "at": at}
    state.phase, state.installAttempts = "armed", 0
    return 0


def disarm(state, link=None, target=None):
    link, target = link or OFFLINE_LINK, target or OFFLINE_TARGET
    if offline_owner(link, target) == "raye":
        os.unlink(link)
    try:
        os.unlink(target)
    except OSError:
        pass
    state.armed = None
    if state.phase == "armed":
        state.phase = "paused" if is_paused(state) else ("ready" if state.readyId else "idle")


# --- the install at start-up ------------------------------------------------

def dpkg_half_configured():
    rc, out, _ = run(["dpkg", "--audit"], timeout=120)
    return bool(out.strip())


def still_pending(pending):
    """The entries of `pending` apt would still upgrade to the same version."""
    rc, out, _ = run(["apt-get", "-s", *APT_OPTS, *upgrade_args()])
    if rc != 0:
        return []
    left = {(p["name"], p["to"]) for p in parse_simulation(out)}
    return [p for p in pending if (p["name"], p["to"]) in left]


def install_packages(on_percent, followups=None, on_item=None):
    """Install the downloaded set. (ok, output tail). on_item gets the
    package (or trigger) apt works on, each time it changes."""
    last = []
    current = [None]

    # apt says the same whole number many times; each one costs a call to
    # plymouth, and they pile up behind a slow screen
    def on_status(kind, item, percent, msg):
        if kind == "pmstatus" and on_item is not None and item != current[0]:
            current[0] = item
            on_item(item)
        if kind == "pmstatus" and int(percent) not in last[-1:]:
            last.append(int(percent))
            on_percent(int(percent))
    run(["dpkg", "--configure", "-a", "--force-confdef", "--force-confold"])
    # what is downloaded: a package that turned up after the download (a
    # phase that reached this PC, a newer version) waits for next time
    # instead of failing the whole install with "unable to fetch". apt only
    # ever takes the version its lists name: an older file in the cache is
    # never installed in place of a newer one it could not download.
    followups = raye_kernel_followups() if followups is None else followups
    plan, held = start_up_holds(followups)
    with contextlib.ExitStack() as stack:
        pins = stack.enter_context(pins_file(plan, held)) if held else []
        cmd = ["apt-get", *APT_OPTS, *pins, "-y", *FROM_CACHE, *upgrade_args(followups)]
        rc, out = run_with_status(cmd, on_status)
        if rc != 0:
            # apt's own recovery, then one more try
            run(["dpkg", "--configure", "-a", "--force-confdef", "--force-confold"])
            run(["apt-get", *APT_OPTS, *pins, "-y", *FROM_CACHE, "-f", "install"])
            rc, out = run_with_status(cmd, on_status)
    if rc == 0:
        settle_raye_kernels(followups)
    return rc == 0, out


def start_up_holds(followups=()):
    """(plan, names held) for the install at start-up. The lists may be
    newer than the download (read again, then the PC stopped before the
    download ended): a Raye source of which only part of the newest version
    is downloaded waits whole, never installed in part (the rule of
    whole_raye_sources, at start-up too). Nothing held when nothing needs it."""
    rc, out, _ = run(["apt-get", "-s", *APT_OPTS, *upgrade_args(followups)])
    plan = parse_simulation(out) if rc == 0 else []
    if not any(is_raye(p) for p in plan):
        return plan, set()
    held = whole_raye_sources(plan, set(), cached=True, extra=followups)
    if held:
        log("raye-update: waits until all of it is downloaded: " + ", ".join(sorted(held)))
    return plan, held


def install_firmware(firmware, cache=None):
    """Install firmware updates from the cabinets downloaded beforehand
    (there is no network at start-up). False when one was skipped or failed."""
    if not firmware:
        return True
    on_ac, _ = battery_level()
    ready = [f for f in firmware if firmware_ready(f, cache) and (on_ac or not f.get("needsPower"))]
    for f in firmware:
        if f not in ready:
            why = "not downloaded" if not firmware_ready(f, cache) else "the charger is not plugged in"
            log(f"raye-update: skipping firmware for {f['device']}: {why}")
    if not ready:
        return False
    # 0 bound for the new firmware, 3 no disk bound to the TPM here, 127 not
    # installed; anything else and the disk would ask for its password
    rc, out, err = run([RAYE_TPM, "prepare-firmware"], timeout=600)
    if rc not in (0, 3, 127):
        # the disk would ask for its password at the next start
        log(f"raye-update: skipping firmware, the TPM could not be prepared: {(out + err)[-400:]}")
        return False
    ok = len(ready) == len(firmware)
    for i, f in enumerate(ready, 1):
        if len(ready) > 1:
            plymouth("display-message", f"--text=raye:firmware-step:{i}:{len(ready)}")
        rc, out, err = run(["fwupdmgr", "local-install", firmware_file(f, cache), f["deviceId"],
                            "--assume-yes", "--no-reboot-check", "--no-unreported-check",
                            "--no-metadata-check"], timeout=3600)
        if rc != 0:
            log(f"raye-update: firmware for {f['device']} failed ({rc}): {(out + err)[-400:]}")
            ok = False
    # fwupd writes db/dbx as it installs: bind the disk to what they are now.
    # If this fails, the TPM protection suspended above covers the next start.
    rc, out, err = run([RAYE_TPM, "prepare-firmware"], timeout=600)
    if rc not in (0, 3, 127):
        log(f"raye-update: the TPM opens the next start through its suspension: {(out + err)[-400:]}")
    return ok


def log(text):
    # the journal only: Plymouth owns the screen during the install
    print(text, file=sys.stderr, flush=True)


def boot_files():
    """{path: (size, mtime)} of what the next start reads: the kernels, the
    initramfs images and every file on the ESP."""
    found = {}
    paths = [os.path.join(BOOT_DIR, n) for n in _listdir(BOOT_DIR)
             if n.startswith(("vmlinuz", "initrd.img"))]
    for top, _dirs, files in os.walk(os.path.join(BOOT_DIR, "efi", "EFI")):
        paths += [os.path.join(top, n) for n in files]
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            continue
        found[path] = (st.st_size, st.st_mtime_ns)
    return found


def _listdir(path):
    try:
        return os.listdir(path)
    except OSError:
        return []


def restart_reasons(state, before, after):
    """Why the install needs a real restart rather than going on to the
    desktop; empty when it does not."""
    why = [p["name"] for p in state.pending if RESTART_PACKAGES.match(p["name"].split(":", 1)[0])]
    if before != after:
        why.append("/boot changed")
    if state.firmware:
        why.append("firmware")
    return why


def after_install(state, then, before):
    """The power action once the install is over: off when asked, a restart
    when the next start has to read something new, else go on."""
    if then == "shutdown":
        return "shutdown"
    if restart_reasons(state, before, boot_files()):
        return "restart"
    plymouth("display-message", "--text=raye:mode:boot")
    return "continue"


def shutdown_screen(link=None, target=None):
    """On the way down to a restart that installs updates: say so right
    away rather than "Restarting", since the update screen only comes once
    the PC has started again and the disk is open.

    PackageKit removes /system-update each time apt tells it the lists or
    the packages changed (20packagekit), whoever made the link: a look at
    the lists while an update waits for the restart, ours or anyone's, left
    the state armed and the next start without its install. Here, once
    PackageKit is down (the unit is ordered after it), an install the state
    still holds armed is set again. Another tool's link is never touched."""
    if offline_owner(link, target) is None:
        try:
            state = load()
        except Exception:           # never in the way of the restart
            state = None
        if state is not None and state.armed and state.phase == "armed" and state.readyId:
            armed = state.armed
            if arm(state, armed.get("then", "restart"), armed.get("at", 0),
                   link=link, target=target) == 0:
                save(state)
                log("raye-update: the update waiting for the next start had been"
                    " disarmed by another tool, it is set again")
    if offline_owner(link, target) == "raye":
        plymouth("display-message", "--text=raye:mode:prepare")
    return 0


def restart_after_install():
    """Restart with the ring still turning: the screen says "Restarting"
    instead of plymouth quitting and the reboot one starting from frame 0."""
    plymouth("display-message", "--text=raye:mode:reboot")
    power("restart")


def continue_boot():
    """Go on to the sign-in screen instead of starting again. /system-update
    is gone, so once systemd has read its units again default.target is the
    desktop one, not system-update.target. False when that did not work: the
    caller then restarts."""
    rc, _, err = run(["systemctl", "daemon-reload"], timeout=120)
    if rc != 0:
        log(f"raye-update: reload failed, restarting instead: {err[-300:]}")
        return False
    rc, out, _ = run(["systemctl", "show", "--property=Id", "--value", "default.target"], timeout=30)
    target = out.strip()
    if rc != 0 or not target or target == "system-update.target":
        log(f"raye-update: the start-up still goes to {target or 'nothing'}, restarting instead")
        return False
    # plymouth-quit.service hands the screen over once the sign-in screen
    # starts; quitting here froze the ring for the whole desktop start
    # start, not isolate: isolate would stop what udev started for the
    # hardware (the guest agent, Bluetooth), which nothing would start again
    rc, _, err = run(["systemctl", "--no-block", "start", "default.target"], timeout=60)
    if rc != 0:
        log(f"raye-update: could not go on to {target}, restarting instead: {err[-300:]}")
        return False
    return True


def restore_point_before(now=None):
    """Make the "Before updates" restore point once per install; a start
    that finishes an interrupted install keeps the first one. Its number,
    0 without snapper."""
    if not os.path.exists(SNAPPER_CONFIG):
        return 0
    try:
        with open(RESTORE_POINT) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        pass
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    rc, out, err = run([*SNAPPER, "create", "-t", "pre", "-p", "-c", "number",
                        "-d", f"Before updates of {day}", "-u", "important=yes,raye=update"],
                       timeout=600)
    number = out.strip()
    if rc != 0 or not number.isdigit():
        log(f"raye-update: no restore point before the updates: {(out + err)[-400:]}")
        return 0
    os.makedirs(os.path.dirname(RESTORE_POINT), exist_ok=True)
    with open(RESTORE_POINT, "w") as f:
        f.write(number + "\n")
    return int(number)


def restore_point_after(number):
    if number:
        rc, out, err = run([*SNAPPER, "create", "-t", "post", "--pre-number", str(number),
                            "-c", "number", "-d", "After updates",
                            "-u", "important=yes,raye=update"], timeout=600)
        if rc != 0:
            log(f"raye-update: no restore point after the updates: {(out + err)[-400:]}")
    try:
        os.unlink(RESTORE_POINT)
    except OSError:
        pass


def install_offline(state, now=None, link=None, target=None):
    """What runs at start-up in system-update.target. Returns the power action:
    "continue" (go on to the desktop), "restart", "shutdown" or None (another
    tool armed the start-up, leave it)."""
    link, target = link or OFFLINE_LINK, target or OFFLINE_TARGET
    if offline_owner(link, target) != "raye":
        return None
    then = (state.armed or {}).get("then", "restart")
    state.installAttempts += 1
    interrupted = state.installAttempts > 1 or state.phase == "installing"
    if state.installAttempts > MAX_ATTEMPTS:
        set_error(state, "install-failed", "gave up after several tries")
        _finish(state, ok=False, now=now, link=link, target=target)
        return then
    state.phase = "installing"
    save(state)

    # the mode as a message: plymouth change-mode reads the script again
    # from the disk, where it is the English fallback, not the words of
    # the language the initramfs drew in
    plymouth("display-message", "--text=raye:mode:updates")
    screen = OfflineScreen()
    screen.progress(0)
    if interrupted or dpkg_half_configured():
        plymouth("display-message", "--text=raye:finishing-updates")
    # the TPM, the restore point and dpkg's own repair hold the 0 a while
    screen.set_slow(True)
    coming = {p["name"] for p in state.pending} | {p["name"] for p in cached_set_plan()}
    if any(BOOT_CHAIN.match(n.split(":", 1)[0]) for n in coming):
        # a new shim, GRUB or dbx can change PCR 7 in ways raye-tpm cannot predict:
        # the TPM opens the next start without it, then seals to it again
        rc, out, err = run([RAYE_TPM, "suspend"], timeout=600)
        if rc not in (0, 3, 127):
            log(f"raye-update: the TPM could not be suspended, the disk may ask its password: "
                f"{(out + err)[-400:]}")
    point = restore_point_before(now)
    before = boot_files()
    # one pair of restore points for the whole install, not one per dpkg run
    if point:
        APT_ENV["DISABLE_APT_SNAPSHOT"] = "yes"
    try:
        ok, out = install_packages(screen.progress, on_item=screen.item)
        if ok:
            remove_obsolete()
    finally:
        APT_ENV.pop("DISABLE_APT_SNAPSHOT", None)
    if not ok:
        if dpkg_half_configured() and state.installAttempts < MAX_ATTEMPTS:
            # keep /system-update: the next start-up tries to finish
            set_error(state, "install-failed", out)
            state.phase = "installing"
            save(state)
            return "restart"
        restore_point_after(point)
        set_error(state, "install-failed", out)
        action = after_install(state, then, before)
        _finish(state, ok=False, now=now, link=link, target=target)
        return action
    fw_count = 0
    if state.firmware:
        # fwupd says nothing while it writes, often for a minute: the
        # screen shows the firmware words and the ring, not a number
        plymouth("display-message", "--text=raye:mode:firmware-upgrade")
        if install_firmware(state.firmware):
            fw_count = len(state.firmware)
        else:
            set_error(state, "firmware-failed")
    restore_point_after(point)
    skipped = still_pending(state.pending)
    if skipped:
        # not downloaded, or held back with something that was not: the next check offers them
        log("raye-update: left for the next round: " + ", ".join(p["name"] for p in skipped))
    clean_downloads()
    screen.set_slow(False)
    screen.progress(100)
    action = after_install(state, then, before)
    _finish(state, ok=True, now=now, link=link, target=target, firmware=fw_count)
    if skipped:
        state.lastInstall["count"] = max(0, state.lastInstall["count"] - len(skipped))
        save(state)
    return action


def _finish(state, ok, now, link, target, firmware=0):
    if offline_owner(link, target) == "raye":
        os.unlink(link)
    try:
        os.unlink(target)
    except OSError:
        pass
    state.lastInstall = {"time": now if now is not None else time.time(),
                         "count": state.count, "firmware": firmware, "ok": ok}
    state.armed, state.installAttempts = None, 0
    if ok:
        state.pending, state.count, state.firmware = [], 0, []
        state.readyId, state.needsRestart, state.kernelChange = "", False, False
        state.totalBytes = state.downloadedBytes = 0
        if state.error is None:
            state.phase = "idle"
    else:
        # the set stays downloaded, the next check offers it again
        state.readyId, state.needsRestart = "", False
    save(state)


# --- taskbar progress (user side) -------------------------------------------

def launcher_props(state):
    """(progress 0..1, visible) for the Unity launcher entry of Raye Settings."""
    if state.phase == "downloading" and state.totalBytes > 0:
        return min(1.0, state.downloadedBytes / state.totalBytes), True
    return 0.0, False


def emit_progress(progress, visible):
    props = f"{{'progress': <{progress:.3f}>, 'progress-visible': <{'true' if visible else 'false'}>}}"
    subprocess.run(["gdbus", "emit", "--session", "--object-path", "/org/raye/update",
                    "--signal", "com.canonical.Unity.LauncherEntry.Update",
                    f"'{SETTINGS_APP}'", props], capture_output=True, timeout=10)


def progress_loop(interval=1.0, sleep=time.sleep, emit=None):
    """Follow a download until it ends, then hide the bar."""
    emit = emit or emit_progress
    shown = None
    while True:
        value, visible = launcher_props(load())
        if (round(value, 3), visible) != shown:
            emit(value, visible)
            shown = (round(value, 3), visible)
        if not visible:
            return 0
        sleep(interval)


# --- command line -----------------------------------------------------------

def _int(text):
    if text is None or not re.fullmatch(r"-?\d{1,12}", text):
        raise ValueError(text)
    return int(text)


def parse_arm(args, now):
    """(then, at, now_flag) from arm's arguments; raises ValueError."""
    then, at, right_now = None, 0, False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--then" and i + 1 < len(args):
            then = args[i + 1]
            i += 2
        elif a == "--at" and i + 1 < len(args):
            at = _int(args[i + 1])
            i += 2
        elif a == "--now":
            right_now = True
            i += 1
        else:
            raise ValueError(a)
    if then not in ("restart", "shutdown"):
        raise ValueError("--then")
    if at and not now < at <= now + MAX_SCHEDULE:
        raise ValueError("--at")
    if at and right_now:
        raise ValueError("--now")
    return then, at, right_now


def refresh_armed(state, now, lists_timeout=3600, download_timeout=None, asked=False):
    """The set waiting for the next start is brought to the newest version:
    the lists are read again and what came out since is downloaded, the
    install at start-up stays armed. When the lists cannot be read, the
    set stays as it was (the newest this PC knows). When a newer version
    cannot be downloaded now, the start-up install leaves that package for
    the next round rather than install its older file (apt takes only what
    its lists name), and a Raye source waits whole (start_up_holds).

    A Raye source installed in part (an older Raye Update, 0.5 to 1.0 Beta)
    is finished first with what is downloaded: the sign-in screen depends
    on it, a newer version comes the round after."""
    if not state.armed or state.phase != "armed" or is_paused(state, now):
        return state
    split = split_raye_sources(state.pending)
    if split:
        log("raye-update: " + ", ".join(sorted(split))
            + " is finished at the next start before anything newer is looked for")
        return state
    saved = _copy(state)
    check(state, now, lists_timeout=lists_timeout, firmware=False)
    if state.phase in ("error", "waiting"):
        _restore(state, saved)
        save_run(state)
        return state
    if state.pending and (not state.readyId or bytes_to_download()):
        download(state, asked=asked, timeout=download_timeout)
        if state.phase == "error":
            # what is downloaded and still the newest installs at the start
            state.error, state.phase = None, "ready"
    newest = {(p["name"], p["to"]) for p in state.pending}
    if newest != {(p["name"], p["to"]) for p in saved.pending}:
        log("raye-update: the update waiting for the next start now has the newest versions")
    state.phase = "armed"
    save_run(state)
    return state


def run_auto(state, command, now):
    if state.phase == "installing" and state.armed:
        return 0                    # the install at start-up runs or was cut: leave it
    if state.phase == "armed":
        # the install at the next start takes the newest version, also one
        # that came out after the person chose to restart
        refresh_armed(state, now)
        return 0
    if state.phase == "installing":
        if live_running():
            return 0
        # an install in use that was cut (power loss): finish what dpkg
        # left before apt is asked anything; the set is still downloaded
        if dpkg_half_configured():
            repair_dpkg()
        state.phase, state.installPercent = ("ready" if state.readyId else "idle"), 0
    if is_paused(state, now):
        state.phase = "paused"
        save_run(state)
        return 0
    if state.pausedUntil:
        resume(state)               # the pause is over
    state = check(state, now, asked=take_check_asked())
    if command == "auto" and state.phase in ("idle", "ready") and (state.pending or state.firmware):
        # download what is new, or what was cleaned from the cache since
        if not state.readyId or (state.pending and bytes_to_download()) \
                or not all(firmware_ready(f) for f in state.firmware):
            state = download(state)
    if command == "auto" and state.phase in ("idle", "ready") and not live_running():
        if swap_left_over(swap=not state.needsRestart):
            log("raye-update: WineHQ's packages replaced by raye-wine-compat")
    if command == "auto" and state.phase == "ready" and state.readyId and not state.armed:
        split = split_raye_sources(state.pending)
        if split:
            # a PC an earlier install in use left half way (0.5 to 1.0 Beta):
            # the rest must not wait for a restart someone asks for, the
            # sign-in screen may be gone. Any next start finishes it.
            log("raye-update: " + ", ".join(sorted(split))
                + " installed in part, the rest installs at the next start")
            save_run(state)
            with locked():
                if not load().armed and arm(state, "restart", 0, now) == 0:
                    save(state)
    if command == "auto" and state.phase == "ready" and state.readyId and not state.armed:
        with locked():
            if not load().armed and arm_broken(state, now):
                save(state)
    save_run(state)
    if command == "auto" and live_waiting(state):
        start_live()
    return 0 if state.phase != "error" else 1


def archive_check(args):
    """`raye-update archive-check`: say whether the Raye archive's address and
    key hang together. 0 when they do, 1 when they do not.

    RAYE_RELEASE makes it strict: a provisional address and an unpinned key
    are then failures, so an image cannot ship with either.
    """
    source = mirrors = None
    rest = list(args)
    while rest:
        flag = rest.pop(0)
        if flag == "--source" and rest:
            source = rest.pop(0)
        elif flag == "--mirrors" and rest:
            mirrors = rest.pop(0)
        else:
            print("raye-update: archive-check [--source FILE] [--mirrors FILE]",
                  file=sys.stderr)
            return 2
    release = bool(os.environ.get("RAYE_RELEASE"))
    cfg = archive_config(source, mirrors)
    problems, notes = _archive_findings(cfg, release=release)
    print(json.dumps({**cfg, "release": release, "pinned": list(ARCHIVE_FINGERPRINTS),
                      "problems": problems, "notes": notes}, indent=2))
    return 1 if problems else 0


def main(argv):
    command = argv[1] if len(argv) > 1 else "status"
    args = argv[2:]
    if command == "status":
        print(json.dumps(to_json(load()), indent=2))
        return 0
    if command == "progress":
        return progress_loop()
    if command == "channel":
        return channel_command(args)       # the status for anyone, set as root
    if command == "archive-check":
        # only reads /etc, so no root: a check that asks for a password is a
        # check nobody runs
        return archive_check(args)
    if os.geteuid() != 0:
        print("raye-update: needs root", file=sys.stderr)
        return 1
    if command == "shutdown-screen":
        return shutdown_screen()           # no lock: an "auto" run may hold it
    if command in ("check", "auto"):
        return run_auto(load(), command, time.time())   # saves through save_run
    if command == "install-live":
        return run_live(args, time.time())
    if command == "live-ended":
        return live_ended()
    if command == "arm-if-split":
        return arm_if_split(time.time())
    if command == "catch-up":
        # what raye-update-catch-up.service runs as the PC stops, before
        # the network goes: only then, never when an upgrade restarts it
        if args not in ([], ["--stopping"]):
            print("raye-update: catch-up [--stopping]", file=sys.stderr)
            return 2
        if args and not system_stopping():
            return 0
        return catch_up_before_restart(time.time())
    if command == "check-now":
        # Check for updates: pkexec waits for an admin password as long as it
        # takes, a polkit prompt behind systemctl gives up after 25 s. A
        # check already running is joined (systemd merges the start); the
        # mark makes the run a person's (check(asked=True))
        try:
            os.makedirs(os.path.dirname(CHECK_ASKED), exist_ok=True)
            with open(CHECK_ASKED, "w") as f:
                f.write(f"{time.time():.0f}\n")
        except OSError:
            pass
        run(["systemctl", "start", "--no-block", "raye-update.service"], timeout=60)
        return 0
    if command == "kernel-switch":
        kernel_switch()
        return 0
    if command == "install-now":
        # the Install button: the install runs in its own unit, not under pkexec
        start_live(asked=True)
        return 0
    with locked():
        rc = run_command(command, args, load(), time.time())
    if _RESTART_QUEUED:
        # outside the lock: the look at the lists saves as it goes
        asked = any(_RESTART_QUEUED)
        _RESTART_QUEUED.clear()
        catch_up_before_restart(time.time(), asked=asked)
        power("restart")
    return rc


# "Restart now" and the restart Raye Update decides on queue the restart
# here; main() first brings the waiting set to the newest, then restarts.
_RESTART_QUEUED = []
# a look right before a restart someone waits for: the lists in a minute,
# the newest files in four, else the set as it is downloaded installs
# (never an older file of a newer version: apt takes what its lists name)
RESTART_LISTS_TIMEOUT = 60
RESTART_DOWNLOAD_TIMEOUT = 240


def restart_soon(asked=False):
    """asked: a person pressed Restart now (a metered network does not
    stop the look for a newer version then)."""
    _RESTART_QUEUED.append(asked)


def system_stopping():
    """True while the PC shuts down or restarts."""
    rc, out, _ = run(["systemctl", "is-system-running"], timeout=30)
    return out.strip() == "stopping"


def catch_up_before_restart(now, asked=False):
    """The set armed for the next start, brought to the newest version just
    before the PC restarts (refresh_armed, with a short time limit). A
    check this recent is not read again."""
    state = load()
    if not state.armed or state.phase != "armed" or offline_owner() != "raye":
        return 0
    if now - (state.lastCheck or 0) < CATCH_UP_FRESH:
        return 0
    try:
        refresh_armed(state, now, lists_timeout=RESTART_LISTS_TIMEOUT,
                      download_timeout=RESTART_DOWNLOAD_TIMEOUT, asked=asked)
    except Exception as e:      # never in the way of the restart
        log(f"raye-update: the waiting set could not be looked at again: {e}")
    return 0


def arm_if_split(now, link=None, target=None):
    """`arm-if-split`, from raye-update's postinst: a PC an older Raye Update
    left with a Raye source installed in part (0.5 to 1.0 Beta: raye-desktop
    1.0 and raye-system 0.5, no sign-in screen) finishes at its next start,
    whatever start that is. Never waits: the install at start-up holds the
    state while it runs, and its dpkg runs this very script."""
    if offline_owner(link, target) is not None:
        return 0                    # an install at start-up is set or running
    with locked(wait=False) as mine:
        if not mine:
            return 0
        state = load()
        if not state.readyId or state.armed:
            return 0
        split = split_raye_sources(state.pending)
        if split and arm(state, "restart", 0, now, link, target) == 0:
            log("raye-update: " + ", ".join(sorted(split))
                + " installed in part, the rest installs at the next start")
            save(state)
        elif arm_broken(state, now, link, target):
            save(state)
    return 0


def run_live(args, now):
    """`install-live [auto|now]`, what raye-update-live@.service runs."""
    if args not in ([], ["auto"], ["now"]):
        print("raye-update: install-live [auto|now]", file=sys.stderr)
        return 2
    with live_lock() as held:
        if held is None:
            return 0                # one install at a time
        state = load()
        if state.phase == "installing" and not state.armed:
            state.phase = "ready" if state.readyId else "idle"   # an earlier one was cut
            clear_marks(state)
        state = install_live(state, asked=args == ["now"], now=now)
        save_run(state)
    return 0


def live_ended():
    """After raye-update-live@ stops: one that died without writing its end
    would show "Installing updates" until the next check, hours later. And
    the restart someone asked for while it ran (arm --now) happens now."""
    restart = False
    with live_lock() as held:
        if held is None:
            return 0                # another install in use runs
        with locked():
            state = load()
            if state.phase == "installing" and not state.armed:
                state.phase = "ready" if state.readyId else "idle"
                state.installPercent = 0
                clear_marks(state)
                save(state)
            if state.armed and state.armed.get("afterInstall"):
                state.armed.pop("afterInstall", None)
                if state.phase == "installing":
                    state.phase = "armed"
                save(state)
                # the PC already stops (that is what stopped the install):
                # the restart is under way
                restart = not system_stopping()
    if restart:
        log("raye-update: the updates in use are installed, restarting as asked")
        catch_up_before_restart(time.time(), asked=True)
        power("restart")
    return 0


def run_command(command, args, state, now):
    if command == "pause":
        try:
            weeks = _int(args[0] if args else None)
        except ValueError:
            weeks = 0
        if not pause(state, weeks, now):
            print(f"raye-update: pause takes 1 to {MAX_PAUSE_WEEKS} weeks", file=sys.stderr)
            return 2
        save(state)
        return 0
    if command == "resume":
        resume(state)
        save(state)
        return 0
    if command == "arm":
        try:
            then, at, right_now = parse_arm(args, now)
        except ValueError:
            print("raye-update: arm --then restart|shutdown [--at TIME] [--now]", file=sys.stderr)
            return 2
        was_in_use = state.phase == "installing" and not state.armed
        rc = arm(state, then, at, now)
        if rc == 3:
            print("raye-update: no updates are downloaded", file=sys.stderr)
            return 3
        if rc == 4:
            print("raye-update: another install is already set up for the next start",
                  file=sys.stderr)
            return 4
        if right_now and was_in_use and live_running():
            # apt holds a block inhibitor while dpkg works: a restart now is
            # refused. The end of the install in use (live-ended) restarts.
            state.armed["afterInstall"] = True
            log("raye-update: the PC restarts when the updates in use are installed")
            save(state)
            return 0
        save(state)
        if right_now:
            restart_soon(asked=True)   # the install runs at start-up, then "then" applies
        return 0
    if command == "disarm":
        disarm(state)
        save(state)
        return 0
    if command == "active-hours":
        try:
            start, end = _int(args[0]), _int(args[1])
        except (IndexError, ValueError):
            start = end = -1
        if len(args) != 2 or not valid_active_hours(start, end):
            print(f"raye-update: active-hours START END, hours 0-23, {MAX_ACTIVE_SPAN} hours at most",
                  file=sys.stderr)
            return 2
        state.activeHours = {"start": start, "end": end}
        save(state)
        return 0
    if command == "auto-restart":
        if args not in (["on"], ["off"]):
            print("raye-update: auto-restart on|off", file=sys.stderr)
            return 2
        state.autoRestart = args == ["on"]
        save(state)
        return 0
    if command == "restart-due":
        on_ac, _ = battery_level()
        why = restart_decision(state, now, time.localtime(now).tm_hour, is_gaming(), on_ac)
        if not why:
            return 0
        if why == "auto" and state.phase != "armed":
            if arm(state, "restart", 0, now) != 0:
                return 0
        if state.armed:
            state.armed["at"] = 0
        save(state)
        restart_soon()
        return 0
    if command == "install-offline":
        then = install_offline(state)
        if then == "continue":
            # one restart, as on Windows: nothing the next start reads has
            # changed, so the start-up goes on to the sign-in screen
            if not continue_boot():
                restart_after_install()
        elif then == "restart":
            restart_after_install()
        elif then:
            power(then)
        return 0
    print(f"raye-update: unknown command {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
