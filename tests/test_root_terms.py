"""Raye Update says root and sudo, never administrator, in what it shows:
notifications, the polkit prompt, unit descriptions. Comments may keep it."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
OLD = re.compile(r"administrat|\badmin\b", re.I)
COMMENT = re.compile(r"^\s*(#|//|<!--)")


def test_nothing_shown_says_administrator():
    bad = []
    for folder in ("src", "data"):
        for path in sorted((ROOT / folder).iterdir()):
            if not path.is_file():
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                # auth_admin_keep is polkit's own keyword
                if OLD.search(line.replace("auth_admin", "")) and not COMMENT.match(line):
                    bad.append(f"{path.relative_to(ROOT)}:{number}")
    assert bad == []
