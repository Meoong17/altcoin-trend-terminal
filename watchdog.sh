#!/usr/bin/env bash
# Watchdog: cek umur data.json, alert via Telegram kalau stale
# Dipanggil tiap jam dari cron

DATA="/opt/altcoin-terminal/data.json"
LOG="/opt/altcoin-terminal/watchdog.log"
BOT_TOKEN="8384949144:AAGjKpyrkWAO8eAmciaXFgz_WXzRFwwev6U"
CHAT_ID="1919130571"
STALE_HOURS=7

now=$(date +%s)
file_ts=$(stat -c %Y "$DATA" 2>/dev/null || echo 0)
if [ "$file_ts" -eq 0 ]; then
    msg="⚠️ ALTCOIN WATCHDOG: data.json tidak ditemukan!"
    curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/sendMessage" \
        -d "chat_id=$CHAT_ID" -d "text=$msg" -d "disable_notification=false" > /dev/null
    echo "[$(date '+%F %T')] FILE MISSING — alert sent" >> "$LOG"
    exit 1
fi

age=$(( (now - file_ts) / 3600 ))
if [ "$age" -ge "$STALE_HOURS" ]; then
    msg="⚠️ ALTCOIN WATCHDOG: data.json stale ${age}h (threshold ${STALE_HOURS}h). Cron mungkin mati!"
    curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/sendMessage" \
        -d "chat_id=$CHAT_ID" -d "text=$msg" -d "disable_notification=false" > /dev/null
    echo "[$(date '+%F %T')] STALE ${age}h — alert sent" >> "$LOG"
else
    echo "[$(date '+%F %T')] OK ${age}h" >> "$LOG"
fi
