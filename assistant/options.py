"""The starter questions offered under the opening message.

Typing is the thing this app's users are worst served by. Many are on a phone,
on a site, in gloves, and some are reading in a second language — so somebody
who does not know what to ask can get started with one tap instead.

This module used to also build tappable answers for the form-filling branch,
derived from the Django form fields themselves. That branch is gone: forms are
filled in on the form now, one question per screen, where a choice is a real
radio button rather than a chat message that has to be parsed back into one.
"""

from __future__ import annotations

from django.utils.translation import gettext as _

#: Questions people actually arrive with. The first one is deliberately the
#: question this whole product turns on — somebody who has never done the work
#: is asking whether they are allowed here at all, and the answer is yes.
#: Kept short enough to fit a phone button.
QA_STARTERS = (
    _("Can I work here with no experience?"),
    _("How do I get paid?"),
    _("What's the platform fee?"),
    _("What is escrow?"),
    _("How does check-in work?"),
    _("How do I post a job?"),
)


def qa_options() -> list[dict[str, str]]:
    return [{"value": str(question), "label": str(question)} for question in QA_STARTERS]
