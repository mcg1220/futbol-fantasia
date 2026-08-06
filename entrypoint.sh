#!/bin/sh
# Render only allows one persistent disk per service, but this app has two
# directories that need to survive redeploys: data/ (the SQLite db) and
# static/uploads/ (team photos, memes). The disk is mounted at /var/data, so
# this makes both real directories live *there* and symlinks the app's usual
# paths to them — the app code and templates never know the difference.
set -e

mkdir -p /var/data/data /var/data/uploads/team_photos /var/data/uploads/memes

# badges.json ships baked into the image (it's tracked in git, unlike the
# real db). Seed it onto the disk once, before /app/data becomes a symlink,
# so it's still there on every future boot.
if [ ! -f /var/data/data/badges.json ] && [ -f /app/data/badges.json ]; then
  cp /app/data/badges.json /var/data/data/badges.json
fi

if [ ! -L /app/data ]; then
  rm -rf /app/data
  ln -s /var/data/data /app/data
fi

if [ ! -L /app/static/uploads ]; then
  rm -rf /app/static/uploads
  ln -s /var/data/uploads /app/static/uploads
fi

exec "$@"
