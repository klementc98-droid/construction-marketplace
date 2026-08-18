"""Posting, applying, and the browse filters.

The two post types get two forms rather than one form that hides half its
fields. A client posting a gig should never see a rate-range input at all —
not disabled, not hidden, absent. It also keeps :meth:`Job.clean` honest: each
form can only produce the shape its type allows.
"""

from __future__ import annotations

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.dates import date_picker_attrs, parse_date_list
from core.models import Region, Trade

from .models import Application, Counter, Job, JobType, PositionType, Review


class _RegionDefaultMixin(forms.ModelForm):
    """Pre-fill and hide the region while there is only one launch market.

    Same reasoning as the mixin in ``accounts.forms``: the field is a real FK
    from day one so a second market is a data change, but asking someone to
    pick their city from a list of one is a question with no information in
    it. Kept local so the two apps do not depend on each other's form guts.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        active = Region.objects.filter(is_active=True)
        self.fields["region"].queryset = active
        if active.count() == 1:
            self.fields["region"].initial = active.first()
            self.fields["region"].widget = forms.HiddenInput()


class _BaseJobForm(_RegionDefaultMixin):
    """What both post types ask for."""

    class Meta:
        model = Job
        fields = ["trade", "title", "description", "region", "location"]
        labels = {
            "trade": _("Trade"),
            "title": _("Give it a short name"),
            "description": _("Describe the work"),
            "location": _("Where in town?"),
        }
        help_texts = {
            "title": _("The line people see on the board — a few words, not the whole job."),
            "location": _("Neighbourhood, cross streets, or site name."),
        }
        widgets = {
            "description": forms.Textarea(attrs={"rows": 6}),
            "title": forms.TextInput(attrs={"placeholder": _("e.g. Framing carpenter, 2-storey rebuild")}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["trade"].queryset = Trade.objects.all()


class GigForm(_BaseJobForm):
    """One dated shift at a fixed price — or several, one per day.

    The form asks for a set of days rather than a single date, and the view
    writes one gig per day. A gig is still one dated shift: two days cannot
    share a row when either can be finished, disputed or called off while the
    other runs normally. What is shared is the asking, because "Monday and
    Tuesday" is one decision and filling the form twice is the kind of
    paperwork that ends with the client messaging the worker instead.

    Lives here rather than on OfferForm, where it started, so posting to the
    board and offering somebody directly meet the same date picker. Two pickers
    for the same question is how one of them quietly stays broken.
    """

    #: ``data-date-list`` is what crew.js looks for. With JS it becomes a
    #: calendar that stays open across several taps plus a row of removable
    #: chips; without it, the plain text input it already is. Same widget as the
    #: worker availability field, so somebody who both works and hires here
    #: meets one date control.
    gig_dates = forms.CharField(
        label=_("Which days?"),
        help_text=_("Pick one or more. Each day is booked and paid separately."),
        # Its own required message. "This field is required" on a picker that
        # shows chips reads as a fault in the widget rather than an answer the
        # form is still waiting for.
        error_messages={"required": _("Pick at least one day.")},
        widget=forms.TextInput(
            attrs={
                "placeholder": _("2026-08-04, 2026-08-05"),
                "data-date-list": "",
            }
        ),
    )

    #: A ceiling, not a rule about how people work. Each day written here is a
    #: row, a thread and a card in somebody's list, and a mistyped paste should
    #: not be able to create ninety of them.
    MAX_DAYS = 14

    class Meta(_BaseJobForm.Meta):
        fields = _BaseJobForm.Meta.fields + [
            "gig_hours",
            "fixed_pay",
            "use_escrow",
            "site_latitude",
            "site_longitude",
        ]
        labels = _BaseJobForm.Meta.labels | {
            "use_escrow": _("How is this paid?"),
            "gig_hours": _("Hours"),
            "fixed_pay": _("Total pay for the day"),
            "site_latitude": _("Site latitude (optional)"),
            "site_longitude": _("Site longitude (optional)"),
        }
        help_texts = _BaseJobForm.Meta.help_texts | {
            "fixed_pay": _("What the worker is paid in full, before the platform fee."),
            "use_escrow": _(
                "Escrow means the money is charged before the worker travels "
                "and released once you sign the day off. Settling directly is "
                "between the two of you — we keep the record, not the money."
            ),
            "site_longitude": _("If you add these, we can sanity-check the worker's "
            "check-in against the site. It is only ever a note on the record — "
            "never a condition of them checking in."),
        }
        widgets = _BaseJobForm.Meta.widgets | {
            # Two labelled options, not a lone tick box. An unticked box cannot
            # tell "settle it ourselves" apart from "did not read the question",
            # and this one decides whether anybody's money is protected.
            "use_escrow": forms.RadioSelect(
                choices=[
                    (True, _("Hold it in escrow until the day is signed off")),
                    (False, _("We'll settle it directly — cash, invoice, our own way")),
                ]
            ),
            "gig_hours": forms.NumberInput(attrs={"step": "0.5", "min": "0.5"}),
            "fixed_pay": forms.NumberInput(attrs={"step": "1", "min": "0"}),
            "site_latitude": forms.NumberInput(attrs={"step": "any", "placeholder": _("40.712776")}),
            "site_longitude": forms.NumberInput(attrs={"step": "any", "placeholder": _("-74.005974")}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.job_type = JobType.GIG

        for name in ("gig_hours", "fixed_pay"):
            self.fields[name].required = True

        # Declared fields land after the model's, which would put the dates
        # below the pay. Back where the single date used to be: the day comes
        # first, because the hours and the price are answers about that day.
        order = [f for f in self.Meta.fields]
        order.insert(order.index("gig_hours"), "gig_dates")
        self.order_fields(order)

        # The calendar's floor and its wording, both from the server — see
        # core.dates.date_picker_attrs. A picker that cannot offer yesterday
        # saves the round trip to a server-side error for the commonest slip.
        self.fields["gig_dates"].widget.attrs.update(
            date_picker_attrs(floor=timezone.localdate())
        )

    def clean_gig_dates(self) -> list:
        dates = parse_date_list(
            self.cleaned_data.get("gig_dates"), today=timezone.localdate()
        )
        if not dates:
            raise forms.ValidationError(_("Pick at least one day."))
        if len(dates) > self.MAX_DAYS:
            raise forms.ValidationError(
                _(
                    "That's %(count)s days — %(limit)s at a time is the limit. "
                    "Post these and write the rest as a second batch."
                )
                % {"count": len(dates), "limit": self.MAX_DAYS}
            )
        return dates

    def clean(self):
        """Give the instance a date before the model gets to validate it.

        ``_post_clean`` runs Job.full_clean, which refuses a gig with no date —
        and gig_date is not a form field here, so without this the instance
        reaches that check empty. The first day is as good as any: the view
        overwrites it per job, and this instance is only ever the template the
        days are copied from.

        Today as the placeholder when the dates did not survive validation.
        Job.clean would otherwise report a missing gig_date, and a ModelForm
        cannot attach a model error to a field it does not have — it raises
        ValueError instead of rendering. The reader has already been told what
        is actually wrong, on gig_dates, and this instance is never saved.
        """
        cleaned = super().clean()
        days = cleaned.get("gig_dates")
        self.instance.gig_date = days[0] if days else timezone.localdate()
        return cleaned


class StandingForm(_BaseJobForm):
    """An open position, paid at a rate rather than a fixed total."""

    class Meta(_BaseJobForm.Meta):
        fields = _BaseJobForm.Meta.fields + [
            "position_type",
            "rate_type",
            "rate_min",
            "rate_max",
        ]
        labels = _BaseJobForm.Meta.labels | {
            "position_type": _("Type of position"),
            "rate_type": _("Paid by"),
            "rate_min": _("Rate"),
            "rate_max": _("Up to (optional)"),
        }
        help_texts = _BaseJobForm.Meta.help_texts | {
            "rate_max": _("Leave blank if it is a single flat rate."),
        }
        widgets = _BaseJobForm.Meta.widgets | {
            "rate_min": forms.NumberInput(attrs={"step": "1", "min": "0"}),
            "rate_max": forms.NumberInput(attrs={"step": "1", "min": "0"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.job_type = JobType.STANDING
        self.fields["position_type"].choices = PositionType.choices
        for name in ("position_type", "rate_type", "rate_min"):
            self.fields[name].required = True


JOB_FORMS = {JobType.GIG: GigForm, JobType.STANDING: StandingForm}


class OfferForm(GigForm):
    """A gig written for one named worker.

    Subclasses :class:`GigForm` rather than reimplementing it: an offer is an
    ordinary gig that happens to start as an approach, so the days, hours and
    price it asks for must be exactly the ones escrow will later work from. A
    parallel form would be a second place for those rules to drift — including
    the date picker, which is why the multi-day field lives on GigForm now and
    not here.

    The only additions are the covering note and the site coordinates being
    dropped — a client writing to one person is not going to stop and look up
    a latitude, and leaving the fields in makes the form look like paperwork.
    They can still add them later by editing the job.
    """

    note = forms.CharField(
        label=_("Anything they should know?"),
        required=False,
        max_length=1500,
        widget=forms.Textarea(
            attrs={
                "rows": 5,
                "placeholder": _("Why you're asking them, what the day looks like, "
                "where to park, who to ask for on site…"),
            }
        ),
        help_text=_("Only this worker sees it. The description above is the job "
        "itself and stays with the post."),
    )

    class Meta(GigForm.Meta):
        fields = [
            f for f in GigForm.Meta.fields
            if f not in ("site_latitude", "site_longitude")
        ]

    def __init__(self, *args, worker=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.worker = worker

        # The note is declared here, so it lands after everything GigForm
        # ordered and needs putting at the end explicitly.
        self.order_fields([f for f in self.fields if f != "note"] + ["note"])

        if worker is None:
            return

        self.fields["title"].widget.attrs["placeholder"] = (
            f"e.g. Second fix with {worker.user.short_name or worker.user}"
        )
        # Default to what this person actually does. You are on their profile
        # because of their trade, so making it the pre-filled answer removes
        # the one question in this form whose answer is already known. Still a
        # full list underneath — people do work outside their listed trades,
        # and an offer is exactly where that conversation starts.
        if not self.is_bound:
            first_trade = worker.trades.first()
            if first_trade is not None:
                self.fields["trade"].initial = first_trade


class OfferExistingForm(forms.Form):
    """Send a worker a post the client already has, instead of writing another.

    Most direct offers are for work that is already on the board. Retyping the
    title, the trade, the date, the hours and the price to reach one named
    person is not just tedious — it produces a second job that is meant to be
    the same job, and the two drift the moment either is edited.

    So this form collects a choice and a covering note, and nothing else. Every
    figure comes from the post that already exists, which is the only copy of
    those numbers there has ever been.

    The queryset is passed in rather than filtered here: which of a client's
    posts may be offered is a rule about offers — see ``_offerable_jobs`` —
    and a form that decided it for itself would be a second place to keep that
    rule right. Passing it in is also what stops a posted primary key reaching
    somebody else's job, since validation can only match what is in the list.
    """

    job = forms.ModelChoiceField(
        queryset=Job.objects.none(),
        label=_("Which job?"),
        error_messages={
            "required": _("Pick one of your open jobs, or write a new one."),
            "invalid_choice": _(
                "That job isn't open to offer any more — it may have been "
                "taken or already sent to somebody."
            ),
        },
    )
    note = forms.CharField(
        label=_("Anything they should know?"),
        required=False,
        max_length=1500,
        widget=forms.Textarea(
            attrs={
                "rows": 5,
                "placeholder": _("Why you're asking them, what the day looks like, "
                "where to park, who to ask for on site…"),
            }
        ),
        help_text=_("Only this worker sees it. The post itself stays as it is."),
    )

    def __init__(self, *args, offerable=None, **kwargs):
        super().__init__(*args, **kwargs)
        if offerable is not None:
            self.fields["job"].queryset = offerable


class OfferResponseForm(forms.Form):
    """The worker's reply. Optional either way.

    "No" is a complete answer and nobody should have to justify it to get out
    of the form — a required reason turns declining into a chore and the
    result is offers left unanswered, which is worse for the client than a
    fast no.
    """

    response_note = forms.CharField(
        label=_("Add a note (optional)"),
        required=False,
        max_length=300,
        widget=forms.Textarea(
            attrs={"rows": 3, "placeholder": _("e.g. Can't do the 14th, but the 15th works.")}
        ),
    )


class CounterForm(forms.ModelForm):
    """Revised terms on a live offer, from whichever side is proposing.

    One form for both directions. The questions are identical — what should the
    pay be, what day, how long — and a client-shaped and a worker-shaped
    version of the same three fields would be two places to fix the same bug.

    Every field is pre-filled with the terms currently on the table, so the
    person countering edits a real number rather than filling in a blank. Most
    counters move one figure, and starting from the current one makes that a
    single edit instead of a re-entry of everything.
    """

    class Meta:
        model = Counter
        fields = ["fixed_pay", "gig_hours", "gig_date", "use_escrow", "note"]
        labels = {
            "fixed_pay": _("Total pay for the day"),
            "gig_hours": _("Hours"),
            "gig_date": _("Date"),
            "use_escrow": _("How is this paid?"),
            "note": _("Why? (optional)"),
        }
        widgets = {
            "fixed_pay": forms.NumberInput(attrs={"step": "1", "min": "1"}),
            "gig_hours": forms.NumberInput(attrs={"step": "0.5", "min": "0.5"}),
            "gig_date": forms.DateInput(attrs={"type": "date"}),
            # Two labelled options, not a lone tick box. An unticked box cannot
            # tell "settle it ourselves" apart from "did not read the question",
            # and this one decides whether anybody's money is protected.
            "use_escrow": forms.RadioSelect(
                choices=[
                    (True, _("Hold it in escrow until the day is signed off")),
                    (False, _("We'll settle it directly — cash, invoice, our own way")),
                ]
            ),
            "note": forms.TextInput(
                attrs={"placeholder": _("e.g. That's a long day for the price — €280 and it's yours.")}
            ),
        }

    def __init__(self, *args, terms=None, **kwargs):
        """``terms`` is the job as it stands, plus any counter already agreed."""
        super().__init__(*args, **kwargs)
        self.terms = terms

        # The same calendar the offer was written on. Attached before the early
        # return below, because the picker is how the field is operated and not
        # part of pre-filling it — a counter form built without terms still has
        # a date to collect.
        #
        # The input keeps type="date". The script hides it and writes ISO into
        # it, which is what a date input holds anyway, so with JS off this is
        # still the native picker rather than a text box asking for a format.
        self.fields["gig_date"].widget.attrs.update(
            date_picker_attrs(floor=timezone.localdate(), single=True)
        )
        self.fields["gig_date"].widget.attrs["min"] = timezone.localdate().isoformat()

        if terms is None:
            return
        if not self.is_bound:
            for name in ("fixed_pay", "gig_hours", "gig_date", "use_escrow"):
                self.fields[name].initial = getattr(terms, name, None)

    def clean_gig_date(self):
        day = self.cleaned_data.get("gig_date")
        if day is not None and day < timezone.localdate():
            raise forms.ValidationError("That date has already passed.")
        return day

    def clean(self):
        cleaned = super().clean()
        if self.terms is None:
            return cleaned

        # A counter that changes nothing is not a counter. Without this, the
        # accept button on the other side would offer somebody the terms they
        # had already been offered, which reads as a bug and wastes a round.
        moved = any(
            cleaned.get(name) is not None
            and cleaned.get(name) != getattr(self.terms, name, None)
            for name in ("fixed_pay", "gig_hours", "gig_date", "use_escrow")
        )
        if not moved:
            raise forms.ValidationError(
                "Change the pay, the hours or the date — otherwise there's "
                "nothing here to answer."
            )
        return cleaned


class ApplicationForm(forms.ModelForm):
    class Meta:
        model = Application
        fields = ["message"]
        labels = {"message": "Message to the client"}
        widgets = {
            "message": forms.Textarea(
                attrs={
                    "rows": 5,
                    "placeholder": _("Optional — what makes you right for this one?"),
                }
            )
        }


class BrowseFilterForm(forms.Form):
    """Shared shape for the two browse filters.

    The search box is rendered on its own in the bar and everything else lives
    behind the Filters button, so the template needs to walk "the rest" without
    a hardcoded list of names that would go stale the day a filter is added.
    """

    #: Rendered in the search bar rather than the panel.
    SEARCH_FIELD = "q"

    def panel_fields(self):
        return [f for f in self if f.name != self.SEARCH_FIELD]

    def active_count(self) -> int:
        """How many panel filters are set.

        Shown on the button, and the reason the panel opens itself when it is
        non-zero. Collapsing the filters is what makes the bar small; it also
        makes an applied filter invisible, and "why are there only three jobs"
        is a bad way to find out you left a trade selected last week.
        """
        return sum(1 for field in self.panel_fields() if self.data.get(field.html_name))


class JobFilterForm(BrowseFilterForm):
    """Browse filters. Bound to GET so a filtered list is a shareable URL."""

    q = forms.CharField(
        required=False,
        label=_("Search"),
        widget=forms.TextInput(
            attrs={"placeholder": _("Title, description, or area")}
        ),
    )
    trade = forms.ModelChoiceField(
        queryset=Trade.objects.all(), required=False, empty_label=_("All trades")
    )
    job_type = forms.ChoiceField(
        required=False,
        label=_("Type"),
        choices=[("", _("Standing and gigs"))] + list(JobType.choices),
    )

    def filtered(self, queryset):
        data = self.cleaned_data if self.is_valid() else {}
        trade = data.get("trade")
        return (
            queryset.matching(data.get("q"))
            .for_trade(trade.slug if trade else None)
            .for_type(data.get("job_type"))
        )


class WorkerFilterForm(BrowseFilterForm):
    """The mirror of :class:`JobFilterForm`, for clients looking for people."""

    q = forms.CharField(
        required=False,
        label=_("Search"),
        widget=forms.TextInput(
            attrs={"placeholder": _("Name, bio, or service area")}
        ),
    )
    trade = forms.ModelChoiceField(
        queryset=Trade.objects.all(), required=False, empty_label=_("All trades")
    )
    available_now = forms.BooleanField(required=False, label=_("Available now only"))
    full_time = forms.BooleanField(
        required=False,
        label=_("Open to full-time"),
        help_text=_("Workers who said they'd take a permanent position."),
    )


class ReviewForm(forms.ModelForm):
    """One side's verdict on a finished job.

    A score and, if they want, words. The comment is optional on purpose:
    forcing prose is how a board fills up with "good" a thousand times, and a
    bare score is still a usable data point.
    """

    class Meta:
        model = Review
        fields = ["rating", "comment"]
        labels = {
            "rating": _("How did it go?"),
            "comment": _("Anything to add? (optional)"),
        }
        widgets = {
            # Radios, not a select or a star widget that needs script. Five
            # options is few enough to show all of them, and a tap target per
            # score beats a dropdown on a phone.
            "rating": forms.RadioSelect(
                choices=[
                    (5, _("5 — would work with them again without thinking")),
                    (4, _("4 — good, no complaints")),
                    (3, _("3 — the job got done")),
                    (2, _("2 — problems worth knowing about")),
                    (1, _("1 — would not work with them again")),
                ]
            ),
            "comment": forms.Textarea(
                attrs={
                    "rows": 4,
                    "placeholder": _("Turned up on time, tidy work, no chasing."),
                }
            ),
        }
