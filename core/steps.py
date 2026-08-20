"""Forms that ask one question per screen.

Posting a job and writing a helper's profile are the two long forms in this
app, and both are filled in by somebody standing on a site with a phone. A
screenful of inputs is not hard, exactly — no single answer is — but it reads
as paperwork before the first one is given, and that is where people stop.

So both are grouped into steps. The grouping lives on the form class rather
than in the template because what belongs with what is a fact about the
questions, not about the markup: a template that decided it would have to know
every field name and would go stale the day one was added.

None of this is server state. Every step is rendered into the page inside one
form that posts once; the script hides all but the current one. That is the
whole trick, and it is why there is no half-written job in a session, nothing
to strand between step three and step four, and nothing to migrate when the
steps change. With no JavaScript the page is the long form it always was, with
headings on it.

Here because both apps need it and neither should import the other's form
module — see the region mixins, which are duplicated for exactly that reason.
"""

from __future__ import annotations


class StepsMixin:
    """Adds :meth:`steps` to a form that declares ``STEPS``.

    A ``STEPS`` entry is ``(question, [names])`` or
    ``(question, [names], [folded])``. The third group renders behind a
    disclosure: fields that belong on a step without being what it asks —
    optional, secondary, answered by roughly nobody. They are folded rather
    than dropped, because "we removed the field nobody used" is how a feature
    disappears for the few who did.

    Every visible field must belong to exactly one step. Nothing here enforces
    that, because a form that raised at render time would take the page down;
    the tests enforce it instead, and the failure they catch is silent — a
    field added to the form and forgotten here renders nowhere at all, and the
    first sign of it is a job posted without a price.
    """

    #: Overridden per form. Empty means the form is not stepped.
    STEPS: list = []

    @classmethod
    def step_field_names(cls) -> list:
        """Every field name the steps name, in order, folded ones included.

        One place knows the shape of a STEPS entry. Everything else — the
        coverage tests especially — asks here, so adding a third element to an
        entry does not mean finding every loop that unpacked two.
        """
        names = []
        for _question, fields, *rest in cls.STEPS:
            names.extend(fields)
            names.extend(rest[0] if rest else [])
        return names

    def steps(self):
        """The steps, numbered, with their bound fields.

        Hidden fields are skipped: the region is a real field that is filled in
        and hidden while there is one launch market, and a step containing only
        it would be a screen asking nothing. A step left empty that way
        disappears rather than being counted, so the progress never reads
        "step 2 of 6" on a screen with no question on it.
        """
        rendered = []
        for question, names, *rest in self.STEPS:
            folded_names = rest[0] if rest else []
            fields = [self[n] for n in names if n in self.fields]
            fields = [f for f in fields if not f.is_hidden]
            folded = [self[n] for n in folded_names if n in self.fields]
            folded = [f for f in folded if not f.is_hidden]
            if fields or folded:
                # When the step asks one thing and the field's label is that
                # same thing, the label is kept for the accessibility tree and
                # taken off the screen — see .step-said in the stylesheet.
                # Rendering it twice makes a one-question screen look like a
                # heading with a mistake under it.
                said = (
                    len(fields) == 1
                    and str(fields[0].label).strip() == str(question).strip()
                )
                rendered.append(
                    {
                        "question": question,
                        "fields": fields,
                        "folded": folded,
                        "label_said": said,
                        # For a template that has to put something of its own
                        # beside a particular field — a licence box under the
                        # trades, a link to the résumé already uploaded. Asking
                        # "is this the step with trades on it" is stable;
                        # asking "is this step three" is not, and comparing the
                        # translated question is worse.
                        "field_names": [f.name for f in fields + folded],
                    }
                )

        total = len(rendered)
        for i, step in enumerate(rendered, start=1):
            step["index"] = i
            step["total"] = total
            step["is_last"] = i == total
        return rendered
