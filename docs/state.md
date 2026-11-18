# Raye Update: the state file and the ways in

`/var/lib/raye-update/state.json` is the one place where Raye Update says
what it is doing. Root writes it (always by replacing the whole file), and
everyone can read it. The Start menu, the logout screen, Raye Settings and
the notifier read it; none of them writes it.

Readers watch the folder, not only the file: the file is replaced on every
write, so a watch on the old file stops firing.

## Schema version 2

```json
{
  "version": 2,
  "phase": "ready",
  "waitReason": "",
  "pending": [
    {"name": "firefox", "from": "140.0", "to": "141.0",
     "origin": "packages.mozilla.org", "kind": "app", "restart": false},
    {"name": "linux-image-7.0.0-12-generic", "from": "", "to": "7.0.0-12.12",
     "origin": "Ubuntu:26.04/resolute-security", "kind": "kernel", "restart": true}
  ],
  "count": 2,
  "downloadedBytes": 81264640,
  "totalBytes": 81264640,
  "needsRestart": true,
  "readyId": "5f0c2a9e41b7",
  "pausedUntil": 0,
  "activeHours": {"start": 8, "end": 17},
  "autoRestart": false,
  "armed": null,
  "lastCheck": 1793462520,
  "lastInstall": {"time": 1793376000, "count": 14, "firmware": 0, "ok": true},
  "kernelChange": true,
  "firmware": [
    {"device": "System Firmware", "deviceId": "6a3c...", "from": "1.07",
     "to": "1.09", "summary": "Fixes for the power button", "size": 9437184,
     "sha256": "1bf2dd35...", "location": "https://fwupd.org/downloads/..."}
  ],
  "error": null,
  "installAttempts": 0,
  "installPercent": 0,
  "checkPercent": 0,
  "bytesPerSecond": 0,
  "secondsLeft": 0,
  "brokenSources": [
    {"uri": "mirror+file:/etc/apt/mirrors/raye.list", "problem": "offline"}
  ]
}
```

Times are seconds since 1970 (UTC). A reader must ignore keys it does not
know, and must treat a missing key as its default below. A file that
cannot be read or parsed means "idle, nothing known".

