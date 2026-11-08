#!/usr/bin/env python3
"""
Market Scanner: US stocks (pre-market + live) and forex pairs -> Telegram
-------------------------------------------------------------------------
Stocks : gap > 5% vs previous close, price > $3, volume (since 04:00 ET) > 50,000
         Tickers/prices from Yahoo Finance (yfinance); catalysts from Benzinga
         (falls back to Yahoo news if no key / no result)
Forex  : pairs whose move vs previous close >= FOREX_MIN_MOVE_PCT (up or down)
Modes  : premarket = volume/price counted up to 09:30 ET
         live      = volume/price counted up to now (regular session)
         auto      = premarket before 09:30 ET, otherwise live
Alerts : each symbol is alerted once per day (no repeats every 30 min).
Output : console + results/scan_YYYY-MM-DD.json (all runs of the day) + Telegram

Setup  : pip install yfinance requests pandas
Env    : BENZINGA_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
         FOREX_MIN_MOVE_PCT (default 0.5), SCANNER_OUT_DIR (default results)
Run    : python premarket_scanner.py [--mode auto|premarket|live|test]
         (test = just send a Telegram test message, works any day)
"""
import argparse
import html
import json
import os
import re
import sys
import time as _time
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

ET = ZoneInfo("America/New_York")
ICT = ZoneInfo("Asia/Bangkok")

MIN_GAP_PCT = 5.0
MIN_PRICE = 3.0
MIN_VOLUME = 50_000
FOREX_MIN_MOVE_PCT = float(os.environ.get("FOREX_MIN_MOVE_PCT", "0.5"))
FOREX_PAIRS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X", "USDCHF=X",
    "NZDUSD=X", "EURJPY=X", "GBPJPY=X", "EURGBP=X", "USDTHB=X",
]

OUT_DIR = Path(os.environ.get("SCANNER_OUT_DIR", "results"))
BENZINGA_KEY = os.environ.get("BENZINGA_API_KEY")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


# ------------------------------------------------------------ Stocks ----
def get_candidates() -> list[str]:
    """Candidate pool: Yahoo Finance day-gainers screener."""
    res = yf.screen("day_gainers", count=250)
    return sorted({q["symbol"] for q in res.get("quotes", []) if q.get("symbol")})


def stock_stats(sym: str, df, today, end_t: time) -> dict | None:
    d = df.dropna(subset=["Close"])
    if d.empty:
        return None
    idx = d.index.tz_convert(ET)
    d = d.set_axis(idx)
    dates, times = idx.date, idx.time

    cur = d[(dates == today) & (times >= time(4, 0)) & (times < end_t)]
    prev = d[(dates < today) & (times >= time(9, 30)) & (times < time(16, 0))]
    if cur.empty or prev.empty:
        return None

    prev_close = float(prev["Close"].iloc[-1])
    price = float(cur["Close"].iloc[-1])
    return {
        "ticker": sym,
        "price": round(price, 2),
        "prev_close": round(prev_close, 2),
        "gap_pct": round((price - prev_close) / prev_close * 100, 2),
        "volume": int(cur["Volume"].sum()),
    }


def scan_stocks(mode: str) -> list[dict]:
    today = datetime.now(ET).date()
    end_t = time(9, 30) if mode == "premarket" else time(16, 0)
    symbols = get_candidates()
    if not symbols:
        return []

    data = yf.download(
        symbols, period="5d", interval="1m", prepost=True,
        group_by="ticker", progress=False, threads=True,
    )
    hits = []
    for sym in symbols:
        try:
            df = data[sym] if len(symbols) > 1 else data
            s = stock_stats(sym, df, today, end_t)
        except Exception:
            continue
        if s and s["gap_pct"] > MIN_GAP_PCT and s["price"] > MIN_PRICE and s["volume"] > MIN_VOLUME:
            hits.append(s)
    return sorted(hits, key=lambda x: x["gap_pct"], reverse=True)


# ------------------------------------------------------------- Forex ----
def scan_forex() -> list[dict]:
    hits = []
    for sym in FOREX_PAIRS:
        try:
            fi = yf.Ticker(sym).fast_info
            price, prev = float(fi["last_price"]), float(fi["previous_close"])
        except Exception:
            continue
        chg = (price - prev) / prev * 100
        if abs(chg) >= FOREX_MIN_MOVE_PCT:
            name = sym.replace("=X", "")
            if len(name) == 6:
                name = f"{name[:3]}/{name[3:]}"
            digits = 3 if ("JPY" in sym or "THB" in sym) else 5
            hits.append({
                "pair": name, "price": round(price, digits),
                "prev_close": round(prev, digits), "change_pct": round(chg, 3),
            })
    return sorted(hits, key=lambda x: abs(x["change_pct"]), reverse=True)


# ----------------------------------------------------------- Catalyst ----
def _one_sentence(text: str, limit: int = 200) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    sentence = m.group(1) if m else text
    return sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…"


def benzinga_catalyst(sym: str) -> str | None:
    if not BENZINGA_KEY:
        return None
    try:
        r = requests.get(
            "https://api.benzinga.com/api/v2/news",
            params={"token": BENZINGA_KEY, "tickers": sym, "pageSize": 1,
                    "displayOutput": "abstract", "sort": "created:desc"},
            headers={"accept": "application/json"}, timeout=15,
        )
        r.raise_for_status()
        items = r.json()
        if items:
            return _one_sentence(items[0].get("title") or items[0].get("teaser"))
    except Exception as e:
        print(f"  [benzinga] {sym}: {e}", file=sys.stderr)
    return None


def yahoo_catalyst(sym: str) -> str | None:
    try:
        news = yf.Ticker(sym).news or []
        if news:
            c = news[0].get("content", news[0])
            return _one_sentence(c.get("title", ""))
    except Exception:
        pass
    return None


