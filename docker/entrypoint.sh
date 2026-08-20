#!/bin/sh
# Everything that has to be true before the first request, and nothing else.
#
# Migrations run here rather than in a separate deploy step because there is
# one web container: the alternative is a step somebody can forget between
# pulling an image and starting it, and the failure mode of forgetting is a
# 500 on a column that does not exist yet.
#
# `set -e` matters more than usual. If a migration fails, this must stop —
# starting gunicorn against a half-migrated database would serve errors while
# looking healthy to anything watching the port.
set -e

echo "==> migrate"
python manage.py migrate --noinput

# Idempotent, and cheap when the table is already there. The assistant's rate
# limit counts in this table; without it every check raises and the limit is
# not a limit. See CACHES in settings for why the database and not Redis.
echo "==> cache table"
python manage.py createcachetable

exec "$@"