| Key | Type | Default | Meaning |
|---|---|---|---|
| `version` | int | 2 | schema version |
| `phase` | string | `idle` | see the phases below |
| `waitReason` | string | `""` | while `phase` is `waiting`: `game`, `metered`, `battery` or `busy` (another install holds the package lock). With `phase` `ready`: why the updates that need no restart did not install in use yet (`battery` after Install now on a low battery) |
| `pending` | list | `[]` | updates found, one object per package: `name`, `from` (empty for a new package), `to`, `origin`, `kind` (`kernel`, `driver`, `security`, `app`, `system`), `restart` (see below). `raye-kernel` only points at a kernel and is `system`. While updates install in use, the ones of that run also carry `state`: `queued`, `installing`, `installed` or `failed`; the key is gone once the run ends |
| `count` | int | 0 | number of pending packages |
| `downloadedBytes` | int | 0 | bytes downloaded so far in this set |
| `totalBytes` | int | 0 | bytes this set needs to download (0 when everything is already downloaded). Known from the check on, before the download starts |
| `needsRestart` | bool | false | downloaded updates wait for a restart: at least one `pending` entry has `restart` true, or there is firmware. False when everything downloaded installs in use: then no reader offers "Update and restart" |
| `readyId` | string | `""` | changes each time a new set is ready; empty while nothing is downloaded |
| `pausedUntil` | number | 0 | no checking or downloading before this time; 0 = not paused |
| `activeHours` | object | `{"start": 8, "end": 17}` | local hours (0–23) when the PC never restarts on its own; `end` is excluded, a span may cross midnight, 18 hours at most |
| `autoRestart` | bool | false | the owner lets the PC restart on its own outside active hours |
| `armed` | object or null | null | `{"then": "restart" or "shutdown", "at": time}` once someone picked "Update and restart/shut down"; `at` is 0 for "at the next restart", else the scheduled time |
| `lastCheck` | number | 0 | last successful check |
| `lastInstall` | object or null | null | `time`, `count` (packages), `firmware` (devices), `ok`; after an install in use that failed, also `restartSet`: the `readyId` of what it left. That set is not tried in use again, it waits for the restart. After an install in use, `packages`: the names it installed, so a reader can still show them as done once they left `pending` |
| `kernelChange` | bool | false | the pending set changes the kernel |
| `firmware` | list | `[]` | firmware updates from fwupd: `device`, `deviceId`, `from`, `to`, `summary`, `size`, and `sha256`, `location` of the cabinet (kept in `/var/cache/raye-update/firmware/<sha256>.cab` once downloaded), `needsPower` (fwupd wants the charger plugged in; missing means false). A device that already runs `to` or newer is not listed. Firmware always installs at the next start-up |
| `error` | object or null | null | `{"code": ..., "detail": ...}`; `detail` is the tail of the tool output, for a "Details" expander only, never as the message |
| `installAttempts` | int | 0 | boots that started the install of the armed set; reset after it ends |
| `installPercent` | int | 0 | 0–100 while updates install in use (`phase` `installing`, `armed` null) |
| `checkPercent` | int | 0 | 0–100 while `phase` is `checking`: how much of the lists apt has read. Never goes back; 0 once the check ends |
| `bytesPerSecond` | int | 0 | while `phase` is `downloading`: the download speed, averaged over a few seconds |
| `secondsLeft` | int | 0 | while `phase` is `downloading`: time left at that speed. 0 means "cannot be told" (just started, or stalled), never "done" |
| `brokenSources` | list | `[]` | the places updates come from that the last check could not read, one object per repository: `uri` (as apt is configured with it) and `problem` (`offline`, `missing`, `unsigned`, `invalid`, `unknown`). Empty is the normal case. Added after the first version 2 readers shipped: a file without the key means "none known" |
| `checkedEverything` | bool | true | the last check read **every** repository apt is configured with. Derived (`brokenSources` empty and `phase` not `error`) and written anyway, so four readers do not each derive it their own way. **Never say "you're up to date" while this is false** - see below. A file without the key was written by a client that only had the all-clear case, so its absence means `true` |

### Updates that need a restart, and the ones that do not

Each `pending` entry carries `restart`: true when it only installs at the
next start-up, false when it installs while the PC is in use, as Windows
does with most of its updates. A file written before the key existed had
everything install at start-up, so a missing `restart` means true.

What needs a restart (`OFFLINE_PACKAGES` in the code): the kernel and its
modules, the boot loaders, drivers whose module is loaded (NVIDIA, DKMS),
microcode and linux-firmware; libc6 (running programs load its NSS modules
later, old and new would mix), systemd and udev, and D-Bus (a session cannot
outlive its bus); and the desktop itself: Qt 6, KDE Frameworks, Plasma,
KWin, SDDM, Mesa, and raye-theme, raye-shell, raye-style and
raye-polkit-agent. They load plugins and QML long after they started, so
new files under a running shell crash it or draw a mix. Besides the names,
anything built from one of those source packages (`OFFLINE_SOURCES`, read
with `apt-cache show`) waits too, so a library Plasma splits out later is
covered without being listed. Everything else installs in use: apps, Raye
Settings, Store and the other Raye apps, libraries the session has no
reason to reload.

The split is decided by apt, not guessed: every update that needs a
restart is pinned at its installed version, and what a plain upgrade then
takes is the set that installs in use. An update whose new version needs
one of the pinned ones is held back by apt, so it waits for the restart
too. When the pins do not hold, or apt would remove something, everything
waits for the restart.

Checked one by one for 0.6: a GRUB theme (`grub-theme-raye`) is only read at
the next start, and Raye's own QML modules (`qml6-module-org-raye-account`,
`qml6-module-org-raye-setup`) are loaded by apps, never by the shell, so
they install in use (Settings is closed and opened again when its account
module changed). The admin prompt (`raye-polkit-agent`) and the account
service (`raye-account`) install in use too, and their user service is
started again in each session right after (`try-restart`, so only where it
runs). A password prompt open at that very moment closes; the action that
asked for it can be started again. The theme, the shell and the style stay on the list: the shell and
KWin load their QML, effects and decoration long after they started.

