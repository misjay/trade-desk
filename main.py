"""
main.py — Trade Desk Bot entry point.

Schedules:
  - Scalp scan: every 5 minutes (15m candles)
  - Day scan:   every 30 minutes (4h candles)
  - Desk summary push: every 60 minutes (configurable)
  - Mini HTTP server on :8765 serving state.json for dashboard

Usage:
    python main.py              # runs in current mode (from .env)
    python main.py --demo       # force demo mode
    python main.py --live       # force live mode (requires live API keys)
    python main.py --reset      # reset state.json and start fresh
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import List

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

# ── Patch mode before importing config singleton ──────────────────────────────
def _parse_args():
    p = argparse.ArgumentParser(description="Trade Desk Bot")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--demo", action="store_true", help="Force demo mode")
    group.add_argument("--live", action="store_true", help="Force live mode")
    p.add_argument("--reset", action="store_true", help="Reset state.json")
    p.add_argument("--no-telegram", action="store_true", help="Disable Telegram (local test)")
    return p.parse_args()


args = _parse_args()
if args.demo:
    os.environ["TRADE_MODE"] = "demo"
if args.live:
    os.environ["TRADE_MODE"] = "live"

# Now safe to import config and modules
from config import cfg, ALL_TICKERS, CORE_TICKERS, EXTRA_TICKERS
import state
import scanner
import chart as chart_mod
import notifier
import engine

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, cfg.log_level.upper(), logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Path(__file__).parent / "trade_desk.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("main")

# ── Patch notifier if --no-telegram ──────────────────────────────────────────
if args.no_telegram:
    def _noop(*a, **kw): pass
    notifier.send_text = _noop
    notifier.send_photo = _noop
    log.info("Telegram disabled (--no-telegram)")


# ── State init ────────────────────────────────────────────────────────────────
if args.reset:
    state.reset_state()
    log.info("State reset.")


# ── Shared last-signals store ─────────────────────────────────────────────────
_last_signals: List[dict] = []
_signals_lock = threading.Lock()


def _update_last_signals(signals: list) -> None:
    with _signals_lock:
        _last_signals.clear()
        _last_signals.extend(signals)


def _get_last_signals() -> list:
    with _signals_lock:
        return list(_last_signals)


# ── Dedup: only act on a signal if it differs from last one for that ticker ───
def _should_execute(sig: dict) -> bool:
    """
    Avoid re-emitting the same signal back-to-back.
    A signal is 'new' if side changed, or entry range shifted > 0.1%.
    """
    if sig["side"] == "WAIT":
        return False
    prev = state.get_last_signal(sig["ticker"])
    if prev is None:
        return True
    if prev["side"] != sig["side"]:
        return True
    # Check if entry range shifted significantly
    prev_mid = ((prev.get("entry_low") or 0) + (prev.get("entry_high") or 0)) / 2
    curr_mid = ((sig.get("entry_low") or 0) + (sig.get("entry_high") or 0)) / 2
    if prev_mid == 0:
        return True
    shift = abs(curr_mid - prev_mid) / prev_mid
    return shift > 0.001  # 0.1% change = new signal


# ── Scan job ──────────────────────────────────────────────────────────────────
def _run_scan(trade_type: str) -> None:
    log.info("=== %s scan started ===", trade_type.upper())
    try:
        signals = scanner.run_scan(trade_type)
        _update_last_signals(signals)
    except Exception as exc:
        log.error("Scan error: %s", exc, exc_info=True)
        return

    for sig in signals:
        try:
            state.save_signal(sig)

            if sig["side"] == "WAIT":
                # Only push WAIT cards on the desk summary, not every scan
                continue

            if not _should_execute(sig):
                log.info("Dedup: %s %s already active, skipping", sig["ticker"], sig["side"])
                continue

            # Generate chart
            chart_bytes = None
            try:
                df = scanner.fetch_ohlcv(
                    sig["ticker"],
                    15 if trade_type == "scalp" else 240,
                    limit=100,
                )
                if df is not None:
                    chart_bytes = chart_mod.generate_chart(
                        df=df,
                        ticker=sig["ticker"],
                        side=sig["side"],
                        tf=sig["tf"],
                        entry_low=sig["entry_low"],
                        entry_high=sig["entry_high"],
                        tp1=sig["tp1"],
                        tp2=sig["tp2"],
                        sl=sig["sl"],
                        live_price=sig["live_price"],
                    )
            except Exception as chart_exc:
                log.warning("Chart generation failed for %s: %s", sig["ticker"], chart_exc)

            # Send Telegram notification
            notifier.notify_signal(sig, chart_bytes)

            # Execute (paper or live)
            engine.execute_signal(sig)

        except Exception as exc:
            log.error("Error processing signal for %s: %s", sig.get("ticker"), exc, exc_info=True)

    log.info("=== %s scan complete ===", trade_type.upper())


def run_scalp_scan():
    _run_scan("scalp")


def run_day_scan():
    _run_scan("day")


# ── Hourly desk summary ───────────────────────────────────────────────────────
def send_desk_summary():
    signals = _get_last_signals()
    if not signals:
        return
    equity = state.get_equity()
    stats = state.get_session_stats()
    notifier.notify_summary(signals, equity, stats)
    log.info("Desk summary sent. Equity: %.2f", equity)


# ── Mini HTTP server (for dashboard.html) ────────────────────────────────────
_STATE_FILE = Path(__file__).parent / "state.json"


class _StateHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/state", "/state.json"):
            try:
                data = _STATE_FILE.read_bytes()
            except Exception:
                data = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/signals":
            data = json.dumps(_get_last_signals()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *a):
        pass  # silence default HTTP log noise


def _start_http_server(port: int = 8765) -> None:
    try:
        server = HTTPServer(("127.0.0.1", port), _StateHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True, name="http-srv")
        t.start()
        log.info("Dashboard state server: http://127.0.0.1:%d/state", port)
    except Exception as exc:
        log.warning("Could not start HTTP server on %d: %s", port, exc)


# ── Scheduler ─────────────────────────────────────────────────────────────────
_scheduler = BackgroundScheduler(timezone="UTC")


def _setup_scheduler() -> None:
    # Scalp: every 5 min
    _scheduler.add_job(
        run_scalp_scan,
        trigger=IntervalTrigger(seconds=cfg.scan_interval_scalp),
        id="scalp_scan",
        name="Scalp Scan (15m)",
        replace_existing=True,
        max_instances=1,
    )
    # Day: every 30 min
    _scheduler.add_job(
        run_day_scan,
        trigger=IntervalTrigger(seconds=cfg.scan_interval_day),
        id="day_scan",
        name="Day Scan (4h)",
        replace_existing=True,
        max_instances=1,
    )
    # Desk summary
    if cfg.desk_summary_interval > 0:
        _scheduler.add_job(
            send_desk_summary,
            trigger=IntervalTrigger(minutes=cfg.desk_summary_interval),
            id="desk_summary",
            name="Desk Summary",
            replace_existing=True,
        )


# ── Graceful shutdown ─────────────────────────────────────────────────────────
def _shutdown(signum=None, frame=None):
    log.info("Shutting down…")
    _scheduler.shutdown(wait=False)
    engine.stop_monitor()
    sys.exit(0)


signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    log.info("Trade Desk Bot starting — mode=%s", cfg.trade_mode.upper())

    if cfg.is_live and not cfg.binance_api_key:
        log.error("LIVE mode requires BINANCE_API_KEY. Set it in .env.")
        sys.exit(1)

    if not cfg.telegram_token and not args.no_telegram:
        log.warning("No TELEGRAM_BOT_TOKEN set — notifications disabled.")

    _start_http_server()
    engine.start_monitor()
    _setup_scheduler()
    _scheduler.start()

    notifier.send_startup_message(cfg.trade_mode)

    # Run initial scan immediately on start
    log.info("Running initial scalp scan…")
    run_scalp_scan()

    log.info("Scheduler running. Press Ctrl+C to stop.")
    try:
        while True:
            import time as _time
            _time.sleep(10)
    except (KeyboardInterrupt, SystemExit):
        _shutdown()


if __name__ == "__main__":
    main()
