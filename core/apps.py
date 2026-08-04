from pathlib import Path

from django.apps import AppConfig
from django.utils.autoreload import autoreload_started


class CoreConfig(AppConfig):
    name = "core"

    def ready(self):
        autoreload_started.connect(_watch_env_file)


def _watch_env_file(sender, **kwargs):
    """Make ``runserver`` restart when ``.env`` changes.

    The autoreloader only watches imported ``.py`` files, but ``load_dotenv``
    reads ``.env`` once at settings-import time. Editing a credential therefore
    changed nothing until the process was killed by hand — and the failure was
    silent, because a fresh ``manage.py`` process (``check_google``) read the
    new value while the live server kept serving the old one.
    """
    sender.extra_files.add(Path(__file__).resolve().parent.parent / ".env")
