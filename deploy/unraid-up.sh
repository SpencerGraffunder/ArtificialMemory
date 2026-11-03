#!/bin/sh
# Ambient Memory — Unraid bring-up. Paste-friendly; run FROM THE UNRAID TERMINAL
# (or ssh root@tower). Idempotent: re-run after any update.
#
# Usage (private repo — needs a fine-grained PAT with Contents:Read on
# SpencerGraffunder/ambient-memory, OR pre-placed source, see unraid.md):
#   PAT=github_pat_xxx sh unraid-up.sh
set -eu

APP=/mnt/user/appdata/ambient-memory
SRC=$APP/src
mkdir -p "$APP"

if [ -d "$SRC/.git" ]; then
    git -C "$SRC" pull --ff-only
else
    [ -n "${PAT:-}" ] || { echo "PAT=... required for first run (private repo)"; exit 1; }
    git clone -q "https://x-access-token:${PAT}@github.com/SpencerGraffunder/ambient-memory.git" "$SRC"
fi

cd "$SRC"
if [ ! -f .env ]; then
    cp .env.example .env
    tok=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
    sec=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
    sed -i "s|^AMBIENT_TOKEN=.*|AMBIENT_TOKEN=$tok|; s|^SEARXNG_SECRET=.*|SEARXNG_SECRET=$sec|" .env
    echo "──────────────────────────────────────────────────────────"
    echo "AMBIENT_TOKEN written to $SRC/.env — you'll paste it into the iPhone app:"
    grep '^AMBIENT_TOKEN=' .env
    echo "──────────────────────────────────────────────────────────"
fi

# If a SearxNG container already exists on this box (owner had one before), keep
# it and skip the bundled one:
#   AMBIENT_SEARCH__SEARXNG_URL=http://<that-container>:8080  (same compose net)
# The bundled SearxNG is the default — set up, keyed, no config needed.
docker compose --env-file .env up -d --build

sleep 5
code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8378/v1/health \
       -H "Authorization: Bearer $(grep '^AMBIENT_TOKEN=' .env | cut -d= -f2)" || true)
echo "health: HTTP $code (200 = up, 401 = up-but-auth-mismatch, 000 = not yet listening)"
docker compose ps
