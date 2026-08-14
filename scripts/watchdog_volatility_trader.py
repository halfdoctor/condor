#!/usr/bin/env python3
"""
Hourly Watchdog Monitor for Derive Autonomous Continuous Volatility Loop Trader.
Checks if the continuous background routine is healthy and actively pulsing heartbeats.
If healthy: Exits silently with code 0 (nothing happens).
If down / stalled (> 20 minutes without heartbeat): Dispatches immediate Telegram alert to user.
"""

import os
import sys
import json
import time
import datetime
from pathlib import Path
import requests
from dotenv import load_dotenv

# Load Condor environment variables
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

HEARTBEAT_FILE = BASE_DIR / "agents" / "derive_volatility_spread_trader" / "routines" / ".heartbeat.json"
MAX_STALE_MINUTES = 20.0  # 4x the 5-minute cadence

def send_telegram_alert(message: str) -> bool:
    """Send high-priority markdown alert to configured Telegram chat/admin."""
    token = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("ADMIN_USER_ID") or os.getenv("TELEGRAM_CHAT_ID")
    
    if not token or not chat_id:
        print(f"[ERROR] Cannot send Telegram alert: TELEGRAM_TOKEN or ADMIN_USER_ID missing in .env", file=sys.stderr)
        return False
        
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    
    try:
        resp = requests.post(url, json=payload, timeout=8)
        if resp.status_code == 200:
            print(f"[INFO] Watchdog Telegram alert successfully delivered to chat {chat_id}")
            return True
        else:
            print(f"[ERROR] Telegram API error {resp.status_code}: {resp.text}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"[ERROR] Failed to send Telegram watchdog alert: {e}", file=sys.stderr)
        return False

def check_routine_health() -> tuple[bool, str, dict]:
    """Check the heartbeat file of derive_volatility_loop_trader."""
    now_epoch = time.time()
    now_utc_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    
    if not HEARTBEAT_FILE.exists():
        return False, f"Heartbeat file does not exist ({HEARTBEAT_FILE})", {}
        
    try:
        with open(HEARTBEAT_FILE, "r") as f:
            data = json.load(f)
    except Exception as e:
        return False, f"Failed to read/parse heartbeat file: {e}", {}
        
    timestamp_epoch = data.get("timestamp_epoch")
    if not timestamp_epoch:
        return False, "Heartbeat file missing 'timestamp_epoch' field", data
        
    elapsed_seconds = now_epoch - timestamp_epoch
    elapsed_minutes = elapsed_seconds / 60.0
    
    if elapsed_minutes > MAX_STALE_MINUTES:
        return False, f"Heartbeat is stale ({elapsed_minutes:.1f} minutes old > {MAX_STALE_MINUTES} min limit)", data
        
    if data.get("status") != "healthy":
        return False, f"Heartbeat status is '{data.get('status')}' (expected 'healthy')", data
        
    return True, f"Heartbeat healthy ({elapsed_minutes:.1f}m ago)", data

def main():
    now_utc_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    is_healthy, reason, data = check_routine_health()
    
    if is_healthy:
        # Healthy: do nothing and exit 0 silently (as requested)
        print(f"[{now_utc_str}] WATCHDOG OK: derive_volatility_loop_trader is healthy. {reason}")
        sys.exit(0)
    else:
        # Unhealthy: log alert and notify Telegram user
        print(f"[{now_utc_str}] WATCHDOG CRITICAL: derive_volatility_loop_trader is DOWN/STALLED! Reason: {reason}", file=sys.stderr)
        
        last_seen = data.get("timestamp", "Never")
        spot = data.get("spot_price", "N/A")
        util = data.get("margin_utilization_pct", "N/A")
        
        alert_msg = (
            f"🚨 *[CONDOR WATCHDOG CRITICAL ALERT]* 🚨\n\n"
            f"• *Routine*: `derive_volatility_loop_trader`\n"
            f"• *Status*: ❌ *NOT RUNNING / STALLED*\n"
            f"• *Reason*: {reason}\n"
            f"• *Last Heartbeat*: `{last_seen}`\n"
            f"• *Last Known Spot*: `${spot}` | *Margin Util*: `{util}%`\n"
            f"• *Checked At*: `{now_utc_str}`\n\n"
            f"⚠️ *Action Required*: The 5-minute autonomous trading loop has stopped. Please check the Condor server."
        )
        
        send_telegram_alert(alert_msg)
        sys.exit(1)

if __name__ == "__main__":
    main()
