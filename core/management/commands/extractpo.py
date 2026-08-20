"""Collect translatable strings into the ``.po`` catalogues.

The other half of :mod:`compilepo`. Django's ``makemessages`` shells out to
GNU gettext's ``xgettext``, which is not installed here, so this walks the
templates and Python sources itself and looks for the same markers.

Merging, not overwriting, is the whole point: every translation already in the
catalogue is carried across, strings that have disappeared from the source are
dropped, and anything new arrives with an empty ``msgstr`` — which renders as
the original English rather than as a blank label, so a half-translated
catalogue is safe to ship.

Usage::

    python manage.py extractpo && python manage.py compilepo
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from .compilepo import parse_po

#: {% translate "…" %} and its older spelling, single or double quoted. The
#: `as var` form is matched too — the string still needs translating.
TEMPLATE_SIMPLE = re.compile(
    r"{%\s*(?:translate|trans)\s+"
    r"(?P<q>[\"'])(?P<text>.*?)(?<!\\)(?P=q)",
    re.S,
)

#: {% blocktranslate %}…{% endblocktranslate %}, with an optional {% plural %}.
TEMPLATE_BLOCK = re.compile(
    r"{%\s*blocktranslate(?P<args>[^%]*)%}(?P<body>.*?){%\s*endblocktranslate\s*%}",
    re.S,
)

#: One string literal, so a run of them can be matched in sequence.
STRING_LITERAL = re.compile(r"(?P<q>[\"'])(?P<text>(?:\\.|(?!(?P=q)).)*)(?P=q)", re.S)

#: _("…"), gettext("…"), gettext_lazy("…") in Python.
#:
#: The literal is captured as a *run*, because Python concatenates adjacent
#: string literals and this codebase wraps long messages across lines that way.
#: Matching only the first one produced a msgid that could never match the
#: string actually passed at runtime — a catalogue entry translated with care
#: and never once used.
#: One literal, either quoting style. Spelled out per style rather than as a
#: single character class, because "What's the work?" is a double-quoted string
#: containing a single quote — a class of "not either quote" stops dead on the
#: apostrophe and the entry never reaches the catalogue. That is a silent miss:
#: no error, just a label that stays English however carefully it was wrapped.
_LITERAL = r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""

PYTHON_SIMPLE = re.compile(
    r"(?:\b_|\bgettext|\bgettext_lazy|\bpgettext)\s*\(\s*"
    r"(?P<run>" + _LITERAL + r"(?:\s*" + _LITERAL + r")*)",
    re.S,
)


def _join_literals(run: str) -> str:
    """Concatenate a run of adjacent Python string literals, as Python does."""
    return "".join(
        match.group("text").replace('\\"', '"').replace("\\'", "'")
        for match in STRING_LITERAL.finditer(run)
    )

#: ngettext("one", "many", n) — both forms, as one plural entry.
PYTHON_PLURAL = re.compile(
    r"\bn(?:gettext|gettext_lazy)\s*\(\s*"
    r"(?P<q1>[\"'])(?P<one>.*?)(?<!\\)(?P=q1)\s*,\s*"
    r"(?P<q2>[\"'])(?P<many>.*?)(?<!\\)(?P=q2)",
    re.S,
)


def _collapse(text: str) -> str:
    """Normalise a blocktranslate body to the msgid gettext would see."""
    return " ".join(text.split())


def _blocktranslate_ids(body: str, args: str) -> list[tuple[str, str | None]]:
    """One (singular, plural) pair from a blocktranslate body."""
    if "{% plural %}" in body or "{%plural%}" in body:
        singular, _, plural = body.partition("{% plural %}")
        if not plural:
            singular, _, plural = body.partition("{%plural%}")
        return [(_collapse(singular), _collapse(plural))]
    return [(_collapse(body), None)]


def _variables_to_placeholders(text: str) -> str:
    """``{{ counter }}`` becomes ``%(counter)s``, which is what lands in the .po."""
    return re.sub(r"{{\s*([a-zA-Z_][\w.]*)\s*}}", r"%(\1)s", text)


def scan(root: Path) -> dict[tuple[str, str | None], set[str]]:
    """Every marked string under ``root``, mapped to the files it appears in."""
    found: dict[tuple[str, str | None], set[str]] = {}

    def note(key, path):
        found.setdefault(key, set()).add(str(path))

    # .txt as well as .html, and the reason is a bug this had: every email
    # template is a .txt, so scanning only .html quietly decided that forty-odd
    # translated email strings had disappeared from the source and dropped them
    # from the catalogue. The next compile then sent Greek recipients a
    # half-English email — and the failure is silent at both ends, because an
    # untranslated string renders as its English original rather than as a gap.
    for path in sorted(
        p for pattern in ("*.html", "*.txt") for p in root.rglob(pattern)
    ):
        if ".venv" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in TEMPLATE_SIMPLE.finditer(text):
            note((match.group("text"), None), path)
        for match in TEMPLATE_BLOCK.finditer(text):
            for singular, plural in _blocktranslate_ids(
                match.group("body"), match.group("args")
            ):
                note(
                    (
                        _variables_to_placeholders(singular),
                        _variables_to_placeholders(plural) if plural else None,
                    ),
                    path,
                )

    for path in sorted(root.rglob("*.py")):
        if ".venv" in path.parts or path.name in {"extractpo.py", "compilepo.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        for match in PYTHON_PLURAL.finditer(text):
            note((match.group("one"), match.group("many")), path)
        for match in PYTHON_SIMPLE.finditer(text):
            note((_join_literals(match.group("run")), None), path)

    return found


def _quote(text: str) -> str:
    body = (
        text.replace("\\", r"\\")
        .replace('"', r"\"")
        .replace("\n", r"\n")
        .replace("\t", r"\t")
    )
    return f'"{body}"'


class Command(BaseCommand):
    help = "Collect translatable strings into locale/*/LC_MESSAGES/django.po"

    def handle(self, *args, **options):
        base = Path(settings.BASE_DIR)
        found = scan(base)

        for locale_root in [Path(p) for p in settings.LOCALE_PATHS]:
            for po in sorted(locale_root.rglob("*.po")):
                existing = parse_po(po.read_text(encoding="utf-8"))
                header = existing.get("", "")

                lines = [
                    "# Translation catalogue for XTISE.",
                    "#",
                    "# Regenerate with `python manage.py extractpo`, then compile with",
                    "# `python manage.py compilepo`. Neither needs GNU gettext installed.",
                    "#",
                    "# An empty msgstr falls back to the English source, so leaving one",
                    "# blank is safe — it is a to-do, not a broken string.",
                    'msgid ""',
                    f"msgstr {_quote(header)}",
                    "",
                ]

                kept = new = 0
                for (singular, plural), files in sorted(found.items()):
                    if not singular:
                        continue
                    key = singular if plural is None else f"{singular}\0{plural}"
                    translation = existing.get(key, "")
                    if translation:
                        kept += 1
                    else:
                        new += 1

                    for source in sorted(files):
                        lines.append(f"#: {Path(source).relative_to(base).as_posix()}")
                    if plural is None:
                        lines.append(f"msgid {_quote(singular)}")
                        lines.append(f"msgstr {_quote(translation)}")
                    else:
                        forms = translation.split("\0") if translation else ["", ""]
                        forms += [""] * (2 - len(forms))
                        lines.append(f"msgid {_quote(singular)}")
                        lines.append(f"msgid_plural {_quote(plural)}")
                        lines.append(f"msgstr[0] {_quote(forms[0])}")
                        lines.append(f"msgstr[1] {_quote(forms[1])}")
                    lines.append("")

                po.write_text("\n".join(lines), encoding="utf-8")
                self.stdout.write(
                    self.style.SUCCESS(
                        f"{po.relative_to(locale_root)}: "
                        f"{kept} kept, {new} untranslated, {kept + new} total"
                    )
                )
