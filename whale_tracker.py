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
    "0xda744273f80b22412417f7cfe0503f3d721f987d": {
        "label": "Wallet 1 (BTC)",
        "win_rate": "88.0%",
        "pattern": "High-frequency, BTC-focused (~35 trades/day)",
    },
    "0x362ad6209a5e904a5569f69884375809c5781d9f": {
        "label": "Wallet 2 (Multi-coin)",
        "win_rate": "86.8%",
        "pattern": "Very high-frequency, multi-coin (~118 trades/day)",
    },
    "0x0a847e73a0618e330d54e26a0789425604e76ddd": {
        "label": "Wallet 3 (ETH 25x)",
        "win_rate": "73.3%",
        "pattern": "Single-asset concentrated bet, high leverage (25x)",
    },
    "0xa559edb06548fb5d9b2066ec43320e5f9726ff95": {
        "label": "Wallet 4 (ETH 20x)",
        "win_rate": "70.4%",
        "pattern": "ETH-focused, high leverage (10-20x)",
    },
    "0xbb34960afec64f3f1cc78b0c9c342c4657021696": {
        "label": "Wallet 5 (Multi-coin)",
        "win_rate": "65.4%",
        "pattern": "Multi-coin (BTC/ETH/HYPE), moderate frequency",
    },
    "0xbc433ba7c34752448c5361e5ed3038628a1753b0": {
        "label": "Wallet 6 (Short-biased)",
        "win_rate": "59.3%",
        "pattern": "Multi-coin short-biased (BTC/ETH/SOL/BNB)",
    },
    "0xfb996bd6467d1575d338967415dc0ee72c6037f3": {
        "label": "Wallet 7 (ETH low-lev)",
        "win_rate": "55.8%",
        "pattern": "ETH-focused, low leverage (3x), high volume",
    },
    "0xc6758a779bccee1ef0190dbe8292fdf44076795d": {
        "label": "Wallet 8 (SOL/HYPE)",
        "win_rate": "55.0%",
        "pattern": "Multi-coin (SOL/HYPE), high volume",
    },
    "0x051c2e6d49cf82ebc47f08f9b85800f94fc9693c": {
        "label": "Wallet 9 (Multi-coin whale)",
        "win_rate": "51.2%",
        "pattern": "Very high volume, multi-coin, large account ($3M+ PnL)",
    },
    "0x56cd86d6ef24a3f51ce6992b7f1db751b0a0276a": {
        "label": "Wallet 10 (SOL/XRP)",
        "win_rate": "100%*",
        "pattern": "Low sample size (2 coins only) - treat with caution",
    },
    "0x099691c206cb8ceaa41d3b9e55aaf859b2d3f78d": {
        "label": "Wallet 11 (BTC/SOL)",
        "win_rate": "94.6%*",
        "pattern": "Low sample size (6 coins) - treat with caution",
    },
    "0x7ff59a95c9e0b908adfc374139008ecb9a1d126b": {
        "label": "Wallet 12 (Scalper)",
        "win_rate": "77.4%",
        "pattern": "Scalper - ~9 trades/day across 24 coins, cross margin",
    },
    "0x6f9d33c906a93ab7595417646d502a12aa50a03b": {
        "label": "Wallet 13 (Scalper, safer)",
        "win_rate": "79.9%",
        "pattern": "Scalper - ~3-4 trades/day across 13 coins, isolated margin (lower risk)",
    },
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