`raye-update-live@auto.service` installs them after a download, judged as
the download is (not in a game, not on a low battery, not on a metered
network); the Install button starts `raye-update-live@now.service`, which
only waits for a low battery. Both take a pair of restore points, like the
install at start-up. If dpkg fails, what is left waits for the restart:
`lastInstall.restartSet` remembers it, and that set is not tried in use
again. On a low battery Install now does nothing and says so in
`waitReason`. When the unit stops without writing its end (killed),
`raye-update live-ended` sets `phase` back to `ready`.

### Updates apt holds back

Nothing apt holds back is an error, and none of it is offered: it is not in
`pending`, it never makes a restart, and it comes in the day it can.

- **Phased updates.** Ubuntu gives a new version to a share of the machines
  first, a little more each day, and draws each source package on its own,
  so a Plasma point release would reach a PC a few packages at a time over
  a week, with two versions of the desktop mixed in between. Every apt run
  of Raye Update passes `APT::Get::Never-Include-Phased-Updates=true`: a
  phased update waits until Ubuntu gives it to everyone, then comes in with
  the rest of its release, in one set. Security updates are never phased.
- **Kept back.** An update whose new version needs a newer build of a
  package Raye rebuilds and pins (Plasma, see `raye-pins.pref`) is kept back
  by `apt-get upgrade` until Raye publishes that build. Same for a package a
  person put on hold (`apt-mark hold`).
- **Files that did not download.** A mirror still syncing answers 404 for a
  file its lists already name, and the archive keeps only the newest update,
  so a version can vanish between the check and the download. The download
  goes on with the other files; the set that is ready is what the
  downloaded files can install (`apt-get -s --no-download --fix-missing`),
  the rest is found again at the next check. Only a download where nothing
  usable arrived is an error.
- **What turned up after the download.** Both installs take only what is
  downloaded (`--no-download --fix-missing`): a package the lists offer
  since (a phase that reached everyone, a newer version) waits for the next
  round. Without `--fix-missing` apt refuses the whole install as soon as
  one file is missing.

`tests/test_real_apt.py` checks the four against the real apt.

### Phases

| `phase` | What it means | What a reader shows |
|---|---|---|
| `idle` | nothing to install (`count` 0); with `count` above 0, updates were found by `check` and not downloaded yet | "You're up to date" **only if `checkedEverything`** / "Updates available" |
| `checking` | reading the list of updates, `checkPercent` done | "Checking for updates…" |
| `waiting` | updates found, waiting for a better moment (`waitReason`) | "Updates are waiting" + reason |
| `downloading` | downloading, `downloadedBytes` of `totalBytes`, `bytesPerSecond`, `secondsLeft` | "Downloading – 45 % · 4 MB/s · about 2 minutes left" |
| `ready` | downloaded; waiting for a restart when `needsRestart`, else for the install in use | "Restart required" / "Ready to install" |
| `armed` | someone picked "Update and restart/shut down" | "Your PC will update when it restarts" |
| `installing` | with `armed` set: the install at start-up (seen only from a recovery shell); with `armed` null: updates installing in use, `installPercent` done | "Installing – 40 %" |
| `paused` | paused until `pausedUntil` | "Updates paused until <date>" |
| `error` | the last run failed, `error.code` says why | message for the code |

Error codes and the words for them (Settings and the notifier own the
final wording, these are the plain ones):

| `error.code` | English | French |
|---|---|---|
| `no-internet` | We couldn't connect to the update service. Check your internet connection. | Impossible de joindre le service de mises à jour. Vérifiez votre connexion internet. |
| `server` | The update service didn't answer. We'll try again later. | Le service de mises à jour n'a pas répondu. Nouvel essai plus tard. |
| `no-space` | There isn't enough free space to update. Free up some space and try again. | Il n'y a pas assez d'espace libre pour la mise à jour. Libérez de l'espace et réessayez. |
| `broken` | Some updates can't be installed together. We'll try again later. | Certaines mises à jour ne peuvent pas être installées ensemble. Nouvel essai plus tard. |
| `install-failed` | Updates couldn't be installed. Your PC was not changed. | Les mises à jour n'ont pas pu être installées. Votre PC n'a pas été modifié. |
| `firmware-failed` | A firmware update couldn't be installed. | Une mise à jour du microprogramme n'a pas pu être installée. |
| `unknown` | Something went wrong. We'll try again later. | Une erreur s'est produite. Nouvel essai plus tard. |

