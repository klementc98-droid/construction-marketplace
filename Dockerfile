# XTISE — the image that runs in production.
#
# Two stages, and the split is about what ends up on the server rather than
# about size. The builder has a compiler and the wheels it needed; the runtime
# has neither, so a dependency with a build step cannot leave one behind for
# somebody to find later.
#
# Pinned to the same Python the app is developed on. A marketplace that moves
# money should not discover a version difference in production.

# ---------------------------------------------------------------- builder ---
FROM python:3.14-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Requirements first, and only requirements: this layer is rebuilt when the
# pins change and reused on every commit that does not touch them, which is
# nearly all of them.
COPY requirements.txt .
RUN python -m venv /venv \
 && /venv/bin/pip install --no-cache-dir -r requirements.txt


# ---------------------------------------------------------------- runtime ---
FROM python:3.14-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/venv/bin:$PATH"

# Not root. If something in this app is ever talked into writing a file, it
# should not be able to write one that the next process will execute.
RUN useradd --create-home --uid 10001 xtise

WORKDIR /app
COPY --from=builder /venv /venv
COPY --chown=xtise:xtise . .

# Built into the image rather than on boot, so every container serves the same
# bytes and a restart cannot produce a half-collected static directory.
#
# collectstatic imports settings, which insists on a key. A throwaway one, only
# ever alive for this layer: the real one arrives as an environment variable at
# run time, and nothing here is signed or encrypted with this.
RUN DJANGO_SECRET_KEY=build-only-never-used \
    DJANGO_DEBUG=False \
    python manage.py collectstatic --noinput --clear \
 && python manage.py compilepo

USER xtise
EXPOSE 8000

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--threads", "2", \
     "--timeout", "60", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
