"""Turning a queued row into an actual email.

Rendering lives here rather than in the model so that the model stays a record
of what happened and this stays a description of how it is said — the two
change for different reasons and at different times.
"""

from __future__ import annotations

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import translation
# Lazy, and it matters: SUBJECTS is built once at import, long before anybody
# knows who is reading. Eager gettext would freeze every subject line in
# whatever language the server started in — which is how a body arrives in
# Greek under an English subject.
from django.utils.translation import gettext_lazy as _

from .models import Kind

#: The subject line per kind. Held here rather than in each template because a
#: subject is the one line that decides whether the rest is read, and eleven of
#: them in one list is the only way to see that they are consistent.
SUBJECTS = {
    Kind.MESSAGE: _("New message about %(job)s"),
    Kind.JOB_POSTED: _("New work posted: %(job)s"),
    # The one subject with no job in it. A nudge is about a list, not a thing.
    Kind.REMINDER: _("You've got something waiting"),
    Kind.TOMORROW: _("Tomorrow: %(job)s"),
    Kind.OFFER_RECEIVED: _("You've been offered work: %(job)s"),
    Kind.OFFER_ANSWERED: _("Your offer was answered: %(job)s"),
    Kind.APPLICATION: _("Somebody applied for %(job)s"),
    Kind.SELECTED: _("You got the job: %(job)s"),
    Kind.WORK_FINISHED: _("Work marked finished: %(job)s"),
    Kind.JOB_CLOSED: _("Job closed: %(job)s"),
    Kind.ESCROW_FUNDED: _("The money is held for %(job)s"),
    Kind.PAYMENT_RELEASED: _("You've been paid for %(job)s"),
    Kind.DISPUTE: _("A dispute was raised on %(job)s"),
    Kind.RATING: _("You were rated for %(job)s"),
}


def _language_for(user) -> str:
    """The recipient's language, never the sender's.

    A Greek worker gets Greek when an English client pressed the button. The
    session that caused the event belongs to the wrong person entirely, and the
    command that sends this has no session at all.
    """
    return user.language or settings.LANGUAGE_CODE


def build(notification) -> EmailMultiAlternatives:
    """The email for one queued row, in the recipient's language."""
    job = notification.job
    payload = notification.payload or {}

    with translation.override(_language_for(notification.recipient)):
        title = payload.get("job_title") or (job.title if job else _("your job"))
        subject = str(SUBJECTS.get(notification.kind, _("Update on %(job)s"))) % {
            "job": title
        }

        context = {
            "notification": notification,
            "job": job,
            "recipient": notification.recipient,
            "title": title,
            "site_url": settings.SITE_URL,
            # Every email ends in one link back to the thing it is about, and
            # this is where that link is decided — a template guessing at a URL
            # is how people end up mailed a 404.
            "link": settings.SITE_URL + (payload.get("path") or _default_path(job)),
            **payload,
        }

        text = render_to_string(
            f"notifications/email/{notification.kind}.txt", context
        )
        body = render_to_string("notifications/email/_wrapper.txt", {
            **context, "body": text.strip(),
        })

    message = EmailMultiAlternatives(
        subject=subject.strip(),
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[notification.recipient.email],
    )
    return message


def _default_path(job) -> str:
    if job is None:
        return "/"
    return job.get_absolute_url()