### A place updates come from that cannot be read

`brokenSources` is not an error. One repository being unreadable must never
stop the others: apt keeps and uses the indexes it does have, so Ubuntu's
security updates still come through while Raye's own archive is down. So a
non-empty `brokenSources` comes **with** a normal `phase` and a fresh
`lastCheck`, and the updates in `pending` are real and installable.

A reader shows the updates as usual, and adds one quiet line next to them - 
never a blocking dialog, never instead of the updates:

**And it must not say "You're up to date".** With the Raye archive
unreadable and Ubuntu having nothing to offer, `phase` is `idle` and `count`
is 0 - byte for byte what a PC that really is up to date looks like. Saying
"up to date" there turns a statement about what we managed to see into a
statement about the machine. `checkedEverything` is `false` in exactly that
case; the words to use instead are "We couldn't check everything" /
« Nous n'avons pas pu tout vérifier », followed by the quiet line below.
This is the same family as the whole-machine case further down, one notch
less severe: there, nothing at all was read and it is an error; here, part
was read and the part that was is real.

| `problem` | What happened | English | French |
|---|---|---|---|
| `offline` | the name does not resolve, or nothing answers | One place updates come from couldn't be reached. The rest were checked. | Une source de mises à jour n'a pas pu être joignable. Les autres ont été vérifiées. |
| `missing` | the server answers, the index is not there - a 404, or no Release file at all (an archive that is not published yet) | One place updates come from has nothing to offer right now. The rest were checked. | Une source de mises à jour n'a rien à proposer pour l'instant. Les autres ont été vérifiées. |
| `unsigned` | it answered, and what it served could not be trusted: no signature, a signature that does not check, one that **stopped** being there, or an index with only weak checksums | One place updates come from couldn't be trusted, so it was skipped. | Une source de mises à jour n'a pas pu être authentifiée ; elle a été ignorée. |
| `invalid` | the index is damaged or out of date | One place updates come from sent something unusable. The rest were checked. | Une source de mises à jour a renvoyé des données inutilisables. Les autres ont été vérifiées. |
| `unknown` | apt words it in a way we do not know yet | One place updates come from couldn't be read. The rest were checked. | Une source de mises à jour n'a pas pu être lue. Les autres ont été vérifiées. |

When **every** repository failed, it is a different thing entirely and it
is an error, not a quiet line: `phase` is `error`, the code is usually
`no-internet`, **`lastCheck` is not stamped**, and `brokenSources` names
them all. This matters because apt exits 0 in that case, and the simulation
then answers "0 upgraded" - a reader that trusted either would tell someone
whose PC reached nothing at all that they were up to date.

Only `uri` is written, never a friendly name: the words for a repository
belong to the reader, not to this file. `uri` is the URI apt is configured
with (`mirror+file:/etc/apt/mirrors/raye.list` for Raye's own archive, not
the mirror apt happened to pick behind it).

apt 3.0.3 has **five** ways of saying "I refused this repository", and
they do not all mention a signature:

```
The repository '…' is not signed.                        -> unsigned
The repository '…' is no longer signed.                  -> unsigned
The repository '…' provides only weak security information. -> unsigned
The repository '…' does not have a Release file.         -> missing
The repository '…' no longer has a Release file.         -> missing
```

Reading only the first left the other four **nameless**, and a nameless
failure made the whole check an error that threw away the Ubuntu security
updates apt had already read. The fourth is the one an unpublished archive
prints, so it was the case every beta tester would meet first. `strings` on
libapt re-derives the list, and a test asks apt for it so a sixth message
cannot slip in - see `tests/test_sources.py#REPO_MESSAGES`.

### Keys of version 1, still written

Until every reader has moved to version 2 (Raye Settings' update page and
the notifier), the file also carries the old keys, computed from the new
ones. They will be removed in a later version; do not use them in new code.

