"""Compile every ``.po`` under LOCALE_PATHS into the ``.mo`` Django reads.

This is what ``compilemessages`` does, minus the dependency. That command
shells out to GNU gettext's ``msgfmt``, which is not installed on Windows by
default and is a genuine obstacle — the alternative is asking every person who
clones this repo to install a toolchain before the site will render in Greek.

The format is small enough to write directly: a header, two tables of
``(length, offset)`` pairs, and the strings. Only what Django's own
``gettext.GNUTranslations`` reads back is emitted, so the hash table is left
empty — it is an optional lookup accelerator, and catalogues this size are
found by binary search over the sorted table either way.

Usage::

    python manage.py compilepo
"""

from __future__ import annotations

import array
import struct
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

#: Identifies the file as a little-endian .mo. Read by gettext to detect
#: byte order, which is why it is written unswapped rather than as bytes.
MAGIC = 0x950412DE


def _unquote(line: str) -> str:
    """Take the text out of one quoted .po line, resolving its escapes."""
    body = line.strip()[1:-1]
    return (
        body.replace(r"\\", "\x00")          # park real backslashes
        .replace(r"\n", "\n")
        .replace(r"\t", "\t")
        .replace(r"\"", '"')
        .replace("\x00", "\\")
    )


def parse_po(text: str) -> dict[str, str]:
    """``.po`` source to ``{original: translation}``.

    Plurals are joined with NUL on both sides, which is how the format stores
    them: the original becomes ``singular\\0plural`` and the translation
    ``form0\\0form1``. Untranslated entries — an empty msgstr — are dropped, so
    they fall back to the source string rather than rendering as blank.
    """
    entries: dict[str, str] = {}
    key = plural_key = None
    forms: dict[int, str] = {}
    field = None

    def flush() -> None:
        if key is None:
            return
        translations = [forms.get(i, "") for i in range(max(forms) + 1)] if forms else []
        if not any(translations):
            return
        original = key if plural_key is None else f"{key}\0{plural_key}"
        entries[original] = "\0".join(translations)

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("msgid_plural"):
            field, plural_key = "plural", _unquote(line[len("msgid_plural"):])
        elif line.startswith("msgid"):
            flush()
            key, plural_key, forms = _unquote(line[len("msgid"):]), None, {}
            field = "id"
        elif line.startswith("msgstr["):
            index = int(line[line.index("[") + 1: line.index("]")])
            forms[index] = _unquote(line[line.index("]") + 1:])
            field = index
        elif line.startswith("msgstr"):
            forms = {0: _unquote(line[len("msgstr"):])}
            field = 0
        elif line.startswith('"'):
            # A continuation of whichever field we are in — .po wraps long
            # strings across lines and they concatenate with nothing between.
            piece = _unquote(line)
            if field == "id":
                key += piece
            elif field == "plural":
                plural_key += piece
            elif isinstance(field, int):
                forms[field] = forms.get(field, "") + piece

    flush()
    return entries


def write_mo(entries: dict[str, str], path: Path) -> None:
    """Serialise ``entries`` to ``path`` in .mo binary format."""
    # Sorted because the reader binary-searches this table.
    items = sorted(entries.items())
    originals = b"\x00".join(k.encode("utf-8") for k, _ in items)
    translations = b"\x00".join(v.encode("utf-8") for _, v in items)

    count = len(items)
    header = 7 * 4
    original_table = header
    translation_table = original_table + count * 8
    data_start = translation_table + count * 8

    offsets, cursor = [], data_start
    for key, _value in items:
        length = len(key.encode("utf-8"))
        offsets.append((length, cursor))
        cursor += length + 1                     # +1 for the NUL separator

    translation_start = cursor
    cursor = translation_start
    for _key, value in items:
        length = len(value.encode("utf-8"))
        offsets.append((length, cursor))
        cursor += length + 1

    out = struct.pack(
        "<7I", MAGIC, 0, count, original_table, translation_table, 0, data_start
    )
    out += array.array("i", [n for pair in offsets for n in pair]).tobytes()
    out += originals + b"\x00"
    out += translations + b"\x00"

    path.write_bytes(out)


class Command(BaseCommand):
    help = "Compile .po catalogues to .mo without requiring GNU gettext."

    def handle(self, *args, **options):
        roots = [Path(p) for p in settings.LOCALE_PATHS]
        found = 0
        for root in roots:
            for po in sorted(root.rglob("*.po")):
                entries = parse_po(po.read_text(encoding="utf-8"))
                mo = po.with_suffix(".mo")
                write_mo(entries, mo)
                found += 1
                # ASCII only. A Windows console on a non-UTF-8 codepage — cp1253
                # on a Greek machine, which is exactly who runs this — raises
                # UnicodeEncodeError on a stray arrow and fails the command
                # after the file has already been written correctly.
                self.stdout.write(
                    self.style.SUCCESS(
                        f"{po.relative_to(root)} -> {mo.name} ({len(entries)} strings)"
                    )
                )
        if not found:
            self.stdout.write(self.style.WARNING("No .po files found."))
