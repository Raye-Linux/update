# Raye Update

System updates for Raye Linux. They are looked for in the background,
downloaded on their own, and installed when you restart, the way Windows
does it. Firmware updates come from fwupd.

- `src/` the service and its command line tool
- `data/` systemd units and settings
- `docs/` how the state file and the archive work
- `tests/` the tests

## Test

    python3 -m pytest tests

To build the package: `dpkg-buildpackage -b -us -uc`.

## License

GPL-3.0-or-later (`LICENSE`).

## Links

- Website: https://rayelinux.com
- Discord: https://discord.gg/TrxhUkabyB (questions, bug reports, news)
