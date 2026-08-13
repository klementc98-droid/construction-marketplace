"""Greek, and the machinery that makes it reachable.

The catalogue is compiled by ``manage.py compilepo`` rather than Django's own
``compilemessages``, because GNU gettext is not installed here — so these tests
cover the compiler as well as the translation. A ``.mo`` that is subtly wrong
does not raise; it silently returns the English string, which looks like a
switcher that does nothing.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from core.management.commands.compilepo import parse_po, write_mo


class CatalogueTests(TestCase):
    """The Greek strings Django actually loads at runtime."""

    def test_greek_is_offered(self):
        from django.conf import settings

        self.assertIn("el", dict(settings.LANGUAGES))

    def test_a_chrome_string_is_translated(self):
        with translation.override("el"):
            self.assertEqual(translation.gettext("Jobs"), "Δουλειές")

    def test_english_is_left_alone(self):
        with translation.override("en"):
            self.assertEqual(translation.gettext("Jobs"), "Jobs")

    def test_plurals_pick_the_right_form(self):
        """Greek pluralises on n != 1, like English, but from its own catalogue."""
        with translation.override("el"):
            one = translation.ngettext(
                "%(counter)s worker.", "%(counter)s workers.", 1
            )
            many = translation.ngettext(
                "%(counter)s worker.", "%(counter)s workers.", 5
            )
        self.assertEqual(one % {"counter": 1}, "1 τεχνίτης.")
        self.assertEqual(many % {"counter": 5}, "5 τεχνίτες.")

    def test_an_untranslated_string_falls_back_to_english(self):
        """Coverage is partial on purpose; the gap must read as English, not blank."""
        with translation.override("el"):
            self.assertEqual(
                translation.gettext("Not a string in the catalogue"),
                "Not a string in the catalogue",
            )


class SwitcherTests(TestCase):
    """Choosing a language, and having it stick."""

    def test_the_switcher_is_on_the_page(self):
        response = self.client.get(reverse("jobs:list"))
        self.assertContains(response, "langswitch")
        self.assertContains(response, reverse("set_language"))

    def test_choosing_greek_renders_the_page_in_greek(self):
        self.client.post(
            reverse("set_language"), {"language": "el", "next": reverse("jobs:list")}
        )
        response = self.client.get(reverse("jobs:list"))
        self.assertContains(response, "Βρες δουλειά")
        self.assertContains(response, 'lang="el"')

    def test_the_choice_survives_the_next_request(self):
        """It lives in the session, which is what makes it a setting not a link."""
        self.client.post(reverse("set_language"), {"language": "el", "next": "/"})
        self.assertContains(self.client.get(reverse("jobs:worker_list")), 'lang="el"')

    def test_switching_back_to_english_works(self):
        self.client.post(reverse("set_language"), {"language": "el", "next": "/"})
        self.client.post(reverse("set_language"), {"language": "en", "next": "/"})
        response = self.client.get(reverse("jobs:list"))
        self.assertContains(response, "Find work")
        self.assertContains(response, 'lang="en"')

    def test_the_url_is_unchanged_by_language(self):
        """No i18n_patterns: a link someone sends still points at the same page."""
        self.client.post(reverse("set_language"), {"language": "el", "next": "/"})
        self.assertEqual(reverse("jobs:list"), "/jobs/")


class PoCompilerTests(TestCase):
    """The parser and writer, since they stand in for GNU gettext here."""

    def test_a_simple_entry_round_trips(self):
        entries = parse_po('msgid "Hello"\nmsgstr "Γεια"\n')
        self.assertEqual(entries, {"Hello": "Γεια"})

    def test_an_untranslated_entry_is_dropped(self):
        """Emitting it would map the string to "" and render a blank label."""
        self.assertEqual(parse_po('msgid "Hello"\nmsgstr ""\n'), {})

    def test_comments_are_ignored(self):
        entries = parse_po('# a note\nmsgid "Hello"\nmsgstr "Γεια"\n')
        self.assertEqual(entries, {"Hello": "Γεια"})

    def test_multi_line_strings_are_joined(self):
        entries = parse_po('msgid ""\n"one "\n"two"\nmsgstr "ένα δύο"\n')
        self.assertEqual(entries, {"one two": "ένα δύο"})

    def test_plurals_are_joined_with_nul(self):
        entries = parse_po(
            'msgid "%(n)s cat"\nmsgid_plural "%(n)s cats"\n'
            'msgstr[0] "%(n)s γάτα"\nmsgstr[1] "%(n)s γάτες"\n'
        )
        self.assertEqual(entries, {"%(n)s cat\0%(n)s cats": "%(n)s γάτα\0%(n)s γάτες"})

    def test_escapes_are_resolved(self):
        self.assertEqual(parse_po(r'msgid "a\"b"' + '\nmsgstr "x"\n'), {'a"b': "x"})

    def test_the_written_file_is_readable_by_gettext(self):
        """The end of the contract: stdlib gettext must accept what we wrote."""
        import gettext as gettext_module

        path = self.make_mo({"Hello": "Γεια", "Bye": "Αντίο"})
        with path.open("rb") as handle:
            catalogue = gettext_module.GNUTranslations(handle)
        self.assertEqual(catalogue.gettext("Hello"), "Γεια")
        self.assertEqual(catalogue.gettext("Bye"), "Αντίο")

    def test_a_string_absent_from_the_file_falls_through(self):
        import gettext as gettext_module

        path = self.make_mo({"Hello": "Γεια"})
        with path.open("rb") as handle:
            catalogue = gettext_module.GNUTranslations(handle)
        self.assertEqual(catalogue.gettext("Missing"), "Missing")

    def test_a_catalogue_without_charset_metadata_cannot_be_decoded(self):
        """Why make_mo below always writes the header entry.

        gettext takes the encoding from the "" entry's Content-Type and falls
        back to ASCII when there is none — so a catalogue missing that header
        raises on the first Greek character rather than returning mojibake.
        Our django.po declares it; this is the proof that it has to.
        """
        import gettext as gettext_module

        path = self.make_mo({"Hello": "Γεια"}, with_header=False)
        with path.open("rb") as handle:
            with self.assertRaises(UnicodeDecodeError):
                gettext_module.GNUTranslations(handle)

    def make_mo(self, entries, with_header=True):
        import tempfile
        from pathlib import Path

        if with_header:
            entries = {
                "": "Content-Type: text/plain; charset=UTF-8\n",
                **entries,
            }
        path = Path(tempfile.mkdtemp()) / "test.mo"
        write_mo(entries, path)
        return path
