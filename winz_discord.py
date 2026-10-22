"""
winz_discord.py — Dedicated Discord Bot for Winz Trade Desk.
Supports:
  1. Interactive Discord Menus & Buttons for 10 Hourly Calls (6 Scalps + 4 Day).
  2. On-demand dynamic Candlestick Chart generation when users click an asset.
  3. Commands:
     - !calls / /calls  → Displays the 10 hourly calls with an interactive Dropdown Menu.
     - !research <COIN> → Generates live institutional research note + chart.
     - !tweet <COIN>    → Instant 280-char Twitter post ready to copy.
     - !tracked         → View live status of active tracked calls (TPs, SLs, gains).
     - !help            → Winz Discord Command & Guide Menu.
  4. Real-time broadcast channel for Milestone Alerts (TP1, TP2, SL, +10% gain, -20% drop).
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import threading
import time
from typing import Optional

try:
    import discord
    from discord.ext import commands
    HAS_DISCORD = True
except ImportError:
    discord = None
    commands = None
    HAS_DISCORD = False

from config import cfg, CORE_TICKERS, EXTRA_TICKERS
from scanner import fmt_dollar
import state

log = logging.getLogger("winz_discord")

# Discord intents setup (default unprivileged intents so bot connects instantly)
if HAS_DISCORD:
    intents = discord.Intents.default()
    intents.message_content = False
    bot = commands.Bot(command_prefix=["!", "/"], intents=intents, help_command=None)
else:
    intents = None
    bot = None

_discord_loop: Optional[asyncio.AbstractEventLoop] = None


# ── Interactive Select Dropdown for Calls ───────────────────────────────────
class CallSelectView(discord.ui.View):
    def __init__(self, calls: list[dict]):
        super().__init__(timeout=3600)
        self.calls_map = {f"{c['ticker']}_{c.get('tf', '15m')}": c for c in calls}

        options = []
        for c in calls[:25]:
            t = c["ticker"]
            side = c["side"]
            tf = c.get("tf", "15m")
            is_spot = c.get("trade_type") == "spot" or tf in ("spot", "1D")
            emoji = "💎" if is_spot else ("⚡" if tf == "15m" else "🏛")
            type_label = "Spot" if is_spot else tf
            label = f"{t} ({type_label} {side})"
            el = fmt_dollar(c.get("entry_low"))
            eh = fmt_dollar(c.get("entry_high"))
            desc = f"Entry: {el}–{eh} | TP1: {fmt_dollar(c.get('tp1'))}"
            cb_val = f"{t}_spot" if is_spot else f"{t}_{tf}"
            options.append(discord.SelectOption(label=label, value=cb_val, description=desc[:100], emoji=emoji))

        if options:
            select = discord.ui.Select(
                placeholder="👇 Select any asset to view its Live Chart & Analysis...",
                min_values=1,
                max_values=1,
                options=options,
            )
            select.callback = self.select_callback
            self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        selected_key = interaction.data["values"][0]
        ticker, tf_str = selected_key.split("_")
        is_spot = tf_str.lower() in ("spot", "1d")
        tf_min = 1440 if is_spot else (240 if tf_str == "4h" else 15)

        import market_research
        import chart
        from scanner import fetch_ohlcv

        if is_spot:
            sig = market_research.build_spot_signal(ticker)
        else:
            sig = market_research.build_signal_for_timeframe(ticker, tf_minutes=tf_min)
        note = market_research.generate_market_research(sig)

        df = fetch_ohlcv(ticker, tf_minutes=tf_min, limit=80)
        chart_tf = "Spot" if is_spot else tf_str
        png_bytes = chart.generate_chart(
            df=df,
            ticker=ticker,
            side=sig.get("side", "BUY"),
            tf=chart_tf,
            entry_low=sig.get("entry_low"),
            entry_high=sig.get("entry_high"),
            tp1=sig.get("tp1"),
            tp2=sig.get("tp2"),
            sl=sig.get("sl"),
            live_price=sig.get("live_price"),
        )

        files = []
        if png_bytes and len(png_bytes) > 1000:
            files.append(discord.File(io.BytesIO(png_bytes), filename=f"{ticker}_{chart_tf}_chart.png"))

        title_badge = "💎 Winz Spot Accumulation" if is_spot else "🔬 Winz Research"
        embed = discord.Embed(
            title=f"{title_badge}: {ticker}/USDT ({chart_tf} {sig.get('side')})",
            description=note[:4000],
            color=0x2ECC71 if is_spot else (0x00FF88 if sig.get("side") == "BUY" else 0xFF3366),
        )
        if files:
            embed.set_image(url=f"attachment://{ticker}_{chart_tf}_chart.png")

        await interaction.followup.send(embed=embed, files=files)


# ── Shared Command Builders ──────────────────────────────────────────────────
def _build_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🤖 Winz Trade Desk — Discord Control & Signals",
        description="Winz provides automated institutional market calls, charts, and real-time trade tracking.",
        color=0x5865F2,
    )
    embed.add_field(name="🎯 `/calls` or `!calls`", value="Display the latest 15 Hourly Calls (6 Scalps + 4 Day + 5 Spot) with an interactive chart dropdown.", inline=False)
    embed.add_field(name="💎 `/spot` or `!spot`", value="Display the 5 Spot Accumulation setups with interactive chart dropdown.", inline=False)
    embed.add_field(name="🔬 `/research <COIN>`", value="Generate deep institutional thesis, levels & live chart (e.g. `/research SOL` or `/research SOL spot`).", inline=False)
    embed.add_field(name="🐦 `/tweet [COIN]`", value="Convert call setup into a 280-char Twitter/X post ready to copy.", inline=False)
    embed.add_field(name="📋 `/tracked`", value="Show live status of all active tracked calls (TPs, SLs, gains).", inline=False)
    embed.add_field(name="⚡ `/setchannel`", value="Set the current channel as the destination for automated hourly calls & milestone alerts.", inline=False)
    embed.set_footer(text="Winz Trade Desk • Non-Automated Manual Execution Signals")
    return embed


def _set_channel_id(cid: str) -> str:
    s = state._load_raw()
    s["discord_channel_id"] = str(cid)
    state._save(s)
    return str(cid)


def _build_calls_message(signals: Optional[list] = None) -> tuple[discord.Embed, CallSelectView]:
    import market_research

    if signals is None:
        signals = market_research.get_scheduled_2h_calls()

    all_calls = signals
    state.save_tracked_calls(all_calls)

    scalp_sigs = [s for s in all_calls if s.get("tf") in ("15m", "15")]
    day_sigs = [s for s in all_calls if s.get("tf") not in ("15m", "15")]

    embed = discord.Embed(
        title="🚨 WINZ 2-HOUR TRADE DESK (4 SCALPS + 3 DAY TRADES)",
        description="**For Manual / External Exchange Trading**\nSelect any asset from the dropdown below to view its live chart markup and institutional research note.",
        color=0x00FF88,
    )

    scalp_text = []
    for idx, s in enumerate(scalp_sigs, 1):
        t, side = s["ticker"], s["side"]
        conv = s.get("conviction", 90.0)
        el, eh = fmt_dollar(s["entry_low"]), fmt_dollar(s["entry_high"])
        tp1, sl, rr = fmt_dollar(s["tp1"]), fmt_dollar(s["sl"]), s.get("rr", 2.0)
        tp1_pct = s.get("tp1_pct", 2.4)
        sl_pct = s.get("sl_pct", 1.2)
        scalp_text.append(f"**{idx}. `[{conv:.0f}% Conviction]` ${t}** `{side}` — Entry: `{el}–{eh}` | TP: `+{tp1_pct:.1f}%` ({tp1}) | SL: `-{sl_pct:.1f}%` ({sl}) (R:R {rr:.1f})")

    day_text = []
    for idx, s in enumerate(day_sigs, len(scalp_sigs) + 1):
        t, side = s["ticker"], s["side"]
        conv = s.get("conviction", 92.0)
        el, eh = fmt_dollar(s["entry_low"]), fmt_dollar(s["entry_high"])
        tp1, sl, rr = fmt_dollar(s["tp1"]), fmt_dollar(s["sl"]), s.get("rr", 2.0)
        tp1_pct = s.get("tp1_pct", 6.5)
        sl_pct = s.get("sl_pct", 2.8)
        day_text.append(f"**{idx}. `[{conv:.0f}% Conviction]` ${t}** `{side}` — Entry: `{el}–{eh}` | TP: `+{tp1_pct:.1f}%` ({tp1}) | SL: `-{sl_pct:.1f}%` ({sl}) (R:R {rr:.1f})")

    if scalp_text:
        embed.add_field(name="⚡ 4 SCALP CALLS (15m Timeframe)", value="\n".join(scalp_text), inline=False)
    if day_text:
        embed.add_field(name="🏛 3 DAY TRADE CALLS (4h Timeframe)", value="\n".join(day_text), inline=False)

    view = CallSelectView(all_calls)
    return embed, view


def _build_spot_message() -> tuple[discord.Embed, CallSelectView]:
    from config import CORE_TICKERS, EXTRA_TICKERS
    import market_research

    candidate_universe = CORE_TICKERS + EXTRA_TICKERS
    spot_sigs = []
    for t in candidate_universe:
        if len(spot_sigs) >= 5:
            break
        sig = market_research.build_spot_signal(t)
        if sig:
            spot_sigs.append(sig)

    state.save_tracked_calls(spot_sigs)

    embed = discord.Embed(
        title="💎 WINZ SPOT ACCUMULATION DESK (5 TRADES)",
        description="**For Manual / External Exchange Spot Trading**\nSelect any asset from the dropdown below to view its Spot chart markup and institutional thesis.",
        color=0x2ECC71,
    )

    spot_text = []
    for idx, s in enumerate(spot_sigs, 1):
        t = s["ticker"]
        conv = s.get("conviction", 94.0)
        el, eh = fmt_dollar(s["entry_low"]), fmt_dollar(s["entry_high"])
        tp1, tp2, sl, rr = fmt_dollar(s["tp1"]), fmt_dollar(s["tp2"]), fmt_dollar(s["sl"]), s.get("rr", 2.5)
        tp1_pct = s.get("tp1_pct", 14.0)
        tp2_pct = s.get("tp2_pct", 32.0)
        sl_pct = s.get("sl_pct", 7.0)
        spot_text.append(f"**{idx}. `[{conv:.0f}% Conviction]` ${t}** `BUY` — Accumulate: `{el}–{eh}` | TP1: `+{tp1_pct:.1f}%` ({tp1}) | TP2: `+{tp2_pct:.1f}%` ({tp2}) | SL: `-{sl_pct:.1f}%` ({sl}) (R:R {rr:.1f})")

    embed.add_field(name="💎 5 SPOT SWING SETUPS (1D Macro Accumulation)", value="\n".join(spot_text) if spot_text else "None", inline=False)
    view = CallSelectView(spot_sigs)
    return embed, view


def _build_research_message(ticker: str) -> tuple[discord.Embed, list[discord.File]]:
    raw = ticker.upper().replace("USDT", "").replace("$", "").strip()
    is_spot = "SPOT" in raw or "1D" in raw
    t_clean = raw.replace("SPOT", "").replace("1D", "").strip() or "BTC"

    import market_research
    import chart
    from scanner import fetch_ohlcv

    if is_spot:
        sig = market_research.build_spot_signal(t_clean)
        tf_label = "Spot"
        tf_min = 1440
    else:
        sig = market_research.build_signal_for_timeframe(t_clean, tf_minutes=15)
        tf_label = "15m"
        tf_min = 15

    note = market_research.generate_market_research(sig)

    df = fetch_ohlcv(t_clean, tf_minutes=tf_min, limit=80)
    png_bytes = chart.generate_chart(
        df=df,
        ticker=t_clean,
        side=sig.get("side", "BUY"),
        tf=tf_label,
        entry_low=sig.get("entry_low"),
        entry_high=sig.get("entry_high"),
        tp1=sig.get("tp1"),
        tp2=sig.get("tp2"),
        sl=sig.get("sl"),
        live_price=sig.get("live_price"),
    )

    files = []
    if png_bytes and len(png_bytes) > 1000:
        files.append(discord.File(io.BytesIO(png_bytes), filename=f"{t_clean}_{tf_label}_chart.png"))

    title_badge = "💎 Winz Spot Accumulation" if is_spot else "📊 Market Research"
    embed = discord.Embed(
        title=f"{title_badge}: {t_clean}/USDT ({tf_label} {sig.get('side')})",
        description=note[:4000],
        color=0x2ECC71 if is_spot else (0x00FF88 if sig.get("side") == "BUY" else 0xFF3366),
    )
    if files:
        embed.set_image(url=f"attachment://{t_clean}_{tf_label}_chart.png")

    return embed, files


def _build_tweet_message(ticker: str) -> str:
    t_clean = ticker.upper().replace("USDT", "").replace("$", "") if ticker else "BTC"
    import market_research
    sig = market_research.build_signal_for_timeframe(t_clean, tf_minutes=15)
    tweet = market_research.format_twitter_post(sig)
    return f"🐦 **Twitter / X Post ({len(tweet)}/280 chars):**\n```\n{tweet}\n```"


def _build_tracked_embed() -> discord.Embed:
    tracked = state.get_tracked_calls()
    embed = discord.Embed(
        title="📋 Winz Active Tracked Calls Desk",
        description="Tracking active triggered trades, pending limit bands, and milestone targets against live Bybit prices.",
        color=0xFEE75C,
    )
    if not tracked:
        embed.description = "ℹ️ No active tracked calls in memory right now. Run `/calls` to generate fresh setups!"
        return embed

    from scanner import fetch_live_price
    active_lines = []
    pending_lines = []

    for cid, c in list(tracked.items()):
        if c.get("is_closed") or c.get("status") in ("CLOSED_TP2", "CLOSED_SL", "CLOSED_BREAKEVEN", "EXPIRED"):
            continue
        t = c["ticker"]
        side = c["side"]
        tf = c.get("tf", "15m")
        is_spot = c.get("trade_type") == "spot" or tf in ("spot", "1D")
        badge = "💎 Spot" if is_spot else tf
        lp = fetch_live_price(t) or float(c.get("live_price") or 0.0)
        el = float(c.get("entry_low") or 0.0)
        eh = float(c.get("entry_high") or 0.0)
        emid = (el + eh) / 2.0 if (el and eh) else lp

        is_active = c.get("entry_filled") or c.get("status") == "ACTIVE"
        if is_active:
            pnl_pct = ((lp - emid) / emid * 100.0) if side == "BUY" else ((emid - lp) / emid * 100.0) if emid > 0 else 0.0
            pnl_badge = f"+{pnl_pct:.1f}% 🟢" if pnl_pct >= 0 else f"{pnl_pct:.1f}% 🔴"
            tp1_stat = "✅ TP1" if c.get("tp1_hit") else f"TP1: {fmt_dollar(c.get('tp1'))}"
            rev_note = f" | ⚠️ _{c.get('reversal_reason')}_" if c.get("reversal_reason") else ""
            active_lines.append(f"🟢 **${t}** ({badge} `{side}`) — Triggered @ `{fmt_dollar(emid)}` | Mark: `{fmt_dollar(lp)}` | PnL: `{pnl_badge}` | {tp1_stat}{rev_note}")
        else:
            dist_pct = abs(lp - emid) / emid * 100.0 if emid > 0 else 0.0
            pending_lines.append(f"⏳ **${t}** ({badge} `{side}`) — Entry: `{fmt_dollar(el)}–{fmt_dollar(eh)}` | Mark: `{fmt_dollar(lp)}` ({dist_pct:.1f}% away)")

    desc_parts = []
    if active_lines:
        desc_parts.append("**🟢 ACTIVE TRADES (Triggered & Running):**\n" + "\n".join(active_lines[:10]))
    if pending_lines:
        desc_parts.append("**⏳ PENDING SETUPS (Waiting for Entry):**\n" + "\n".join(pending_lines[:10]))

    if not desc_parts:
        embed.description = "ℹ️ All tracked trades have finished. Type `/calls` for fresh setups!"
    else:
        embed.description = "\n\n".join(desc_parts)
    return embed


# ── Events & Discord Commands ───────────────────────────────────────────────
@bot.event
async def on_ready():
    global _discord_loop
    _discord_loop = asyncio.get_running_loop()
    try:
        synced = await bot.tree.sync()
        log.info("Synced %d Discord slash commands.", len(synced))
    except Exception as exc:
        log.warning("Could not sync Discord slash commands: %s", exc)
    log.info("Winz Discord Bot is online as %s (ID: %s)", bot.user.name, bot.user.id)
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="15 Hourly Crypto Calls | /calls"))


# Slash Commands
@bot.tree.command(name="help", description="Winz Discord Help & Guide")
async def slash_help(interaction: discord.Interaction):
    await interaction.response.send_message(embed=_build_help_embed())


@bot.tree.command(name="setchannel", description="Set this channel for automated calls & milestone alerts")
async def slash_setchannel(interaction: discord.Interaction):
    cid = _set_channel_id(str(interaction.channel_id))
    await interaction.response.send_message(f"✅ **Winz Channel Set!** Automated hourly calls and trade milestone alerts will broadcast here: <#{cid}>.")


@bot.tree.command(name="calls", description="Display the 15 hourly calls (6 Scalps + 4 Day + 5 Spot) with interactive asset dropdown")
async def slash_calls(interaction: discord.Interaction):
    await interaction.response.defer()
    embed, view = _build_calls_message()
    await interaction.followup.send(embed=embed, view=view)


@bot.tree.command(name="spot", description="Display 5 spot accumulation setups with interactive asset dropdown")
async def slash_spot(interaction: discord.Interaction):
    await interaction.response.defer()
    embed, view = _build_spot_message()
    await interaction.followup.send(embed=embed, view=view)


@bot.tree.command(name="research", description="Generate live institutional research & chart for a crypto asset")
async def slash_research(interaction: discord.Interaction, ticker: str = "BTC"):
    await interaction.response.defer()
    embed, files = _build_research_message(ticker)
    await interaction.followup.send(embed=embed, files=files)


@bot.tree.command(name="tweet", description="Generate 280-char Twitter/X post for an asset")
async def slash_tweet(interaction: discord.Interaction, ticker: str = "BTC"):
    await interaction.response.send_message(_build_tweet_message(ticker))


@bot.tree.command(name="dna", description="View Master Asset DNA, unlock dates & macro levels (e.g. /dna DOGE)")
async def slash_dna(interaction: discord.Interaction, ticker: str = "DOGE"):
    await interaction.response.defer()
    target_t = ticker.upper().replace("USDT", "")
    import asset_dna
    from notifier import fmt_dollar
    from config import bybit_linear_symbol
    dna = asset_dna.get_asset_dna(target_t)
    name = dna.get("name", target_t)
    arch = dna.get("archetype", "UNKNOWN")
    buf_pct = dna.get("min_stop_buffer", 0.0075) * 100.0
    run_st = dna.get("run_style", "N/A")
    tr_mode = dna.get("trail_mode", "N/A")
    unl_risk = dna.get("unlock_risk", False)
    unl_days = dna.get("monthly_unlock_days", [])
    unl_str = f"⚠️ High Cliff Risk (Days {unl_days})" if unl_risk else "✅ Clean (No Cliff Risk)"
    burn = "🔥 Active Supply Burn" if dna.get("burn_catalyst") else "None"
    sess = dna.get("best_session", "ALL")
    desc = dna.get("description", "")

    macro_desc = ""
    try:
        import engine
        sym = bybit_linear_symbol(target_t)
        tick = engine.client.get_ticker(sym)
        if tick:
            l_price = float(tick.get("mark_price", 0.0))
            macro_desc = f"\n**Live Mark Price:** `{fmt_dollar(l_price)}`"
            klines = engine.client.get_klines(sym, interval="D", limit=30, category="linear")
            if klines and len(klines) >= 15:
                h30 = max(float(k.get("high", 0)) for k in klines)
                l30 = min(float(k.get("low", 0)) for k in klines)
                macro_desc += f"\n**30D Major Ceiling:** `{fmt_dollar(h30)}`\n**30D Major Floor:** `{fmt_dollar(l30)}`"
    except Exception:
        pass

    embed = discord.Embed(
        title=f"🧬 Asset DNA Master Profile: {target_t} ({name})",
        color=0x2ECC71 if not unl_risk else 0xE67E22,
    )
    embed.add_field(name="Archetype", value=f"`{arch}`", inline=True)
    embed.add_field(name="Stop Noise Cushion", value=f"`≥ {buf_pct:.2f}%`", inline=True)
    embed.add_field(name="Trailing Mode", value=f"`{tr_mode}`", inline=True)
    embed.add_field(name="Movement Style", value=f"`{run_st}`", inline=True)
    embed.add_field(name="Tokenomics & Unlocks", value=unl_str, inline=True)
    embed.add_field(name="Supply Burn", value=f"`{burn}`", inline=True)
    embed.add_field(name="Key Levels & Price", value=macro_desc if macro_desc else "Active", inline=False)
    embed.add_field(name="Specialist Playbook", value=f"*{desc}*", inline=False)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="compound", description="Configure dynamic balance compounding (e.g. 1.0% or 0 to disable)")
async def slash_compound(interaction: discord.Interaction, pct: Optional[float] = None):
    await interaction.response.defer()
    cid = str(interaction.user.id)
    u = user_store.get_user(cid)
    ex = u.get("exchange", {})
    curr_pct = user_store.get_user_compounding_pct(cid)

    bal_eq = float(u.get("paper_equity", 10000.0))
    import exchange_manager
    client = exchange_manager.get_user_bybit_client(cid)
    if client:
        try:
            wb = client.get_wallet_balance("USDT")
            bal_eq = float(wb.get("equity", 0.0))
        except Exception:
            pass

    if pct is not None:
        if pct <= 0:
            user_store.set_user_compounding_pct(cid, 0.0)
            fixed_margin = float(ex.get("risk_per_trade_usdt", 50.0))
            embed = discord.Embed(
                title="⏸ Dynamic Compounding Disabled",
                description=f"Auto-trade sizing reverted to your fixed margin of **${fixed_margin:.2f} USDT** per trade.",
                color=0x95A5A6,
            )
            await interaction.followup.send(embed=embed)
            return
        if pct > 10.0:
            await interaction.followup.send("⚠️ Maximum allowable compounding risk is 10.0% per trade to protect capital.", ephemeral=True)
            return

        new_pct = user_store.set_user_compounding_pct(cid, pct)
        effective_margin = round(bal_eq * (new_pct / 100.0), 2)
        embed = discord.Embed(
            title="🚀 Dynamic Compounding Mode Activated!",
            description=(
                f"• **Compounding Risk**: `{new_pct:.2f}%` of live equity per trade\n"
                f"• **Reference Equity**: `${bal_eq:,.2f} USDT`\n"
                f"• **Next Trade Margin**: `~${effective_margin:,.2f} USDT`\n\n"
                f"💡 Winz automatically compounds your position sizes proportionally as balance grows!"
            ),
            color=0x2ECC71,
        )
        await interaction.followup.send(embed=embed)
        return

    fixed_margin = float(ex.get("risk_per_trade_usdt", 50.0))
    if curr_pct > 0:
        effective_margin = round(bal_eq * (curr_pct / 100.0), 2)
        embed = discord.Embed(
            title="📈 Dynamic Balance Compounding: ACTIVE",
            description=(
                f"• **Current Setting**: `{curr_pct:.2f}%` of live equity per trade\n"
                f"• **Live Equity**: `${bal_eq:,.2f} USDT`\n"
                f"• **Calculated Margin**: `${effective_margin:,.2f} USDT` per trade\n\n"
                f"👉 Use `/compound pct:1.5` to adjust or `/compound pct:0` to disable."
            ),
            color=0x2ECC71,
        )
    else:
        embed = discord.Embed(
            title="⚙️ Dynamic Balance Compounding: OFF",
            description=(
                f"• **Current Mode**: Fixed margin of `${fixed_margin:.2f} USDT` per trade\n"
                f"• **Live Equity**: `${bal_eq:,.2f} USDT`\n\n"
                f"👉 Use `/compound pct:1.0` to enable dynamic scaling with account growth."
            ),
            color=0x3498DB,
        )
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="tracked", description="View live milestone status of active calls")
async def slash_tracked(interaction: discord.Interaction):
    await interaction.response.send_message(embed=_build_tracked_embed())


@bot.tree.command(name="news", description="Fetch live breaking crypto news and macro sentiment")
async def slash_news(interaction: discord.Interaction, ticker: str = "BTC"):
    await interaction.response.defer()
    import news_sentiment
    report = news_sentiment.format_sentiment_report(ticker.upper())
    embed = discord.Embed(
        title=f"🗞️ Live Crypto News & Sentiment — ${ticker.upper()}",
        description=report,
        color=0xF1C40F,
    )
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="sentiment", description="Check Crypto Fear & Greed Index and institutional sentiment")
async def slash_sentiment(interaction: discord.Interaction, ticker: str = "BTC"):
    await interaction.response.defer()
    import news_sentiment
    report = news_sentiment.format_sentiment_report(ticker.upper())
    embed = discord.Embed(
        title=f"🌡️ Institutional Sentiment & Fear/Greed Index — ${ticker.upper()}",
        description=report,
        color=0x3498DB,
    )
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="watchlist", description="Scan market for the best setups, key shelves & macro context")
async def slash_watchlist(interaction: discord.Interaction):
    await interaction.response.defer()
    embed = _build_watchlist_embed()
    await interaction.followup.send(embed=embed)


# Prefix Commands
@bot.command(name="help")
async def cmd_help(ctx: commands.Context):
    await ctx.send(embed=_build_help_embed())


@bot.command(name="setchannel")
async def cmd_setchannel(ctx: commands.Context):
    cid = _set_channel_id(str(ctx.channel.id))
    await ctx.send(f"✅ **Winz Channel Set!** Automated hourly calls and trade milestone alerts will broadcast here: <#{cid}>.")


@bot.command(name="calls")
async def cmd_calls(ctx: commands.Context):
    await ctx.send("🔍 *Compiling latest 15 market calls (6 Scalps + 4 Day + 5 Spot)...*")
    embed, view = _build_calls_message()
    await ctx.send(embed=embed, view=view)


@bot.command(name="spot")
async def cmd_spot(ctx: commands.Context):
    await ctx.send("🔍 *Compiling 5 spot accumulation setups (1D macro timeframe)...*")
    embed, view = _build_spot_message()
    await ctx.send(embed=embed, view=view)


@bot.command(name="research")
async def cmd_research(ctx: commands.Context, ticker: str = "BTC"):
    await ctx.send(f"🔬 *Analyzing {ticker.upper()} structure, order blocks & generating chart...*")
    embed, files = _build_research_message(ticker)
    await ctx.send(embed=embed, files=files)


@bot.command(name="tweet")
async def cmd_tweet(ctx: commands.Context, ticker: str = ""):
    await ctx.send(_build_tweet_message(ticker))


@bot.command(name="tracked")
async def cmd_tracked(ctx: commands.Context):
    await ctx.send(embed=_build_tracked_embed())


@bot.command(name="news")
async def cmd_news(ctx: commands.Context, ticker: str = "BTC"):
    import news_sentiment
    report = news_sentiment.format_sentiment_report(ticker.upper())
    embed = discord.Embed(
        title=f"🗞️ Live Crypto News & Sentiment — ${ticker.upper()}",
        description=report,
        color=0xF1C40F,
    )
    await ctx.send(embed=embed)


@bot.command(name="sentiment")
async def cmd_sentiment(ctx: commands.Context, ticker: str = "BTC"):
    import news_sentiment
    report = news_sentiment.format_sentiment_report(ticker.upper())
    embed = discord.Embed(
        title=f"🌡️ Institutional Sentiment & Fear/Greed Index — ${ticker.upper()}",
        description=report,
        color=0x3498DB,
    )
    await ctx.send(embed=embed)


@bot.command(name="watchlist", aliases=["setups"])
async def cmd_watchlist(ctx: commands.Context):
    await ctx.send("🔍 *Scanning market for institutional setups, key shelves & macro context...*")
    embed = _build_watchlist_embed()
    await ctx.send(embed=embed)


def _build_watchlist_embed() -> discord.Embed:
    import scanner
    from config import CORE_TICKERS
    btc_regime = scanner.get_btc_macro_regime()

    actionable = []
    for t in CORE_TICKERS:
        try:
            s = scanner.analyze_ticker(t, "scalp")
            if s and s.get("side") in ("BUY", "SELL"):
                actionable.append(s)
        except Exception:
            pass

    embed = discord.Embed(
        title="🎯 Market Scan: Best Setups & Smart Watchlist",
        description=f"**BTC Macro Regime:** `{btc_regime}`\n**Confluence Shield:** Strict 4H Trend Confluence + Regime Lock Active\n\n*Less noise. Better context. Disciplined execution.*",
        color=0x00FF88 if actionable else 0xE67E22,
    )

    if actionable:
        act_text = []
        for s in actionable:
            t = s["ticker"]
            side = s["side"]
            conv = s.get("conviction", 0.0)
            rr = s.get("rr", 0.0)
            e_low = fmt_dollar(s.get("entry_low"))
            e_high = fmt_dollar(s.get("entry_high"))
            mark = fmt_dollar(s.get("live_price"))
            sl = fmt_dollar(s.get("sl"))
            tp1 = fmt_dollar(s.get("tp1"))
            act_text.append(f"• **{t}** `{side}` (Conv: `{conv:.0f}%` | R:R `1:{rr:.2f}`)\n  Entry: `{e_low}–{e_high}` | Stop: `{sl}` | TP1: `{tp1}`")
        embed.add_field(name="🔥 Actionable Setups Ready For Entry", value="\n\n".join(act_text), inline=False)
    else:
        embed.add_field(
            name="🛡️ Actionable Execution Status",
            value="`No entries meeting criteria right now` (Counter-trend dip buys blocked by BTC Bear Regime).",
            inline=False,
        )

    # Focus Shelves
    watch_text = []
    for t in ["BTC", "SOL", "ETH", "SUI"]:
        df = scanner.fetch_ohlcv(t, tf_minutes=15, limit=60)
        if df is not None:
            struct = scanner.detect_shelves_and_edges(df)
            live_p = fmt_dollar(struct["live_price"])
            dem_l, dem_h = struct["demand_shelf"]
            sup_l, sup_h = struct["supply_shelf"]
            trend_4h = scanner.get_4h_trend(t)
            watch_text.append(f"• **{t}** (`{live_p}` | 4H: `{trend_4h}`)\n  Demand: `{fmt_dollar(dem_l)}–{fmt_dollar(dem_h)}` | Supply: `{fmt_dollar(sup_l)}–{fmt_dollar(sup_h)}`")
    if watch_text:
        embed.add_field(name="👀 Curated Watchlist (Key Shelves & Invalidation Levels)", value="\n\n".join(watch_text), inline=False)

    embed.set_footer(text="Winz Trade Desk • Smarter Watchlists & Automated Follow-Ups")
    return embed


# ── External Message Broadcaster (Called by tracker and schedulers) ────────
def broadcast_discord_message(content: str, embed: Optional[discord.Embed] = None) -> None:
    """Safely deliver message into the configured Discord channel from any thread."""
    global _discord_loop
    if not _discord_loop or not bot.is_ready():
        return

    s = state._load_raw()
    cid_str = s.get("discord_channel_id") or cfg.discord_channel_id or os.getenv("DISCORD_CHANNEL_ID")
    if not cid_str:
        return

    try:
        channel_id = int(cid_str)
        channel = bot.get_channel(channel_id)
        if channel:
            asyncio.run_coroutine_threadsafe(channel.send(content=content, embed=embed), _discord_loop)
    except Exception as exc:
        log.warning("Discord broadcast error: %s", exc)


def start_discord_bot() -> None:
    """Run Discord bot in a dedicated background thread."""
    token = cfg.discord_bot_token or os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        log.info("Discord bot token not set. Skipping Discord bot startup.")
        return

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while True:
            try:
                loop.run_until_complete(bot.start(token))
            except Exception as exc:
                log.error("Discord bot runner error: %s. Retrying in 10s...", exc)
                time.sleep(10)

    t = threading.Thread(target=_run, daemon=True, name="discord-bot")
    t.start()
    log.info("Winz Discord bot background thread started.")


def stop_discord_bot() -> None:
    """Gracefully disconnect Discord bot."""
    global _discord_loop
    if _discord_loop and bot.is_ready():
        try:
            asyncio.run_coroutine_threadsafe(bot.close(), _discord_loop)
            log.info("Winz Discord bot disconnect requested.")
        except Exception as exc:
            log.warning("Error stopping Discord bot: %s", exc)

