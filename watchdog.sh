#!/usr/bin/env bash
# Watchdog: cek umur data.json + data integrity, alert via Telegram
# Dipanggil tiap jam dari cron

DATA="/opt/altcoin-terminal/data.json"
LOG="/opt/altcoin-terminal/watchdog.log"
BOT_TOKEN="8384949144:AAGjKpyrkWAO8eAmciaXFgz_WXzRFwwev6U"
CHAT_ID="1919130571"
STALE_HOURS=7

now=$(date +%s)
file_ts=$(stat -c %Y "$DATA" 2>/dev/null || echo 0)
alerts=()

# ── 1. Stale check ──
if [ "$file_ts" -eq 0 ]; then
    alerts+=("⚠️ FILE: data.json tidak ditemukan")
else
    age=$(( (now - file_ts) / 3600 ))
    if [ "$age" -ge "$STALE_HOURS" ]; then
        alerts+=("⚠️ STALE: data.json ${age}h (threshold ${STALE_HOURS}h)")
    fi
    echo "[$(date '+%F %T')] age=${age}h" >> "$LOG"
fi

# ── 2. Data integrity check (via python) ──
if [ -f "$DATA" ]; then
    issues=$(python3 -c "
import json,sys
try:
    d=json.load(open('$DATA'))
except Exception as e:
    print(f'PARSE: {e}')
    sys.exit(0)

lines=[]

# coins
coins=d.get('coins',{})
total=len(coins)
bad=[s for s,c in coins.items() if c.get('status')!='ok']
if bad:
    lines.append(f'COINS: {len(bad)}/{total} unavailable — {\",\".join(bad[:5])}{\"…\" if len(bad)>5 else \"\"}')

# alt_season
as_=d.get('alt_season')
if not as_:
    lines.append('ALT_SEASON: field missing')
elif as_.get('index') is None:
    lines.append(f'ALT_SEASON: index null ({as_.get(\"label\")})')

# GLF components
glf=d.get('macro',{}).get('glf_details',{})
comps=glf.get('components',{})
if not comps:
    lines.append(f'GLF: no component detail')
elif glf.get('status') not in ('ok',None) and glf.get('active_components',0)<4:
    lines.append(f'GLF: degraded ({glf.get(\"active_components\",0)}/8 components)')

# regime
rg=d.get('regime',{})
if rg.get('state')=='UNKNOWN':
    pass  # wajar di awal

if lines:
    print(' | '.join(lines))
" 2>>"$LOG")
    if [ -n "$issues" ]; then
        alerts+=("🔴 $issues")
    fi
fi

# ── 3. Send alert if any ──
if [ ${#alerts[@]} -gt 0 ]; then
    msg="ALTCOIN WATCHDOG"
    for a in "${alerts[@]}"; do msg+="%0A$a"; done
    curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/sendMessage" \
        -d "chat_id=$CHAT_ID" -d "text=$msg" -d "disable_notification=false" > /dev/null
    echo "[$(date '+%F %T')] ALERT: ${#alerts[@]} issue(s)" >> "$LOG"
else
    echo "[$(date '+%F %T')] OK" >> "$LOG"
fi
