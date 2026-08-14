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

from .models import Application, Counter, Job, JobType, PositionType


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
    """One dated shift at a fixed price."""

    class Meta(_BaseJobForm.Meta):
        fields = _BaseJobForm.Meta.fields + [
            "gig_date",
            "gig_hours",
            "fixed_pay",
            "use_escrow",
            "site_latitude",
            "site_longitude",
        ]
        labels = _BaseJobForm.Meta.labels | {
            "gig_date": _("Date"),
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
            "gig_hours": forms.NumberInput(attrs={"step": "0.5", "min": "0.5"}),
            "fixed_pay": forms.NumberInput(attrs={"step": "1", "min": "0"}),
            "site_latitude": forms.NumberInput(attrs={"step": "any", "placeholder": _("40.712776")}),
            "site_longitude": forms.NumberInput(attrs={"step": "any", "placeholder": _("-74.005974")}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.job_type = JobType.GIG
        # Guarded because OfferForm swaps the single gig_date for a list of
        # them — it asks the same question, just allowing more than one answer.
        if "gig_date" in self.fields:
            # A date picker that cannot offer yesterday saves the round trip to
            # a server-side error for the most common slip.
            self.fields["gig_date"].widget.attrs["min"] = (
                timezone.localdate().isoformat()
            )
        for name in ("gig_date", "gig_hours", "fixed_pay"):
            if name in self.fields:
                self.fields[name].required = True


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
    ordinary gig that happens to start as an approach, so the date, hours and
    price it asks for must be exactly the ones escrow will later work from. A
    parallel form would be a second place for those rules to drift.

    The only additions are the covering note and the site coordinates being
    dropped — a client writing to one person is not going to stop and look up
    a latitude, and leaving the fields in makes the form look like paperwork.
    They can still add them later by editing the job.
    """

    #: One or more days, in place of the single ``gig_date`` an ordinary gig
    #: asks for. Booking somebody for Tuesday and Wednesday is one decision and
    #: should be one form; filling this in twice is the kind of paperwork that
    #: makes a client message the worker instead and lose the escrow.
    #:
    #: Each day still becomes its own gig — see the view. A gig is one dated
    #: shift with its own escrow and its own sign-off, and that is not a
    #: presentation detail: two days cannot share a hold when either of them
    #: can be finished, disputed or called off on its own.
    #:
    #: ``data-date-list`` is what crew.js looks for. With JS it becomes a
    #: calendar plus a row of removable chips; without it, the plain text input
    #: it already is. Same widget as the worker availability field, so a client
    #: who also works here meets one date picker, not two.
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

    #: A ceiling, not a rule about how people work. Each day written here is a
    #: row, a thread and a card in the worker's list, and a mistyped paste
    #: should not be able to send somebody ninety of them.
    MAX_DAYS = 14

    class Meta(GigForm.Meta):
        fields = [
            f for f in GigForm.Meta.fields
            if f not in ("site_latitude", "site_longitude", "gig_date")
        ]

    def __init__(self, *args, worker=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.worker = worker

        # Declared fields land after the model's, which would put the dates
        # below the pay. Put them back where gig_date used to be: the day comes
        # before the hours and the price, because the other two are answers
        # about that day.
        order = list(self.Meta.fields)
        order.insert(order.index("gig_hours"), "gig_dates")
        self.order_fields(order + ["note"])

        # The calendar's floor and its wording, both from the server — see
        # core.dates.date_picker_attrs.
        self.fields["gig_dates"].widget.attrs.update(
            date_picker_attrs(floor=timezone.localdate())
        )

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
                    "Send these and write the rest as a second offer."
                )
                % {"count": len(dates), "limit": self.MAX_DAYS}
            )
        return dates

    def clean(self):
        """Give the instance a date before the model gets to validate it.

        ``_post_clean`` runs Job.full_clean, which refuses a gig with no date —
        and gig_date is no longer a form field, so without this the instance
        reaches that check empty. The first day is as good as any: the view
        overwrites it per job, and this instance is only ever the template the
        days are copied from.
        """
        cleaned = super().clean()
        days = cleaned.get("gig_dates")
        # Today as the placeholder when the dates did not survive validation.
        # Job.clean would otherwise report a missing gig_date, and a ModelForm
        # cannot attach a model error to a field it does not have — it raises
        # ValueError instead of rendering. The user has already been told what
        # is actually wrong, on gig_dates, and this instance is never saved.
        self.instance.gig_date = days[0] if days else timezone.localdate()
        return cleaned


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
        fields = ["fixed_pay", "gig_hours", "gig_date", "note"]
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
                attrs={"placeholder": _("e.g. That's a long day for the price — $280 and it's yours.")}
            ),
        }

    def __init__(self, *args, terms=None, **kwargs):
        """``terms`` is the job as it stands, plus any counter already agreed."""
        super().__init__(*args, **kwargs)
        self.terms = terms
        if terms is None:
            return

        self.fields["gig_date"].widget.attrs["min"] = timezone.localdate().isoformat()
        if not self.is_bound:
            for name in ("fixed_pay", "gig_hours", "gig_date"):
                self.fields[name].initial = getattr(terms, name)

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
            and cleaned.get(name) != getattr(self.terms, name)
            for name in ("fixed_pay", "gig_hours", "gig_date")
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
