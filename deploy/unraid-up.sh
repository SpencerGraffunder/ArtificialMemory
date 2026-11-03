#!/bin/sh
# ArtificialMemory — Unraid bring-up. Paste-friendly; run FROM THE UNRAID TERMINAL
# (or ssh root@<tower>). Idempotent: re-run after any update.
#
# Private repo → needs a fine-grained PAT with Contents:Read:
#   PAT=github_pat_xxx sh unraid-up.sh
#
# Reuse a SearxNG you already run (recommended if you have one) instead of the
# bundled container — give its URL from a container's point of view:
#   SEARCH_URL=http://192.168.4.200:8810 PAT=... sh unraid-up.sh
# With no SEARCH_URL it starts the bundled SearxNG (self-contained first run).
set -eu

APP=/mnt/user/appdata/artmem
SRC=$APP/src
mkdir -p "$APP"

if [ -d "$SRC/.git" ]; then
    git -C "$SRC" pull --ff-only
else
    [ -n "${PAT:-}" ] || { echo "PAT=... required for first run (private repo)"; exit 1; }
    git clone -q "https://x-access-token:${PAT}@github.com/SpencerGraffunder/ArtificialMemory.git" "$SRC"
fi

cd "$SRC"
if [ ! -f .env ]; then
    cp .env.example .env
    tok=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
    sec=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
    sed -i "s|^ARTMEM_TOKEN=.*|ARTMEM_TOKEN=$tok|; s|^SEARXNG_SECRET=.*|SEARXNG_SECRET=$sec|" .env
    echo "──────────────────────────────────────────────────────────"
    echo "ARTMEM_TOKEN written to $SRC/.env — you'll paste it into the iPhone app:"
    grep '^ARTMEM_TOKEN=' .env
    echo "──────────────────────────────────────────────────────────"
fi

# Reuse an existing SearxNG when told to; otherwise run the bundled one.
if [ -n "${SEARCH_URL:-}" ]; then
    grep -q '^SEARCH_URL=' .env && sed -i "s|^SEARCH_URL=.*|SEARCH_URL=$SEARCH_URL|" .env \
                                 || echo "SEARCH_URL=$SEARCH_URL" >> .env
    echo "using existing SearxNG at $SEARCH_URL"
    docker compose up -d --build
else
    docker compose --profile bundled-search up -d --build
fi

sleep 5
code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8378/v1/health \
       -H "Authorization: Bearer $(grep '^ARTMEM_TOKEN=' .env | cut -d= -f2)" || true)
echo "health: HTTP $code (200 = up, 401 = up-but-auth-mismatch, 000 = not yet listening)"
docker compose ps