| Old key | Same as |
|---|---|
| `state` | `phase`, with `armed` and `installing` written as `ready` |
| `updates` | `count` |
| `packages` | the `name` of each `pending` entry |
| `reason` | `waitReason` (in `waiting`, or in `ready` when it is set), or `error.detail` in `error` |
| `checked` | `lastCheck` |
| `ready_id` | `readyId` |
| `paused_until` | `pausedUntil` |

## The ways in

Everything that changes the state goes through
`/usr/libexec/raye-update`, run as root. From a session, call it through
`pkexec`: the polkit action `org.raye.update` lets the administrators of
the computer do it without a password while they sit at it, and asks
for an administrator's password otherwise.

| Command | Who calls it | What it does |
|---|---|---|
| `status` | anyone, no root | prints the state |
| `archive-check` | anyone, no root; the image build with `RAYE_RELEASE=1` | says whether the Raye archive's address and key hang together - [docs/archive.md](archive.md) |
| `arm --then restart` | Start menu, logout screen, Settings, notifier | arms the downloaded set; it installs at the next start-up, then the PC restarts |
| `arm --then shutdown` | same | same, then the PC turns off |
| `arm --then restart --at TIME` | notifier "Pick a time", Settings | arms and restarts at TIME (within 7 days) |
| `arm … --now` | scripts without a session | arms and restarts right away |
| `disarm` | logout screen "Cancel", Settings | undoes `arm` |
| `pause N` / `resume` | Settings | pause for N weeks (1 to 5), end a pause |
| `active-hours START END` | Settings | sets `activeHours` (hours 0–23, 18 hours at most) |
| `auto-restart on\|off` | Settings | sets `autoRestart` |
| `auto`, `check` | the timer, `raye-update.service` | check (and download) |
| `restart-due` | `raye-update-restart.timer` | restarts when a picked time is reached, or outside active hours when `autoRestart` is on |
| `install-offline` | `raye-update-install.service` at start-up | installs the armed set |
| `install-now` | Settings "Install" (through pkexec) | starts `raye-update-live@now.service` |
| `check-now` | Settings "Check for updates" (through pkexec) | starts `raye-update.service` |
| `install-live auto\|now` | `raye-update-live@.service` | installs in use what needs no restart |

`arm` only arms; it does not restart. A session then asks Plasma to
restart (so apps get to save their work): after `arm --then shutdown` the
session **restarts** too, the install runs at start-up, and the PC turns
off at the end. `arm` exits with 3 when nothing is downloaded, and with 4
when another tool already armed an install at start-up.

`raye-update` (in `/usr/bin`) is the same program for a terminal: it
runs the root commands through `pkexec`.

## The install at start-up

`arm` points `/system-update` at `/var/lib/raye-update/offline`, so the
next boot goes to `system-update.target` and runs
`raye-update-install.service` (systemd's offline updates). The service:

1. tells Plymouth `change-mode --updates`, and reports
   `system-update --progress=N` while dpkg runs; when firmware follows,
   apt counts for 0–90 % and firmware for 90–100 % in
   `change-mode --firmware-upgrade`;
2. sends `display-message --text=raye:finishing-updates` when an earlier
   install was cut (power loss) and is being finished; the theme turns it
   into "Finishing updates";
3. never prints apt's text: everything goes to the journal;
4. runs `dpkg --configure -a` first, retries once after a failure, and
   keeps `/system-update` for the next boot (three boots at most) while
   dpkg is left half configured;
5. installs firmware from the cabinets fetched with the packages (there
   is no network at start-up) with `fwupdmgr local-install`; a cabinet
   that is missing or does not match its checksum is skipped, the TPM is
   not touched for it, and the error says `firmware-failed`. It runs
   `raye-tpm prepare-firmware` first and skips the firmware when that
   fails (the disk would ask for its password otherwise);
6. writes `lastInstall`, clears the set and restarts or turns the PC off.

## Taskbar progress

While `phase` is `downloading`, the user unit `raye-update-progress`
(started by a path unit on the state file) sends the Unity launcher entry
signal for `application://org.raye.settings.desktop` with `progress`
= `downloadedBytes / totalBytes`, and hides it when the download ends.
