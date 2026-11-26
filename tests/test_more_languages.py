# SPDX-License-Identifier: GPL-3.0-or-later
"""Raye Update speaks Spanish, German, Brazilian Portuguese and Italian
too: the console words of the install at start-up, the Raye kernel
step, and the notification that updates are ready."""
import re
import string

import pytest

from conftest import ru
from test_notify import run_notify

LANGUAGES = ["fr", "es", "de", "pt", "it"]


def fields(text):
    return sorted(f for _, f, _, _ in string.Formatter().parse(text) if f)


@pytest.mark.parametrize("table", ["TEXT_WORDS", "KERNEL_WORDS"])
@pytest.mark.parametrize("lang", LANGUAGES)
def test_every_message_in_every_language(table, lang):
    words = getattr(ru, table)
    assert lang in words, f"{table} has no {lang}"
    assert words[lang].keys() == words["en"].keys()
    for key, english in words["en"].items():
        text = words[lang][key]
        assert fields(text) == fields(english), f"{table}[{lang}][{key}] placeholders"
        assert text != english


@pytest.mark.parametrize("line, lang", [('LANG="es_ES.UTF-8"', "es"), ("LANG=de_DE.UTF-8", "de"),
                                        ('LANG="pt_BR.UTF-8"', "pt"), ("LANG=it_IT.UTF-8", "it"),
                                        ("LANG=ja_JP.UTF-8", "en")])
def test_the_console_follows_the_system_language(tmp_path, line, lang):
    f = tmp_path / "locale"
    f.write_text(line + "\n")
    assert ru.system_language([str(f)]) == lang


READY_1 = {"state": "ready", "ready_id": "m1", "updates": 1}
READY_3 = {"state": "ready", "ready_id": "m3", "updates": 3}
EXPECTED = {
    "es_ES.UTF-8": ("1 actualización descargada", "3 actualizaciones descargadas", "Reiniciar ahora"),
    "de_DE.UTF-8": ("1 Update heruntergeladen", "3 Updates heruntergeladen", "Jetzt neu starten"),
    "pt_BR.UTF-8": ("1 atualização baixada", "3 atualizações baixadas", "Reiniciar agora"),
    "it_IT.UTF-8": ("1 aggiornamento scaricato", "3 aggiornamenti scaricati", "Riavvia ora"),
}


@pytest.mark.parametrize("lang", sorted(EXPECTED))
def test_the_notification_speaks_the_language(tmp_path, lang):
    one, several, button = EXPECTED[lang]
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    out = run_notify(tmp_path / "a", READY_1, lang=lang)
    assert one in out and button in out
    assert "Restart now" not in out
    out = run_notify(tmp_path / "b", READY_3, lang=lang)
    assert several in out


def test_the_session_language_wins_over_the_formats(tmp_path):
    # Settings writes the display language to LANGUAGE ("de:en_US"); LANG
    # is the formats, which can be another country's
    out = run_notify(tmp_path, READY_1, lang="fr_FR.UTF-8", language="de:en_US")
    assert "1 Update heruntergeladen" in out
