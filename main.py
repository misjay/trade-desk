"""
main.py — Entry point for Two-sided Crypto Trade Desk on Bybit.

Modes:
  - Demo (default): Simulated paper broker against live Bybit orderbooks / marks,
                    or Bybit Testnet / Demo UTA API if keys provided.
  - Live: Real Bybit V5 Linear & Spot execution.
  - Desk mode: Generate single live desk publication with EXECUTOR CONTRACT.
  - Execution mode: Direct ingestion and execution of BOT machine contract lines.

Usage:
  python main.py                     # run in default demo mode
  python main.py --demo              # force demo mode
  python main.py --live              # force live mode
  python main.py --desk              # print full Desk publication (Core 24 + Extras)
  python main.py --execute "BOT|..." # execute single contract line
  python main.py --reset             # reset paper equity & positions
  python main.py --no-telegram       # run without telegram
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import List

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ── Parse Arguments ─────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="Trade Desk Bot for Bybit")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--demo", action="store_true", help="Force demo mode (default)")
    group.add_argument("--live", action="store_true", help="Force live mode on Bybit")
    p.add_argument("--desk", action="store_true", help="Generate single live Desk publication and exit")
    p.add_argument("--execute", type=str, default="", help="Execute a single BOT contract line and exit")
    p.add_argument("--execute-file", type=str, default="", help="Execute contract lines from file and exit")
    p.add_argument("--test-connection", action="store_true", help="Test Bybit API connection, credentials, and wallet balance")
    p.add_argument("--reset", action="store_true", help="Reset state.json")
    p.add_argument("--no-telegram", action="store_true", help="Disable Telegram notifications")
    return p.parse_args()


args = parse_args()
if args.demo:
    os.environ["TRADE_MODE"] = "demo"
if args.live:
    os.environ["TRADE_MODE"] = "live"

from config import cfg, CORE_TICKERS, EXTRA_TICKERS, WEEKEND_MAJORS_ONLY, is_weekend_derisk_window, check_macro_news_blackout
import state
import scanner
import notifier
import engine
import telegram_bot
import call_tracker
import winz_discord

# ── Logging ─────────────────────────────────────────────────────────────────
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

if args.no_telegram or cfg.telegram_token in ("", "your_telegram_bot_token"):
    notifier.send_text = lambda *a, **k: False
    log.info("Telegram notifications inactive (set valid token in .env to enable)")

if args.reset:
    state.reset_state()
    log.info("State reset to initial equity $%.2f.", cfg.paper_equity)
    print("State reset successfully.")
    sys.exit(0)


# ── Mini HTTP Server for Dashboard ──────────────────────────────────────────
_STATE_FILE = Path(__file__).parent / "state.json"
_DASHBOARD_FILE = Path(__file__).parent / "dashboard.html"
_last_signals: List[dict] = []
_signals_lock = threading.Lock()


class _DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/dashboard", "/dashboard.html", "/index.html"):
            try:
                content = _DASHBOARD_FILE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f"Error loading dashboard: {e}".encode())
        elif self.path in ("/state", "/state.json"):
            try:
                data = _STATE_FILE.read_bytes()
            except Exception:
                data = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        elif self.path in ("/signals", "/signals.json"):
            with _signals_lock:
                data = json.dumps(_last_signals).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *a):
        pass


def _start_http_server(port: int = 8765):
    try:
        server = HTTPServer(("0.0.0.0", port), _DashboardHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True, name="http-srv")
        t.start()
        log.info("Xira Dashboard live at: http://localhost:%d", port)
    except Exception as exc:
        log.warning("Could not bind HTTP server: %s", exc)


# ── Scan & Execution Cycle ──────────────────────────────────────────────────
def run_scan_cycle(trade_type: str = "scalp", manual: bool = False):
    is_paused = state.is_paused_for_type(trade_type)
    if is_paused and not manual:
        log.info("Xira automated execution is PAUSED for %s via Telegram command. Skipping scheduled %s scan cycle.", trade_type, trade_type)
        return

    log.info("=== Running %s cycle (manual=%s, paused=%s) ===", trade_type.upper(), manual, is_paused)
    core_sigs, extra_sigs, tape = scanner.run_scan(trade_type)
    all_sigs = core_sigs + extra_sigs

    with _signals_lock:
        _last_signals.clear()
        _last_signals.extend(all_sigs)

    skipped_paused_count = 0
    executed_count = 0

    # 1. Global Daily Loss Circuit Breaker Check (Master Paper Trading Desk)
    gcb_active, gcb_reason = state.is_global_circuit_breaker_active()
    master_paper_frozen = False
    if gcb_active and not manual:
        master_paper_frozen = True
        log.warning("🚨 %s Master paper desk is FROZEN. Live user auto-trading and signal tracking will continue.", gcb_reason)

    # 2. High-Impact Macro Economic News Blackout Check (CPI, FOMC, NFP)
    macro_active, macro_reason = check_macro_news_blackout()
    if macro_active and not manual:
        log.warning("🛑 Macro News Blackout: %s Skipping %s execution.", macro_reason, trade_type)
        return

    in_weekend_derisk = is_weekend_derisk_window()
    if in_weekend_derisk:
        log.info("🛡️ Weekend Defense Mode active (Friday 21:00 -> Sunday 22:00 UTC): Restricting %s execution to A+ setups (>=88%%) on liquid leaders.", trade_type)

    # ── Speed-to-Target & Conviction Priority Ranking ────────────────────────
    # Rank actionable signals by highest conviction and closest distance to TP1
    def _speed_to_tp_score(s: dict) -> float:
        conv = float(s.get("conviction", 80.0))
        live = float(s.get("live_price", 0.0))
        tp1 = float(s.get("tp1", 0.0))
        if live > 0 and tp1 > 0:
            dist_pct = abs(tp1 - live) / live * 100.0
        else:
            dist_pct = 2.0
        # Priority index = Conviction / Distance_to_TP1 (higher is faster & higher probability)
        return conv / max(dist_pct, 0.4)

    actionable_sigs = [s for s in all_sigs if s.get("side") in ("BUY", "SELL")]
    wait_sigs = [s for s in all_sigs if s.get("side") not in ("BUY", "SELL")]

    if actionable_sigs:
        actionable_sigs.sort(key=_speed_to_tp_score, reverse=True)
        log.info(
            "Ranked %d actionable setups by Conviction & Speed-to-TP: %s",
            len(actionable_sigs),
            ", ".join(f"{s['ticker']} ({s['side']} @ {s.get('conviction', 80)}% conv, speed={_speed_to_tp_score(s):.1f})" for s in actionable_sigs)
        )

    ordered_sigs = actionable_sigs + wait_sigs

    for sig in ordered_sigs:
        ticker = sig["ticker"]
        side = sig["side"]

        # Check if asset is on Avoid list
        if ticker in state.get_avoid_list():
            log.info("Ticker %s is on Avoid list, skipping execution", ticker)
            state.save_signal(sig)
            continue

        if side == "WAIT":
            state.save_signal(sig)
            continue

        # Weekend Defense Mode Gatekeeper (Liquid leaders only, Max 4 positions, >=88% A+ conviction)
        if in_weekend_derisk:
            if ticker not in WEEKEND_MAJORS_ONLY:
                log.info("🛡️ Weekend Defense: Skipping %s %s (Weekend trading restricted to high-liquidity leaders: %s)",
                         ticker, side, ", ".join(WEEKEND_MAJORS_ONLY))
                state.save_signal(sig)
                continue
            if sig.get("conviction", 0) < 88.0:
                log.info("🛡️ Weekend Defense: Skipping %s %s (conviction %.1f%% < 88.0%% A+ weekend threshold)",
                         ticker, side, sig.get("conviction", 0))
                state.save_signal(sig)
                continue

        # Broadcast card to Telegram if fresh shelf
        prev = state.get_last_signal(ticker)
        is_fresh_shelf = True
        if prev and prev.get("side") == side:
            prev_low = prev.get("entry_low") or 0
            curr_low = sig.get("entry_low") or 0
            if prev_low and abs(curr_low - prev_low) / prev_low < 0.002:
                is_fresh_shelf = False

        if is_fresh_shelf:
            notifier.notify_signal(sig)
            if side in ("BUY", "SELL"):
                import market_research
                threading.Thread(
                    target=market_research.send_call_research_to_feedback_bot,
                    args=(sig,),
                    daemon=True,
                    name=f"research-{ticker}",
                ).start()
                if sig.get("conviction", 0) >= 90.0:
                    threading.Thread(
                        target=market_research.dispatch_instant_high_conviction_call,
                        args=(sig,),
                        daemon=True,
                        name=f"trend-alert-{ticker}",
                    ).start()

        # Decoupled Live User Auto-Trade Dispatch:
        # Evaluate setup for active Bybit auto-traders independently of Master Desk capacity.
        # User-specific risk limits (Option A caps, correlation, margin buffer, user circuit breaker)
        # are safely enforced per-user inside exchange_manager.
        try:
            import exchange_manager
            call_cfg = state.get_call_bot_config()
            c_tok = call_cfg.get("token") or cfg.call_bot_token or cfg.telegram_token
            lp = float(sig.get("live_price", 0.0) or sig.get("entry_mid", 0.0))
            sig_payload = {
                "ticker": ticker,
                "side": side,
                "live_price": lp,
                "trigger_price": lp,
                "sl": sig.get("sl"),
                "tp1": sig.get("tp1"),
                "tp2": sig.get("tp2"),
                "tf": "15m" if trade_type == "scalp" else "4h",
                "conviction": sig.get("conviction", 80),
                "score": sig.get("score") or sig.get("conviction", 80),
                "confidence": sig.get("confidence") or sig.get("conviction", 80),
                "grade": sig.get("grade") or ("A+" if sig.get("conviction", 0) >= 88 else "A"),
            }
            threading.Thread(
                target=exchange_manager.dispatch_triggered_trade_to_users,
                args=(sig_payload,),
                kwargs={"bot_token": c_tok},
                daemon=True,
                name=f"live-user-dispatch-{ticker}",
            ).start()
        except Exception as exc:
            log.warning("Could not dispatch signal %s to live users: %s", ticker, exc)

        # ── Master Trade Desk Execution ───────────────────────────────────
        # Check if Master Desk execution is paused
        if is_paused:
            skipped_paused_count += 1
            log.info("Bot is PAUSED: skipping Master Desk order for %s %s. Use /resume to enable execution.", ticker, side)
            state.save_signal(sig)
            continue

        if master_paper_frozen:
            log.info("Master Paper Desk frozen by circuit breaker: Skipping master demo order for %s %s.", ticker, side)
            state.save_signal(sig)
            continue

        # Check if an active open position already exists for this ticker on Master Desk
        open_pos = state.get_open_positions()
        if has_active_pos := any(p.get("ticker") == ticker for p in open_pos.values()):
            log.info("Active position already open on Master Desk for %s, skipping master order", ticker)
            state.save_signal(sig)
            continue

        # Check Master Desk max concurrent positions cap
        effective_max_pos = 4 if in_weekend_derisk else cfg.max_concurrent_positions
        if len(open_pos) >= effective_max_pos:
            sig_conv = float(sig.get("conviction", 80.0) or 80.0)
            if sig_conv >= 88.0:
                evicted = engine.evict_weakest_working_order(sig_conv, min_gap=6.0)
                if evicted:
                    open_pos = state.get_open_positions()
            if len(open_pos) >= effective_max_pos:
                log.info("Master Desk active positions at cap (%d/%d). Skipping %s master order.",
                         len(open_pos), effective_max_pos, ticker)
                state.save_signal(sig)
                continue

        # Hummingbot Inventory Skew & Net Directional Exposure Balancing for Master Desk
        skew_ok, skew_reason = engine.check_portfolio_inventory_skew(side)
        if not skew_ok:
            log.info("Master Desk inventory skew cap reached: Skipping master order for %s %s (%s)", ticker, side, skew_reason)
            state.save_signal(sig)
            continue

        # Narrative & Ecosystem Sector Basket Exposure Cap for Master Desk
        sector_ok, sector_reason = engine.check_sector_basket_exposure(ticker)
        if not sector_ok:
            log.info("Master Desk sector basket cap reached: Skipping master order for %s (%s)", ticker, sector_reason)
            state.save_signal(sig)
            continue

        # Execute order for Master Desk
        res = engine.execute_signal(sig)
        if res.get("status") == "SUCCESS":
            executed_count += 1
        log.info("Master Desk execution result for %s: %s", ticker, res)

        # Save to state store after execution
        state.save_signal(sig)

    log.info("=== %s cycle complete (executed=%d, skipped_paused=%d) ===", trade_type.upper(), executed_count, skipped_paused_count)

    if manual:
        if is_paused:
            pause_note = f"\n\n⏸ *Execution is PAUSED.* ({skipped_paused_count} setups analyzed, 0 orders placed).\n_Bot will remain paused until you type `/resume`._"
        else:
            pause_note = f"\n\n🟢 *Execution is ACTIVE.* ({executed_count} orders placed)."
        notifier.send_text(f"🔍 *Manual {trade_type.upper()} Scan Complete!*\nScanned Institutional Leaders and Extras.{pause_note}")


# ── Scheduler Setup ─────────────────────────────────────────────────────────
_scheduler = BackgroundScheduler(timezone="UTC")


def _setup_scheduler():
    _scheduler.add_job(
        lambda: run_scan_cycle("scalp"),
        trigger=IntervalTrigger(seconds=cfg.scan_interval_scalp),
        id="scalp_scan",
        name="Scalp Scan (15m)",
        replace_existing=True,
    )
    _scheduler.add_job(
        lambda: run_scan_cycle("day"),
        trigger=IntervalTrigger(seconds=cfg.scan_interval_day),
        id="day_scan",
        name="Day Scan (4h)",
        replace_existing=True,
    )
    import learning_engine
    _scheduler.add_job(
        lambda: learning_engine.send_daily_feedback(),
        trigger=IntervalTrigger(hours=24),
        id="daily_learning_feedback",
        name="Daily Learning Feedback (24h)",
        replace_existing=True,
    )
    import market_research
    _scheduler.add_job(
        lambda: market_research.dispatch_hourly_calls(),
        trigger=IntervalTrigger(hours=2),
        id="hourly_calls_dispatch",
        name="2-Hour A+ Calls Dispatch (Max 3 Quality Signals)",
        replace_existing=True,
    )
    from apscheduler.triggers.cron import CronTrigger
    _scheduler.add_job(
        lambda: notifier.send_daily_recap(),
        trigger=CronTrigger(hour=11, minute=59, timezone="UTC"),
        id="daily_recap_1159",
        name="Daily Recap (11:59 AM UTC)",
        replace_existing=True,
    )
    if cfg.enable_daily_pruning:
        _scheduler.add_job(
            run_daily_pruning_job,
            trigger=IntervalTrigger(hours=24),
            id="daily_pruning_matrix",
            name="Daily Ticker Pruning & Quarantine Matrix (24h)",
            replace_existing=True,
        )
    import daily_intel
    _scheduler.add_job(
        lambda: daily_intel.execute_daily_1am_wat_routine(),
        trigger=CronTrigger(hour=0, minute=0, timezone="UTC"),  # 00:00 UTC == 1:00 AM WAT
        id="daily_1am_wat_intel",
        name="Daily Debug, Learn & Backtest (1:00 AM WAT)",
        replace_existing=True,
    )
    import dynamic_screener
    _scheduler.add_job(
        lambda: dynamic_screener.run_opportunistic_scan_and_dispatch(),
        trigger=IntervalTrigger(minutes=15),
        id="opportunistic_radar_sweep",
        name="Whole-Exchange Opportunistic Radar Sweep (15m)",
        replace_existing=True,
    )


def run_daily_pruning_job():
    """
    Automated Daily Ticker Pruning:
    Runs backtester across core & active universe over the past 150 candles (~37 hours of 15m data).
    If any ticker has Profit Factor < 0.90 with >= 2 trades, automatically quarantines the ticker
    to avoid trading bleeding assets and alerts the desk via Telegram.
    """
    log.info("Running daily ticker pruning and performance audit...")
    try:
        from backtester import XiraBacktester
        bt = XiraBacktester()
        res = bt.run_all(limit=150)
        underperformers = res.get("underperformers", [])
        if underperformers:
            log.warning("Daily Pruning Matrix identified underperforming assets: %s", underperformers)
            for t in underperformers:
                state.quarantine_asset(t, hours=24.0, reason="Daily Pruner: Profit Factor < 0.90")
            notifier.send_text(
                f"🛡 *Automated Daily Pruner Alert*\n"
                f"Backtest matrix quarantined underperforming assets for 24h:\n"
                f"• *Quarantined*: {', '.join(underperformers)}\n"
                f"• *Cash Cows*: {', '.join(res.get('cash_cows', [])[:5])}\n"
                f"Bot will avoid taking new positions on these names until structural edge returns."
            )
        else:
            log.info("Daily Pruning Matrix: All assets performing within risk parameters.")
    except Exception as exc:
        log.error("Daily pruning job failed: %s", exc)


# ── One-shot Desk Report ────────────────────────────────────────────────────
def print_live_desk_report():
    """Run live scan and output formatted desk report to stdout."""
    core_sigs, extra_sigs, tape = scanner.run_scan("scalp")
    report = notifier.format_desk_report(core_sigs, extra_sigs, tape, include_executor_contract=True)
    print(report)


# ── Shutdown ────────────────────────────────────────────────────────────────
def _shutdown(signum=None, frame=None):
    log.info("Shutting down Trade Desk...")
    telegram_bot.stop_telegram_listener()
    call_tracker.stop_call_tracker()
    winz_discord.stop_discord_bot()
    _scheduler.shutdown(wait=False)
    engine.stop_monitor()
    sys.exit(0)


signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    if args.test_connection:
        print("=" * 60)
        print("Bybit API Connection Diagnostic")
        print("=" * 60)
        conn = engine.client.check_connection()
        print(f"Status:       {conn.get('status')}")
        print(f"Target URL:   {conn.get('base_url')}")
        print(f"Mode:         {conn.get('mode', cfg.trade_mode)}")
        print(f"Environment:  {conn.get('demo_env', cfg.effective_demo_env)}")
        print(f"API Key:      {conn.get('masked_key', 'None / Placeholder')}")
        print(f"Message:      {conn.get('message')}")
        if conn.get("equity_usdt") is not None and conn.get("equity_usdt") > 0:
            print(f"USDT Equity:  ${conn.get('equity_usdt', 0.0):,.2f}")
            print(f"Available:    ${conn.get('available_usdt', 0.0):,.2f}")
        if conn.get("permissions"):
            print(f"Permissions:  {json.dumps(conn.get('permissions'))}")
        print("=" * 60)
        return

    if args.desk:
        print_live_desk_report()
        return

    if args.execute:
        print(f"Executing contract line: {args.execute}")
        res = engine.parse_and_execute_contract_line(args.execute)
        print(f"Result: {json.dumps(res, indent=2)}")
        return

    if args.execute_file:
        path = Path(args.execute_file)
        if not path.exists():
            print(f"File not found: {path}")
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            line = line.strip()
            if line.startswith("BOT|"):
                print(f"Executing: {line}")
                res = engine.parse_and_execute_contract_line(line)
                print(f"Result: {json.dumps(res, indent=2)}")
        return

    log.info("Starting Xira Trade Desk Bot — Mode: %s (Environment: %s)", cfg.trade_mode.upper(), cfg.effective_demo_env)
    log.info("Risk per trade: 0.5%% balance risk.")

    _start_http_server()
    engine.start_monitor()
    telegram_bot.start_telegram_listener(scan_trigger_fn=lambda: run_scan_cycle("scalp", manual=True))
    call_tracker.start_call_tracker()
    winz_discord.start_discord_bot()
    _setup_scheduler()
    _scheduler.start()

    # Dispatch initial batch of Winz hourly calls and spot setups on launch (if >= 2h since last dispatch)
    def _delayed_initial_calls():
        time.sleep(3)
        try:
            import market_research
            last_disp = state.get_last_dispatch_time()
            elapsed = time.time() - last_disp
            if last_disp > 0 and elapsed < 7200:
                log.info("Skipping initial 2-hour call dispatch on startup (last dispatch was %.1f minutes ago)", elapsed / 60.0)
                return
            log.info("Dispatching initial batch of Winz hourly calls & spot trades...")
            market_research.dispatch_hourly_calls()
        except Exception as exc:
            log.warning("Initial calls dispatch failed: %s", exc)

    threading.Thread(target=_delayed_initial_calls, daemon=True, name="init-calls").start()

    # Initial scan for automated execution
    run_scan_cycle("scalp")

    log.info("Xira Trade Desk Bot is running. Press Ctrl+C to terminate.")
    while True:
        try:
            time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            _shutdown()
            break
        except Exception as exc:
            log.exception("Unexpected exception in main loop: %s", exc)
            time.sleep(5)


if __name__ == "__main__":
    main()
