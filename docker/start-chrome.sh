#!/usr/bin/env bash
# Runs Google Chrome on the virtual display with a persistent profile and the
# DevTools protocol on localhost:9222, so the app can drive it over CDP.
set -euo pipefail

PROFILE_DIR="${BROWSER_PROFILE_DIR:-/data/browser-profile}"
mkdir -p "$PROFILE_DIR"

# A previous container leaves its profile locks behind and Chrome refuses to start with them
rm -f "$PROFILE_DIR"/SingletonLock "$PROFILE_DIR"/SingletonSocket "$PROFILE_DIR"/SingletonCookie
# The cache lives outside the data volume (see --disk-cache-dir); drop any left in the profile
rm -rf "$PROFILE_DIR/Default/Cache" "$PROFILE_DIR/Default/Code Cache"

for _ in $(seq 1 50); do
  [ -e /tmp/.X11-unix/X99 ] && break
  sleep 0.2
done

# On Linux the UI language comes from the environment rather than --lang
export LANGUAGE=he

exec google-chrome \
  --user-data-dir="$PROFILE_DIR" \
  --disk-cache-dir=/tmp/chrome-cache \
  --disk-cache-size=104857600 \
  --remote-debugging-port=9222 \
  --no-first-run \
  --no-default-browser-check \
  --no-sandbox \
  --password-store=basic \
  --lang=he \
  --accept-lang=he-IL,he,en-US,en \
  --window-position=0,0 \
  --window-size=1440,900 \
  --hide-crash-restore-bubble \
  --disable-features=Translate \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  about:blank
