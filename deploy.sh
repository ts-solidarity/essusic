#!/usr/bin/env bash
set -euo pipefail

HOST="${1:?Usage: ./deploy.sh host [browser]}"
BROWSER="${2:-brave}"
REMOTE_DIR="/opt/essusic"

COOKIE_FILE="$(dirname "$0")/www.youtube.com_cookies.txt"

echo "==> Checking cookies..."
if [[ -f "$COOKIE_FILE" ]]; then
    echo "    Using local cookie file: ${COOKIE_FILE}"
    cp "$COOKIE_FILE" /tmp/essusic-cookies.txt
else
    echo "    Exporting cookies from ${BROWSER}..."
    yt-dlp --cookies-from-browser "$BROWSER" --cookies /tmp/essusic-cookies.txt \
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ" --skip-download --quiet
fi

echo "==> Pushing latest code..."
git push

echo "==> Deploying to ${HOST}..."
ssh "$HOST" "mkdir -p ${REMOTE_DIR}/data"
scp /tmp/essusic-cookies.txt "${HOST}:${REMOTE_DIR}/data/cookies.txt"
ssh "$HOST" "cd ${REMOTE_DIR} && git pull && docker compose up -d --build"
rm /tmp/essusic-cookies.txt

echo "==> Done! Bot is running on ${HOST}"
