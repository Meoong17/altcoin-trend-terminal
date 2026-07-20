"""
Operational alerting — addresses the blind spot flagged in review: the
math-framework analysis assessed epistemic/statistical risk (unvalidated
weights, ignored correlation) but zero operational risk. A cron job
that silently stops updating is indistinguishable, from the dashboard
alone, from a live signal -- the "data > 24h old" banner only helps if
someone is actually looking at the page.

This module sends a Telegram message on:
  1. An uncaught exception during collect.py's run (wrapped at the
     entry point so it fires regardless of WHERE in the pipeline it broke)
  2. Stale data detected BEFORE overwriting data.json (previous cycle's
     generated_at is older than max_age_hours) -- catches the case where
     the collector runs "successfully" but upstream data has gone stale
     for reasons that don't raise an exception (e.g. every fallback tier
     degraded quietly)

Dormant without configuration: requires TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID. No key -> alerts are printed to stderr instead of
silently vanishing, so cron logs still capture the signal.
"""

import json
import os
import sys
import traceback

import requests

API_BASE = "https://api.telegram.org"


def _creds():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return (token, chat_id) if token and chat_id else (None, None)


def is_configured():
    return _creds() != (None, None)


def format_exception_alert(exc, context=""):
    """Pure formatter, testable without network. Truncates traceback to
    keep the message within Telegram's length limits and readable on
    mobile."""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    tb_tail = tb[-800:]
    ctx = f" ({context})" if context else ""
    return (f"\U0001F534 Altcoin Trend Terminal \u2014 collector failed{ctx}\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            f"<pre>{tb_tail}</pre>")


def format_stale_alert(prev_generated_at, age_hours, max_age_hours):
    return (f"\U0001F7E1 Altcoin Trend Terminal \u2014 data is stale\n\n"
            f"Last successful update: {prev_generated_at}\n"
            f"Age: {age_hours:.1f}h (threshold {max_age_hours}h)\n"
            f"Collector is running but upstream data hasn't refreshed \u2014 "
            f"check the fallback chain and API keys.")


def send(message, parse_mode="HTML"):
    """Best-effort send. Never raises -- an alerting failure must not
    crash the collector it's supposed to be protecting. Falls back to
    stderr when unconfigured or on any send failure."""
    token, chat_id = _creds()
    if not token:
        print(f"[alert:unconfigured] {message}", file=sys.stderr)
        return False
    try:
        r = requests.post(
            f"{API_BASE}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": parse_mode},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except (requests.RequestException, ValueError) as e:
        print(f"[alert:send_failed] {e} -- original message: {message}", file=sys.stderr)
        return False


def check_staleness(data_out_path, max_age_hours=8):
    """
    Read the EXISTING data.json (before this cycle overwrites it) and
    alert if it's already older than max_age_hours. Returns True if an
    alert was sent, False otherwise (fresh, missing, or unparseable file
    -- a missing file means first-ever run, not staleness).
    """
    from datetime import datetime, timezone
    if not os.path.exists(data_out_path):
        return False
    try:
        with open(data_out_path) as f:
            prev = json.load(f)
        gen = datetime.fromisoformat(prev["generated_at"].replace("Z", "+00:00"))
        age_h = (datetime.now(timezone.utc) - gen).total_seconds() / 3600
    except (ValueError, KeyError, OSError):
        return False
    if age_h > max_age_hours:
        send(format_stale_alert(prev["generated_at"], age_h, max_age_hours))
        return True
    return False
