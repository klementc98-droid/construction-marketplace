# The interface

There is no component framework here and there is not going to be one. A
component is a template partial with a name, a documented input, and one job;
the list below is what exists, so that the next person adding a screen reaches
for one of these instead of inventing a fourth kind of card.

Everything renders on the server. JavaScript is only ever an improvement on a
page that already works without it — see the header of `static/js/crew.js`.

## The one question this interface answers

Most of the people reading this board have never done the work and arrive
assuming they are not allowed in. So the experience level is not a detail of a
listing, it is the first thing a card says after what the job is, and it is a
filter you can reach without discovering that a filter panel exists.

If you are adding a screen that shows a job, it shows the level. If it shows a
list of jobs, the level is filterable from it.

## Components

| Name | File | What it is |
| --- | --- | --- |
| **JobCard** | `templates/jobs/_job_row.html` | One job on any list. Takes `job`. Trade and title, then the level, then where/when, then the arithmetic — in the order a reader asks for them. |
| **ExperienceBadge** | `templates/jobs/_experience_badge.html` | The level, in three tones. Takes `job`, reads `job.experience_tone`. Green is deliberately the loudest of the three. |
| **ExperienceChips** | `templates/jobs/_experience_chips.html` | The level as a one-tap filter. Takes `form` (a `JobFilterForm`), reads `form.chips`. Links, not a form: each chip is a URL you can send someone. |
| **JobHead** | top of `templates/jobs/job_detail.html` | The card's three bands at page-heading size, so opening a card resolves into the page it was showing. |
| **ApplyBar** | bottom of `templates/jobs/job_detail.html` | Apply, pinned to the bottom of a phone screen with the rate beside it. Hidden on the breakpoint where the inline row shows — one Apply per screen, never two. |
| **StepForm** | `templates/jobs/job_form.html`, `templates/accounts/worker_edit.html` | One question per screen. Every fieldset is in the page; the script hides all but one. See below. |
| **StepProgress** | `templates/jobs/_step_progress.html` | A bar and the words. Takes `total`. Hidden until the script shows it. |
| **PathCards** | `templates/accounts/_paths.html` | The two ways in, in the first person. On the homepage, above the account button. |
| **Openers** | in `templates/jobs/job_apply.html` | Buttons that write the first line of an application into the box. Append, never replace. |
| **EmptyState** | `.card.empty`, in each list template | An icon, what is empty, and what to do about it. A filtered empty list and a bare one are different problems and get different advice. |
| **Field** | `templates/_field.html` | One form field, everywhere. Label → errors → widget → help, and the order is load-bearing. |

## StepForm, and why it is not a wizard

Posting a job asks six questions for a gig and five for a position, one to a
screen. None of that is server state.

Every fieldset is rendered into the page inside one form that posts once. The
script hides all but the current step and moves between them. So:

- there is no half-written job in a session, and nothing to strand between step
  three and step four;
- the server validates exactly what it validated when this was one long form;
- with no JavaScript the page is that long form, with headings on it.

The grouping lives on the form (`STEPS` on `GigForm` / `StandingForm`), not in
the template, because what belongs with what is a fact about the questions. A
coverage test fails if any visible field belongs to no step — the symptom
otherwise is a job posted without a price.

A `STEPS` entry is `(question, [names])` or `(question, [names], [folded])`.
The third group renders behind a disclosure: fields that belong on a step
without being what it asks, like the optional site coordinates on "How is it
paid?". Fold them rather than deleting them — "we removed the field nobody
used" is how a feature disappears for the few who did.

Two forms use it: posting a job and writing a helper's profile. The machinery
is `core.steps.StepsMixin`, in `core/` rather than either app, because neither
app should import the other's form module.

Neither is stepped when it is being edited. Somebody who came to change one
number should not be walked through six screens to reach it. The job form tells
the two apart by whether it has an instance; the profile asks whether it has
any trades, because the profile row is created when the role is chosen and so
its existence proves nothing.

## Dates

There is one date control, `data-date-list`, drawn by `crew.js` and configured
by `core.dates.date_picker_attrs`. Posting a job, offering one, setting your
availability and countering an offer all use it, and all of them pick a set of
days rather than a day.

Two pickers for the same question is how one of them quietly stays broken. If a
form needs dates, it gets this one — with `taken=` set to the days the worker
has already sold, so a day that cannot be agreed is not tickable rather than
being refused after the form is filled in.

## Icons

One inline SVG sprite in `templates/base.html`, stroke-based, `currentColor`,
`stroke-width` 1.8–2 depending on the size the icon renders at. Use it with
`<svg viewBox="0 0 24 24"><use href="#i-name"/></svg>`.

No emoji in the interface. An emoji is a different typeface at a different
weight in every browser, and a board of them reads as a chat window rather than
as a place people are hired.

Trades map to icons in `Job.TRADE_ICONS`, and an unmapped trade falls back to
`i-trade` rather than to a hole in the card. A test asserts every mapped id
exists in the sprite and every seeded trade is mapped.

## Colour

Tokens in `static/css/crew.css`, defined once on `:root` and re-stated for
`:root[data-theme="luxe"]`. Never hardcode a colour in a component.

Gold is the money and the primary action. It is not the colour of "yes you can
do this" — that is `--ok`, and the distinction is the point: the badge is an
invitation, not a price.

Three tones carry meaning and each is paired with a word and a shape, never
with colour alone: `--ok` / `--warn` / `--err`, as `go` / `steady` / `stop`.

## Mobile

Design at 390px first. The tab bar is 62px and lives at the bottom on anything
under 780px; anything else fixed to the bottom of the screen offsets by
`var(--tabbar-h) + env(safe-area-inset-bottom)` and leaves a spacer, or it will
sit on top of the last card.

Tap targets that a person wears gloves for are 40px minimum. Wide rows
(chips, tables, diagrams) scroll inside their own container — the page body
never scrolls sideways.
