#!/usr/bin/env bash
# Watchdog: checks data.json age + data integrity, alerts via Telegram.
# Called hourly from cron.
#
# SECURITY NOTE: credentials are read from .env, never hardcoded here.
# An earlier version of this script had a live bot token committed
# directly into the file — if you're rotating from that version, revoke
# the old token via @BotFather (/mybots -> bot -> Revoke token) first;
# a token that was ever committed to a public repo must be treated as
# compromised regardless of what this script does going forward.

set -euo pipefail

DIR="/opt/altcoin-terminal"
DATA="$DIR/data.json"
LOG="$DIR/watchdog.log"
ENV_FILE="$DIR/.env"

# Load TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from .env without executing
# the rest of the file as shell (in case it contains values with special
# characters) -- read only the two keys we need.
if [ -f "$ENV_FILE" ]; then
    BOT_TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2-)
    CHAT_ID=$(grep -E '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | head -1 | cut -d= -f2-)
fi
BOT_TOKEN="${BOT_TOKEN:-}"
CHAT_ID="${CHAT_ID:-}"
STALE_HOURS="${STALE_ALERT_HOURS:-7}"

now=$(date +%s)
file_ts=$(stat -c %Y "$DATA" 2>/dev/null || echo 0)
alerts=()

# ── 1. Stale check ──
if [ "$file_ts" -eq 0 ]; then
    alerts+=("⚠️ FILE: data.json not found")
else
    age=$(( (now - file_ts) / 3600 ))
    if [ "$age" -ge "$STALE_HOURS" ]; then
        alerts+=("⚠️ STALE: data.json ${age}h old (threshold ${STALE_HOURS}h)")
    fi
    echo "[$(date '+%F %T')] age=${age}h" >> "$LOG"
fi

# ── 2. Data integrity check (via python) ──
if [ -f "$DATA" ]; then
    issues=$(python3 -c "
import json, sys
try:
    d = json.load(open('$DATA'))
except Exception as e:
    print(f'PARSE: {e}')
    sys.exit(0)

lines = []

coins = d.get('coins', {})
total = len(coins)
bad = [s for s, c in coins.items() if c.get('status') != 'ok']
if bad:
    shown = ','.join(bad[:5]) + ('…' if len(bad) > 5 else '')
    lines.append(f'COINS: {len(bad)}/{total} unavailable — {shown}')

asi = d.get('alt_season')
if not asi:
    lines.append('ALT_SEASON: field missing')
elif asi.get('index') is None:
    lines.append(f'ALT_SEASON: index null ({asi.get(\"label\")})')

glf = d.get('macro', {}).get('glf_details', {})
comps = glf.get('components', {})
if not comps:
    lines.append('GLF: no component detail')
elif glf.get('status') not in ('ok', None) and glf.get('active_components', 0) < 4:
    lines.append(f'GLF: degraded ({glf.get(\"active_components\", 0)}/8 components)')

conc = d.get('concentration_warning')
if conc and conc.get('flag'):
    lines.append(f'CONCENTRATION: avg corr {conc.get(\"avg_corr\")} among top-10')

if lines:
    print(' | '.join(lines))
" 2>>"$LOG")
    if [ -n "$issues" ]; then
        alerts+=("🔴 $issues")
    fi
fi

# ── 3. Send alert if any ──
if [ ${#alerts[@]} -gt 0 ]; then
    if [ -z "$BOT_TOKEN" ] || [ -z "$CHAT_ID" ]; then
        echo "[$(date '+%F %T')] ${#alerts[@]} issue(s) found but TELEGRAM_BOT_TOKEN/CHAT_ID not set in .env — printing instead:" >> "$LOG"
        printf '%s\n' "${alerts[@]}" >> "$LOG"
    else
        msg="ALTCOIN WATCHDOG"
        for a in "${alerts[@]}"; do msg+="%0A$a"; done
        curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
            -d "chat_id=${CHAT_ID}" -d "text=${msg}" -d "disable_notification=false" > /dev/null
        echo "[$(date '+%F %T')] ALERT sent: ${#alerts[@]} issue(s)" >> "$LOG"
    fi
else
    echo "[$(date '+%F %T')] OK" >> "$LOG"
fi