def fetch_fills(wallet_address: str) -> list:
    """Fetch recent trade fills (executions) for a wallet.
    Each fill includes: coin, px (execution price), sz, side, time,
    dir (e.g. 'Close Long', 'Open Short'), closedPnl (realized PnL on that fill)."""
    payload = {"type": "userFills", "user": wallet_address}
    resp = requests.post(HYPERLIQUID_API, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


def summarize_closing_fills(fills: list, since_ms: int) -> dict:
    """From a list of fills, build per-coin exit summary for fills that
    happened after `since_ms` and represent closing activity (has closedPnl)."""
    summary = {}
    for f in fills:
        try:
            f_time = int(f.get("time", 0))
        except (TypeError, ValueError):
            continue
        if f_time <= since_ms:
            continue
        closed_pnl = f.get("closedPnl")
        if closed_pnl is None or float(closed_pnl) == 0:
            continue  # this fill wasn't a closing trade
        coin = f.get("coin")
        if not coin:
            continue
        entry = summary.setdefault(coin, {"realized_pnl": 0.0, "exit_price": f.get("px"), "closed_at_ms": f_time})
        entry["realized_pnl"] += float(closed_pnl)
        # keep the latest fill's price/time as the "final" exit point
        if f_time >= entry["closed_at_ms"]:
            entry["exit_price"] = f.get("px")
            entry["closed_at_ms"] = f_time
    return summary


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


def diff_and_alert(wallet_label: str, wallet_address: str, win_rate: str, pattern: str,
                    old_positions: dict, new_positions: dict, fills_summary: dict = None):
    fills_summary = fills_summary or {}
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    short_addr = f"{wallet_address[:6]}...{wallet_address[-4:]}"
    trader_info = f"WR: `{win_rate}` | {pattern}"

    old_coins = set(old_positions.keys())
    new_coins = set(new_positions.keys())

    # New positions opened
    for coin in new_coins - old_coins:
        p = new_positions[coin]
        msg = (
            f"🟢 *NEW POSITION* — {wallet_label}\n"
            f"`{short_addr}`\n"
            f"_{trader_info}_\n\n"
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
        fill_info = fills_summary.get(coin)
        if fill_info:
            closed_time = datetime.fromtimestamp(fill_info["closed_at_ms"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            pnl = fill_info["realized_pnl"]
            pnl_sign = "🟢" if pnl >= 0 else "🔴"
            msg = (
                f"🔴 *POSITION CLOSED* — {wallet_label}\n"
                f"`{short_addr}`\n"
                f"_{trader_info}_\n\n"
                f"*{coin}* {p['side']} closed\n"
                f"Entry: `{p['entry_px']}`\n"
                f"Exit: `{fill_info['exit_price']}`\n"
                f"Realized PnL: {pnl_sign} `{pnl:,.2f}`\n"
                f"Closed at: `{closed_time}`\n\n"
                f"🕒 {ts}"
            )
        else:
            # Fallback if no matching fill data was found (e.g. fill outside lookback window)
            msg = (
                f"🔴 *POSITION CLOSED* — {wallet_label}\n"
                f"`{short_addr}`\n"
                f"_{trader_info}_\n\n"
                f"*{coin}* {p['side']} closed\n"
                f"Last known size: `{p['size']}`\n"
                f"Entry was: `{p['entry_px']}`\n"
                f"(exit price/PnL not available this run)\n\n"
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
                f"`{short_addr}`\n"
                f"_{trader_info}_\n\n"
                f"*{coin}* {p['side']} {p['leverage']}x\n"
                f"Size: `{old_size}` → `{new_size}`\n"
                f"Unrealized PnL: `{p['unrealized_pnl']}`\n\n"
                f"🕒 {ts}"
            )
            send_telegram_message(msg)


def main():
    state = load_state()
    now_ms = int(time.time() * 1000)

    for address, info in WALLETS.items():
        label = info["label"]
        win_rate = info["win_rate"]
        pattern = info["pattern"]

        try:
            raw = fetch_positions(address)
            new_positions = parse_positions(raw)
        except Exception as e:
            print(f"Error fetching positions for {label}: {e}")
            continue

        old_positions = state.get(address, {}).get("positions", {})
        # last time we successfully polled this wallet (ms). Defaults to 1 hour
        # ago on first run so we don't spam alerts for old history.
        last_ts = state.get(address, {}).get("last_ts", now_ms - 60 * 60 * 1000)

        fills_summary = {}
        try:
            fills = fetch_fills(address)
            fills_summary = summarize_closing_fills(fills, since_ms=last_ts)
        except Exception as e:
            print(f"Error fetching fills for {label}: {e}")
            # continue without fills data - close alerts will just lack exit details

        diff_and_alert(label, address, win_rate, pattern, old_positions, new_positions, fills_summary)

        state[address] = {"positions": new_positions, "last_ts": now_ms}

        time.sleep(1)  # be nice to the API between wallets

    save_state(state)


if __name__ == "__main__":
    main()
