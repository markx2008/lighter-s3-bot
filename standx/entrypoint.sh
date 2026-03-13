#!/bin/sh
set -eu

# Avoid duplicate timestamps: Docker already prefixes logs.
# Keep application logs in local timezone (configured via TZ in Dockerfile).
echo "Starting StandX coordinator. LIVE=${LIVE:-0} DRY_RUN=${DRY_RUN:-1} ACCOUNT_EQUITY=${ACCOUNT_EQUITY:-300}"
exec python -m standx.apps.coordinator
