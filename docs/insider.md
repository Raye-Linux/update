# Raye Insider — update channels

| Channel | apt suite | Who |
|---|---|---|
| Stable (default) | `resolute` (in `raye.sources`) | everyone |
| Public Beta | `resolute-public-beta` | anyone who turns it on |
| Beta | `resolute-beta` | Raye accounts accepted as Beta testers |
| Dev | `resolute-dev` | Raye accounts accepted as developers |

Stable is the archive's own source, `/etc/apt/sources.list.d/raye.sources`
(a conffile of raye-archive-keyring). Raye Update never writes it. Any other
channel is one more suite of the same archive, written by Raye Update to
`/etc/apt/sources.list.d/raye-insider.sources` with the same mirrors and the
same key. Stable stays listed, so apt takes the newest version of each package
from either suite.

    raye-update channel              # JSON, no password
    raye-update channel set beta     # as an administrator, then checks for updates
    raye-update channel set stable   # leave: removes the file

Leaving never downgrades: what came from a channel stays installed until
Stable has the same or a newer version, and then updates from Stable as usual.

Access to Beta and Dev is decided by Settings from the Raye account (badges
`beta-tester` and `developer`). The suites themselves sit on the same public
mirrors as Stable: the channel choice is a convenience, not a secret.

A test tree can stand in for the mirrors, as root only:
`RAYE_INSIDER_URIS=file:/path RAYE_INSIDER_KEYRING=/path/key.gpg raye-update channel set dev`.
