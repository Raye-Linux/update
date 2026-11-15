# The Raye archive: what is left to wire, and where

Raye's own packages - `raye-store`, `raye-settings`, the theme - are
updated from Raye's own apt archive. **That archive is not published yet.**
It needs a signing key, which the owner makes and keeps, and somewhere to
live. Both are his decisions, and neither is taken in this repository.

What this file says is where the two values go, so that the day they exist
the wiring is one line and one file, and the tests say whether it worked.

## Measured, 2026-11-06

```
dl.rayelinux.com                       does not resolve
raye-linux.github.io/packages          404
```

So every beta tester is on the path where the archive cannot be read. That
is the normal state for the whole beta, and it must stay quiet: the Ubuntu
security updates still arrive, the Raye archive is named in `brokenSources`
with `problem: missing`, and `checkedEverything` is `false` so nobody is
told they are up to date when their Raye updates were never looked at.

## The three places a value goes

| What | Where | Owner |
|---|---|---|
| the addresses | `iso/packages/raye-base/release/apt/raye-mirrors.list` | the owner |
| the signing key | `iso/packages/raye-base/keyring/raye-archive-keyring.gpg` | the owner |
| the key's **fingerprint** | `ARCHIVE_FINGERPRINTS` in `src/raye_update.py` | this repository |

Only the third one lives here, and it is **empty on purpose**:

```python
ARCHIVE_FINGERPRINTS = ()
```

An empty pin means nothing in this repository can notice a keyring that was
swapped. That is honest while no key exists, and unacceptable in an image,
so release mode refuses it - see below.

## The day the key exists

1. The owner creates the key and keeps the private half **off every
   machine of the lab**. Nothing here ever needs the private half.
2. Its public half replaces
   `iso/packages/raye-base/keyring/raye-archive-keyring.gpg`.
3. Its fingerprint goes on the one line above:

   ```python
   # the shape, not a value: 40 hex characters of HIS key
   ARCHIVE_FINGERPRINTS = ("0123456789ABCDEF0123456789ABCDEF01234567",)
   ```

   40 hex characters, nothing shorter: a short key id can be collided on,
   and a pin that notices half a swap is not a pin. `archive-check` then
   refuses any keyring that does not carry it.
4. The real addresses replace the mirror list.
5. `raye-update archive-check` must answer `"problems": []` **in release
   mode**:

   ```sh
   RAYE_RELEASE=1 raye-update archive-check
   ```

A tuple is deliberate: `ARCHIVE_FINGERPRINTS = "EA73…"` - a bare string - 
would iterate as characters and pin nothing at all, so a test refuses
anything but a tuple.

## What `archive-check` answers

It reads `/etc/apt/sources.list.d/raye.sources` and the mirror list, and
needs no root - a check that asks for a password is a check nobody runs.
Exit 0 when the configuration hangs together, 1 when it does not.

| `problems` | What is wrong |
|---|---|
| `no-source` | no Raye source is installed at all |
| `no-address` | a key, and nothing to fetch with it |
| `insecure-address` | a mirror on plain `http://`. A signature makes that survivable, not acceptable: these packages become root on the machine |
| `provisional-address` | a stand-in host (`.example`, `.invalid`, `.test`, `.local`, `localhost`, a bare IP). A beta that updates from one of these updates from nowhere |
| `no-key` | an address, and no `Signed-By`. apt would fetch it and refuse every index of it, for ever |
| `key-missing` | `Signed-By` names a file that is not installed |
| `key-unreadable` | the keyring is there and holds no key |
| `bad-fingerprint` | the pin in this repository is not 40 hex characters |
| `wrong-key` | the installed keyring is not the pinned one |
| `key-not-pinned` | nothing is pinned. A **note** on a PC, a **problem** in release mode |

`notes` is for what could **not** be checked, and is never counted as a
pass: `key-not-read` means gpg is not installed, so the keyring holds a key
this tool could not look at. On a PC that is fine - apt verifies the
signature itself whatever this tool managed to ask - but it is said out
loud rather than passed over. In release mode the pin could not be enforced
either way, so it comes back as the problem `key-not-pinned`: the machine
that builds an image has gpg, and not being able to ask there is a fault of
the build, not of the PC.

A keyring may hold **several** primary keys, and during a key rotation it
should: the old key and the new one side by side, so that neither the PCs
that have updated nor the ones that have not are locked out. The pin
matches if it names any of them, and subkeys are not offered as a match.

## Why these three incoherences and not others

They are the ones that end in **silence**. Measured on apt 3.0.3, an
address with no key, a key with no address, and a key the archive is not
signed with all come out of `apt-get update` as

```
E: The repository '…' is not signed.
```

exit 100, and nothing else. The archive is then skipped for ever, every
Raye fix with it, and the only trace is one quiet line next to updates that
look complete. `archive-check` exists so that the question is asked on
purpose instead of being answered by accident.

## What is deliberately NOT here

- **No address is chosen.** Not a default, not a placeholder, not a
  "temporary" one. A stand-in address in a shipped image is a machine that
  takes its updates from somewhere nobody meant, so release mode refuses
  every host that looks like one.
- **No key is made.** Not even for a test. A key made for an example is
  taken for a real one sooner or later. The one test that needs a keyring
  creates it in a temporary directory and destroys it with that directory.
- **No signature is verified here.** apt does that, and it is the only
  thing that should: it has `sqv`, the keyring and the policy. This tool
  reports; it never decides to trust.
