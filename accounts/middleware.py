"""Keeping the chosen language on the user, not only in their session.

The header switcher stores the choice in the session, which is right for the
browser and useless for everything else: an email is written by a management
command that has no session and no ``Accept-Language`` header, and the person
who triggered it is usually not the person receiving it.

So the language is copied onto the user the moment it is known. One comparison
per request and no query, and a write only on the request where it actually
changed — which is the one where somebody pressed EL or EN.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model


class RememberLanguage:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        user = getattr(request, "user", None)
        language = getattr(request, "LANGUAGE_CODE", "")
        if (
            language
            and user is not None
            and user.is_authenticated
            and user.language != language
        ):
            # get_user_model rather than type(user): request.user is a lazy
            # proxy, and the proxy class has no manager on it.
            #
            # By id rather than by saving the instance, because this runs on
            # every response and a full save would write back whatever else the
            # view happened to leave on the object.
            get_user_model().objects.filter(pk=user.pk).update(language=language)

        return response
