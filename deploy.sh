#!/usr/bin/env bash
set -euo pipefail

HOST="${1:?Usage: ./deploy.sh user@host}"
REMOTE_DIR="/opt/essusic"

echo "==> Exporting cookies from Brave..."
yt-dlp --cookies-from-browser brave --cookies /tmp/essusic-cookies.txt \
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ" --skip-download --quiet

echo "==> Pushing latest code..."
git push

echo "==> Deploying to ${HOST}..."
ssh "$HOST" "mkdir -p ${REMOTE_DIR}/data"
scp /tmp/essusic-cookies.txt "${HOST}:${REMOTE_DIR}/data/cookies.txt"
ssh "$HOST" "cd ${REMOTE_DIR} && git pull && docker compose up -d --build"
rm /tmp/essusic-cookies.txt

echo "==> Done! Bot is running on ${HOST}"
