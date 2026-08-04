"""No models.

The assistant deliberately stores nothing. A form-filling conversation lives in
the user's session until it hands off to the real form, and a Q&A conversation
is discarded when the widget closes.

That is a design position, not an omission. The transcripts would contain
whatever someone typed into a chat box — phone numbers, rates, site addresses,
occasionally things they would not have put on a public profile — and keeping
them would mean answering for their retention, export and deletion. Nothing
downstream needs them: the form is the artefact, and it is saved through the
ordinary model with the ordinary validation.

If usage analytics are wanted later, count events, not messages.
"""
