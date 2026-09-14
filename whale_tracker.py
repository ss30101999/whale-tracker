"""
Hyperliquid Whale Tracker -> Telegram Alerts
==============================================
Tracks specific wallet addresses on Hyperliquid and posts an alert to a
Telegram channel whenever a position opens, closes, or changes size.

No API key needed for Hyperliquid (public read-only Info API).
You only need a Telegram Bot Token + Channel ID (see setup steps).

Run this script on a schedule (every 2-5 minutes) via cron / GitHub Actions.
Each run compares the current snapshot to the last saved snapshot (state.json)
and only sends a message when something actually changed.
"""

import json
import os
import time
import requests
from datetime import datetime, timezone

# ============ CONFIG - EDIT THESE ============
# Prefers environment variables (safe for GitHub Actions secrets).
# Falls back to the hardcoded strings below for local testing.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PUT_YOUR_CHANNEL_ID_OR_@username_HERE")

WALLETS = {
    "Wallet (73.3% WR, 25x ETH)": "0x0a847e73a0618e330d54e26a0789425604e76ddd",
}

STATE_FILE = "state.json"
HYPERLIQUID_API = "https://api.hyperliquid.xyz/info"
# ================================================


def fetch_positions(wallet_address: str) -> dict:
    """Fetch current open positions + account summary for a wallet."""
    payload = {"type": "clearinghouseState", "user": wallet_address}
    resp = requests.post(HYPERLIQUID_API, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


def parse_positions(raw: dict) -> dict:
    """Turn raw clearinghouseState into {coin: position_info} for easy diffing."""
    positions = {}
    for p in raw.get("assetPositions", []):
        pos = p.get("position", {})
        coin = pos.get("coin")
        if not coin:
            continue
        positions[coin] = {
            "size": pos.get("szi"),
            "entry_px": pos.get("entryPx"),
            "leverage": (pos.get("leverage") or {}).get("value"),
            "side": "Long" if float(pos.get("szi", 0)) > 0 else "Short",
            "unrealized_pnl": pos.get("unrealizedPnl"),
            "liq_px": pos.get("liquidationPx"),
        }
    return positions


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=15)
    if not r.ok:
        print(f"Telegram send failed: {r.status_code} {r.text}")


def diff_and_alert(wallet_label: str, wallet_address: str, old_positions: dict, new_positions: dict):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    short_addr = f"{wallet_address[:6]}...{wallet_address[-4:]}"

    old_coins = set(old_positions.keys())
    new_coins = set(new_positions.keys())

    # New positions opened
    for coin in new_coins - old_coins:
        p = new_positions[coin]
        msg = (
            f"🟢 *NEW POSITION* — {wallet_label}\n"
            f"`{short_addr}`\n\n"
            f"*{coin}* {p['side']} {p['leverage']}x\n"
            f"Entry: `{p['entry_px']}`\n"
            f"Size: `{p['size']}`\n"
            f"Unrealized PnL: `{p['unrealized_pnl']}`\n"
            f"Liq Price: `{p['liq_px']}`\n\n"
            f"🕒 {ts}"
        )
        send_telegram_message(msg)

    # Positions closed
    for coin in old_coins - new_coins:
        p = old_positions[coin]
        msg = (
            f"🔴 *POSITION CLOSED* — {wallet_label}\n"
            f"`{short_addr}`\n\n"
            f"*{coin}* {p['side']} closed\n"
            f"Last known size: `{p['size']}`\n\n"
            f"🕒 {ts}"
        )
        send_telegram_message(msg)

    # Existing positions - check for size change (added/reduced)
    for coin in old_coins & new_coins:
        old_size = float(old_positions[coin]["size"] or 0)
        new_size = float(new_positions[coin]["size"] or 0)
        if abs(old_size - new_size) / max(abs(old_size), 1e-9) > 0.02:  # >2% change
            direction = "increased" if abs(new_size) > abs(old_size) else "reduced"
            p = new_positions[coin]
            msg = (
                f"🟡 *POSITION {direction.upper()}* — {wallet_label}\n"
                f"`{short_addr}`\n\n"
                f"*{coin}* {p['side']} {p['leverage']}x\n"
                f"Size: `{old_size}` → `{new_size}`\n"
                f"Unrealized PnL: `{p['unrealized_pnl']}`\n\n"
                f"🕒 {ts}"
            )
            send_telegram_message(msg)


def main():
    state = load_state()

    for label, address in WALLETS.items():
        try:
            raw = fetch_positions(address)
            new_positions = parse_positions(raw)
        except Exception as e:
            print(f"Error fetching {label}: {e}")
            continue

        old_positions = state.get(address, {})
        diff_and_alert(label, address, old_positions, new_positions)
        state[address] = new_positions

        time.sleep(1)  # be nice to the API between wallets

    save_state(state)


if __name__ == "__main__":
    main()