def add_catalysts(rows: list[dict]) -> None:
    for r in rows:
        text, src = benzinga_catalyst(r["ticker"]), "Benzinga"
        if not text:
            text, src = yahoo_catalyst(r["ticker"]), "Yahoo Finance (fallback)"
        r["catalyst"] = text or "No recent news found."
        r["catalyst_source"] = src if text else None


# ----------------------------------------------------------- Telegram ----
def build_messages(stocks: list[dict], forex: list[dict], mode: str, now: datetime) -> list[str]:
    esc = html.escape
    label = "Pre-market" if mode == "premarket" else "Live"
    header = (
        f"<b>📈 {label} Scan | {now:%Y-%m-%d %H:%M} ET ({now.astimezone(ICT):%H:%M} ICT)</b>\n"
        f"Stocks: gap &gt; {MIN_GAP_PCT}% · price &gt; ${MIN_PRICE} · vol &gt; {MIN_VOLUME:,}\n"
        f"Forex: move ≥ {FOREX_MIN_MOVE_PCT}%\n"
    )
    blocks = []
    if stocks:
        blocks.append("\n<b>STOCKS</b>\n")
        blocks += [
            f"\n<b>{esc(r['ticker'])}</b>  ${r['price']:.2f}  <b>{r['gap_pct']:+.2f}%</b>"
            f"  (Vol {r['volume']:,})\n{esc(r['catalyst'])}\n"
            for r in stocks
        ]
    if forex:
        blocks.append("\n<b>FOREX</b>\n")
        blocks += [f"{r['pair']}  {r['price']}  <b>{r['change_pct']:+.2f}%</b>\n" for r in forex]
    if not blocks:
        blocks = ["\nNo new matches."]

    messages, cur = [], header
    for b in blocks:
        if len(cur) + len(b) > 3800:
            messages.append(cur)
            cur = ""
        cur += b
    messages.append(cur)
    return messages


def send_telegram(messages: list[str]) -> bool:
    if not (TG_TOKEN and TG_CHAT_ID):
        print("Telegram skipped: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.", file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for text in messages:
        for attempt in range(1, 5):  # retry: Telegram's API is sometimes slow
            try:
                r = requests.post(
                    url,
                    json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML",
                          "disable_web_page_preview": True},
                    timeout=30,
                )
                r.raise_for_status()
                break
            except Exception as e:
                # Never print the exception for HTTP errors: it contains the URL (token).
                status = getattr(getattr(e, "response", None), "status_code", None)
                msg = f"HTTP {status}" if status else type(e).__name__
                print(f"Telegram attempt {attempt}/4 failed: {msg}", file=sys.stderr)
                if status in (400, 401, 403, 404):  # wrong token/chat: retrying won't help
                    return False
                if attempt == 4:
                    return False
                _time.sleep(5 * attempt)
    return True


# ------------------------------------------------------- Storage / main ----
def day_path(date_str: str) -> Path:
    return OUT_DIR / f"scan_{date_str}.json"


def load_day(date_str: str) -> dict:
    p = day_path(date_str)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"date": date_str, "runs": []}


def save_day(day: dict, date_str: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = day_path(date_str)
    p.write_text(json.dumps(day, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def print_report(stocks: list[dict], forex: list[dict], label: str) -> None:
    print(f"\n{label}")
    if stocks:
        print(f"\n{'Ticker':<8}{'Price':>9}{'Gap %':>9}   Catalyst")
        print("-" * 100)
        for r in stocks:
            print(f"{r['ticker']:<8}{'$' + format(r['price'], '.2f'):>9}"
                  f"{format(r['gap_pct'], '+.2f') + '%':>9}   {r['catalyst']}")
    if forex:
        print(f"\n{'Pair':<10}{'Price':>12}{'Change':>10}")
        print("-" * 32)
        for r in forex:
            print(f"{r['pair']:<10}{r['price']:>12}{format(r['change_pct'], '+.2f') + '%':>10}")
    if not stocks and not forex:
        print("No new matches.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["auto", "premarket", "live", "test"], default="auto")
    args = ap.parse_args()

    if args.mode == "test":
        ok = send_telegram(["✅ <b>Scanner test</b>\nTelegram connection works."])
        print("Test message sent." if ok else "Test message FAILED.")
        sys.exit(0 if ok else 1)

    now = datetime.now(ET)
    if now.weekday() >= 5:
        print("Weekend: markets closed, nothing to scan.")
        return
    mode = args.mode
    if mode == "auto":
        mode = "premarket" if now.time() < time(9, 30) else "live"
    date_str = now.strftime("%Y-%m-%d")

    day = load_day(date_str)
    alerted = {s for run in day["runs"] for s in run.get("new_alerts", [])}

    stocks = scan_stocks(mode)
    forex = scan_forex()
    new_stocks = [r for r in stocks if r["ticker"] not in alerted]
    new_forex = [r for r in forex if r["pair"] not in alerted]
    add_catalysts(new_stocks)

    day["runs"].append({
        "time_et": now.isoformat(timespec="seconds"),
        "mode": mode,
        "stocks": stocks,
        "forex": forex,
        "new_alerts": [r["ticker"] for r in new_stocks] + [r["pair"] for r in new_forex],
    })
    path = save_day(day, date_str)

    print_report(new_stocks, new_forex, f"{mode.upper()} SCAN | {now:%Y-%m-%d %H:%M} ET")
    print(f"\nSaved -> {path}")

    # Alert on new matches; the pre-market run always reports (even if empty).
    if new_stocks or new_forex or mode == "premarket":
        if send_telegram(build_messages(new_stocks, new_forex, mode, now)):
            print("Sent to Telegram.")


if __name__ == "__main__":
    main()
