"""Bridge between Google's OAuth payload and our User model."""

from __future__ import annotations

from allauth.socialaccount.adapter import DefaultSocialAccountAdapter


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    """Copy the useful bits of the Google profile onto the user.

    Only ever fills BLANK fields on an existing account. A returning user who
    has set their own display name should not have it overwritten by whatever
    their Google account happens to say this week — the first sign-in seeds
    the profile, it does not own it thereafter.
    """

    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)

        name = (data.get("name") or "").strip()
        if not name:
            name = " ".join(
                part for part in (data.get("first_name"), data.get("last_name")) if part
            ).strip()
        if name and not user.full_name:
            user.full_name = name

        picture = (sociallogin.account.extra_data or {}).get("picture", "")
        if picture and not user.google_picture_url:
            user.google_picture_url = picture

        return user
