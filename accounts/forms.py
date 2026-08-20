"""Profile and role-selection forms."""

from __future__ import annotations

from datetime import date, datetime

from django import forms
from django.utils.translation import gettext_lazy as _
from django.utils import timezone

from config import business_rules as rules
from core.models import Region, Trade
from core.steps import StepsMixin
from core.dates import date_picker_attrs, parse_date_list

from .models import (
    AvailabilityDate,
    AvailabilityStatus,
    ClientProfile,
    PortfolioPhoto,
    User,
    WorkerProfile,
)


class AccountDetailsForm(forms.ModelForm):
    """Who you are, independent of what you do here.

    Deliberately separate from the two profile forms: a person who is both a
    worker and a client has one name and one face, and being asked for them
    twice — once per role — is the kind of thing that makes an account feel
    like paperwork.
    """

    class Meta:
        model = User
        fields = [
            "full_name", "avatar", "headline", "phone", "date_of_birth",
            "email_notifications",
        ]
        labels = {
            "full_name": _("Name"),
            "avatar": _("Profile photo"),
            "headline": _("One-line intro"),
            "phone": _("Phone"),
            "date_of_birth": _("Date of birth"),
            "email_notifications": _("Email me about my jobs"),
        }
        help_texts = {
            "full_name": _("How you appear to everyone else on the platform."),
            "avatar": _("A clear photo of your face. Square works best."),
            "headline": _("Shown under your name — e.g. “Framing and finish carpentry, own tools”."),
            "phone": _("Only shown to people you're working with on a job."),
            "date_of_birth": _("Only your age is ever shown, never the date."),
            "email_notifications": _(
                "Offers, applications, messages and anything waiting on your "
                "answer. Never marketing, and one switch turns off the lot."
            ),
        }
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}),
            "headline": forms.TextInput(
                attrs={"placeholder": _("Framing and finish carpentry, own tools")}
            ),
            "phone": forms.TextInput(attrs={"placeholder": _("+1 555 010 0199")}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Bound in the widget as well as in clean_date_of_birth below. The
        # picker refuses to offer a date nobody can have been born on, and the
        # validator still catches anything typed or posted directly — the
        # attribute is a convenience, never the check.
        today = date.today()
        dob = self.fields["date_of_birth"].widget.attrs
        dob["max"] = today.replace(
            year=today.year - rules.MINIMUM_WORKING_AGE
        ).isoformat()
        dob["min"] = today.replace(year=today.year - 120).isoformat()

    def clean_full_name(self) -> str:
        name = (self.cleaned_data.get("full_name") or "").strip()
        if not name:
            raise forms.ValidationError(
                "People need a name to put to the profile before they hire you."
            )
        return name

    def clean_date_of_birth(self):
        dob = self.cleaned_data.get("date_of_birth")
        if dob is None:
            return dob
        today = date.today()
        age = today.year - dob.year - (
            (today.month, today.day) < (dob.month, dob.day)
        )
        if age < rules.MINIMUM_WORKING_AGE:
            raise forms.ValidationError(
                f"You have to be at least {rules.MINIMUM_WORKING_AGE} to use this."
            )
        if age > 120:
            raise forms.ValidationError("Check that date — it doesn't look right.")
        return dob

    def clean_avatar(self):
        avatar = self.cleaned_data.get("avatar")
        limit = rules.MAX_AVATAR_SIZE_MB * 1024 * 1024
        if avatar and hasattr(avatar, "size") and avatar.size > limit:
            raise forms.ValidationError(
                f"Photo must be under {rules.MAX_AVATAR_SIZE_MB} MB."
            )
        return avatar


class RoleSelectionForm(forms.Form):
    """Asked once, immediately after the first Google sign-in.

    Multi-select rather than radio: a user who is both should be able to say
    so at the door instead of discovering later that they picked wrong and
    have to start a second account.
    """

    ROLE_CHOICES = (
        ("worker", "I'm looking for work"),
        ("client", "I'm hiring"),
    )

    roles = forms.MultipleChoiceField(
        choices=ROLE_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        label=_("How will you use the platform?"),
        help_text=_("You can pick both, and you can add the other one later."),
    )


class _RegionMixin(forms.ModelForm):
    """Default the region field to the single active launch market.

    The field exists and is a real FK from day one, so adding a second market
    is a data change. It is just pre-filled and hidden while there is only one
    to choose from — asking someone to select their city from a list of one is
    a question with no information in it.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        active = Region.objects.filter(is_active=True)
        self.fields["region"].queryset = active
        if active.count() == 1:
            self.fields["region"].initial = active.first()
            self.fields["region"].widget = forms.HiddenInput()


class WorkerProfileForm(StepsMixin, _RegionMixin):
    """A helper's profile.

    Filled in one question per screen the first time — see :mod:`core.steps`,
    whose machinery the job forms share. Thirteen inputs on one screen is a
    screen that says "prove you belong here", and the person writing this has
    often never done the work. It is the last place in the app that can afford
    to look like paperwork.

    Editing shows the whole thing at once, because somebody who came back to
    change their rate should not be walked through six screens to reach it.
    """

    #: Trade first: the one question anybody can answer without thinking, and
    #: the right way to open. Experience rides with it rather than getting a
    #: screen of its own, because "none" is a fine answer here and a screen
    #: asking only that makes it look like a test.
    STEPS = [
        (_("What can you do?"), ["trades", "years_experience"]),
        (_("What do you charge?"), ["rate_type", "rate_min", "rate_max"]),
        (
            _("When can you work?"),
            ["availability_status", "available_dates", "availability_note"],
        ),
        (_("Where do you work?"), ["region", "service_area"]),
        (_("What are you after?"), ["seeking", "open_to_full_time"]),
        (_("Anything you want them to know?"), ["bio"], ["cv"]),
    ]

    #: An explicit Yes/No, not a lone tick box. An unticked box cannot tell
    #: "no" apart from "didn't read it", and this answer decides whether the
    #: worker appears in a whole class of client searches. Required, because a
    #: blank answer here is worse than either real one.
    open_to_full_time = forms.TypedChoiceField(
        label=_("Are you looking for a full-time job?"),
        help_text=_("Full-time roles are posted as standing positions, not day gigs. "
        "You can still take gigs either way."),
        choices=[("True", "Yes — I'd take a permanent position"),
                 ("False", "No — day work only")],
        coerce=lambda value: value == "True",
        widget=forms.RadioSelect,
        required=True,
        empty_value=None,
    )

    #: Only meaningful when availability_status is SPECIFIC_DAYS; parsed into
    #: AvailabilityDate rows so phase 2 can match gig dates with a join.
    #: `data-date-list` is what crew.js looks for. With JS it becomes a native
    #: calendar plus a row of removable chips, and this input goes hidden and
    #: keeps holding the canonical comma-separated value the clean method
    #: below already parses. Without JS it stays exactly what it was — a text
    #: box you can type dates into — so the field never depends on the script.
    available_dates = forms.CharField(
        required=False,
        label=_("Which days?"),
        help_text=_("Pick the days you can work. Past dates can't be chosen."),
        widget=forms.TextInput(
            attrs={
                "placeholder": _("2026-08-04, 2026-08-05"),
                "data-date-list": "",
            }
        ),
    )

    class Meta:
        model = WorkerProfile
        fields = [
            "region",
            # "photo" is deliberately absent: the profile picture moved onto
            # the User, so the same person is not asked for a face twice. The
            # model field stays for existing uploads — see User.display_photo.
            "service_area",
            "trades",
            "years_experience",
            "rate_type",
            "rate_min",
            "rate_max",
            "seeking",
            "availability_status",
            "availability_note",
            "open_to_full_time",
            "bio",
            "cv",
        ]
        widgets = {
            "trades": forms.CheckboxSelectMultiple,
            "bio": forms.Textarea(attrs={"rows": 4}),
            "service_area": forms.TextInput(
                attrs={"placeholder": _("e.g. north side, will travel citywide")}
            ),
            "seeking": forms.TextInput(
                attrs={"placeholder": _("Day gigs this week — framing or second fix")}
            ),
        }
        labels = {
            "trades": _("Trades"),
            "rate_min": _("Rate"),
            "rate_max": _("Up to (leave blank for a flat rate)"),
            "cv": _("Résumé (PDF, optional)"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["trades"].queryset = Trade.objects.all()

        # Today, in the app's timezone rather than the browser's. The picker
        # reads this as its floor, so the calendar opens on the current month
        # and yesterday is not selectable.
        today = timezone.localdate()
        self.fields["available_dates"].widget.attrs.update(
            date_picker_attrs(floor=today)
        )

        if self.instance.pk:
            # Dates already in the past are dropped from what we show rather
            # than round-tripped. Otherwise a worker who saved a date last week
            # and comes back to change their rate gets a validation error on a
            # field they never touched — and stale dates are exactly what
            # _sync_availability_dates exists to clear out anyway.
            self.fields["available_dates"].initial = ", ".join(
                d.date.isoformat()
                for d in self.instance.availability_dates.all()
                if d.date >= today
            )
            # NULL means the question predates this field; leave it unanswered
            # rather than pre-selecting "no" on the worker's behalf.
            if self.instance.open_to_full_time is not None:
                self.fields["open_to_full_time"].initial = str(
                    self.instance.open_to_full_time
                )

    def clean_available_dates(self) -> list[date]:
        # Shared with the offer form's date picker — see core.dates for why
        # a past date is rejected rather than quietly dropped.
        return parse_date_list(
            self.cleaned_data.get("available_dates"), today=timezone.localdate()
        )

    def clean_cv(self):
        cv = self.cleaned_data.get("cv")
        # Only validate a freshly uploaded file. An untouched existing CV comes
        # back as a FieldFile with no size check needed, and touching .size on
        # a missing file would raise.
        if cv and hasattr(cv, "size") and cv.size > rules.MAX_CV_SIZE_MB * 1024 * 1024:
            raise forms.ValidationError(
                f"Résumé must be under {rules.MAX_CV_SIZE_MB} MB."
            )
        return cv

    def clean(self):
        cleaned = super().clean()

        rate_min = cleaned.get("rate_min")
        rate_max = cleaned.get("rate_max")
        if rate_min is not None and rate_max is not None and rate_max < rate_min:
            self.add_error("rate_max", "Upper rate can't be below the lower rate.")

        # A worker who says "specific days" but names none is invisible to the
        # date-matching search, which is the opposite of what they intended.
        if (
            cleaned.get("availability_status") == AvailabilityStatus.SPECIFIC_DAYS
            and not cleaned.get("available_dates")
        ):
            self.add_error(
                "available_dates",
                "Add at least one date, or change availability to 'available now'.",
            )

        return cleaned

    def save(self, commit: bool = True) -> WorkerProfile:
        profile = super().save(commit=commit)
        if commit:
            self._sync_availability_dates(profile)
        return profile

    def _sync_availability_dates(self, profile: WorkerProfile) -> None:
        wanted = set(self.cleaned_data.get("available_dates") or [])
        if self.cleaned_data.get("availability_status") != AvailabilityStatus.SPECIFIC_DAYS:
            # Keep stale dates from silently making a worker look bookable on
            # days they never re-confirmed.
            profile.availability_dates.all().delete()
            return

        existing = {d.date for d in profile.availability_dates.all()}
        profile.availability_dates.filter(date__in=existing - wanted).delete()
        AvailabilityDate.objects.bulk_create(
            [AvailabilityDate(worker=profile, date=d) for d in wanted - existing]
        )


class ClientProfileForm(_RegionMixin):
    class Meta:
        model = ClientProfile
        fields = ["region", "company_name", "location"]
        labels = {
            "company_name": _("Company (optional)"),
            "location": _("Where you're based"),
        }


class PortfolioPhotoForm(forms.ModelForm):
    class Meta:
        model = PortfolioPhoto
        fields = ["image", "caption"]
