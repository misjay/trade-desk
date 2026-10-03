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
from typing import Optional

import discord
from discord.ext import commands

from config import cfg, CORE_TICKERS, EXTRA_TICKERS
from scanner import fmt_dollar
import state

log = logging.getLogger("winz_discord")

# Discord intents setup (default unprivileged intents so bot connects instantly)
intents = discord.Intents.default()
intents.message_content = False

bot = commands.Bot(command_prefix=["!", "/"], intents=intents, help_command=None)
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
            label = f"{t} ({tf} {side})"
            el = fmt_dollar(c.get("entry_low"))
            eh = fmt_dollar(c.get("entry_high"))
            desc = f"Entry: {el}–{eh} | TP1: {fmt_dollar(c.get('tp1'))}"
            options.append(discord.SelectOption(label=label, value=f"{t}_{tf}", description=desc[:100], emoji="📊"))

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
        tf_min = 240 if tf_str == "4h" else 15

        import market_research
        import chart
        from scanner import fetch_ohlcv

        sig = market_research.build_signal_for_timeframe(ticker, tf_minutes=tf_min)
        note = market_research.generate_market_research(sig)

        df = fetch_ohlcv(ticker, tf_minutes=tf_min, limit=80)
        png_bytes = chart.generate_chart(
            df=df,
            ticker=ticker,
            side=sig.get("side", "BUY"),
            tf=tf_str,
            entry_low=sig.get("entry_low"),
            entry_high=sig.get("entry_high"),
            tp1=sig.get("tp1"),
            tp2=sig.get("tp2"),
            sl=sig.get("sl"),
            live_price=sig.get("live_price"),
        )

        files = []
        if png_bytes and len(png_bytes) > 1000:
            files.append(discord.File(io.BytesIO(png_bytes), filename=f"{ticker}_{tf_str}_chart.png"))

        embed = discord.Embed(
            title=f"🔬 Winz Research: {ticker}/USDT ({tf_str} {sig.get('side')})",
            description=note[:4000],
            color=0x00FF88 if sig.get("side") == "BUY" else 0xFF3366,
        )
        if files:
            embed.set_image(url=f"attachment://{ticker}_{tf_str}_chart.png")

        await interaction.followup.send(embed=embed, files=files)


# ── Shared Command Builders ──────────────────────────────────────────────────
def _build_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🤖 Winz Trade Desk — Discord Control & Signals",
        description="Winz provides automated institutional market calls, charts, and real-time trade tracking.",
        color=0x5865F2,
    )
    embed.add_field(name="🎯 `/calls` or `!calls`", value="Display the latest 10 Hourly Calls with an interactive chart dropdown.", inline=False)
    embed.add_field(name="🔬 `/research <COIN>`", value="Generate deep institutional thesis, levels & live chart (e.g. `/research SOL`).", inline=False)
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


def _build_calls_message() -> tuple[discord.Embed, CallSelectView]:
    from config import CORE_TICKERS, EXTRA_TICKERS
    import market_research

    candidate_universe = CORE_TICKERS + EXTRA_TICKERS
    scalp_sigs = []
    for t in candidate_universe:
        if len(scalp_sigs) >= 6:
            break
        sig = market_research.build_signal_for_timeframe(t, tf_minutes=15)
        if sig and sig.get("rr", 0) >= 1.5:
            scalp_sigs.append(sig)

    day_sigs = []
    scalp_t = {s["ticker"] for s in scalp_sigs}
    for t in candidate_universe:
        if len(day_sigs) >= 4:
            break
        if t in scalp_t:
            continue
        sig = market_research.build_signal_for_timeframe(t, tf_minutes=240)
        if sig and sig.get("rr", 0) >= 1.5:
            day_sigs.append(sig)

    all_calls = scalp_sigs + day_sigs
    state.save_tracked_calls(all_calls)

    embed = discord.Embed(
        title="🚨 WINZ HOURLY TRADE DESK DISPATCH (10 CALLS)",
        description="**For Manual / External Exchange Trading**\nSelect any asset from the dropdown below to view its live chart markup and institutional research note.",
        color=0x00FF88,
    )

    scalp_text = []
    for idx, s in enumerate(scalp_sigs, 1):
        t, side = s["ticker"], s["side"]
        el, eh = fmt_dollar(s["entry_low"]), fmt_dollar(s["entry_high"])
        tp1, sl, rr = fmt_dollar(s["tp1"]), fmt_dollar(s["sl"]), s.get("rr", 2.0)
        scalp_text.append(f"**{idx}. ${t}** `{side}` — Entry: `{el}–{eh}` | TP: `{tp1}` | SL: `{sl}` (R:R {rr:.1f})")

    day_text = []
    for idx, s in enumerate(day_sigs, len(scalp_sigs) + 1):
        t, side = s["ticker"], s["side"]
        el, eh = fmt_dollar(s["entry_low"]), fmt_dollar(s["entry_high"])
        tp1, sl, rr = fmt_dollar(s["tp1"]), fmt_dollar(s["sl"]), s.get("rr", 2.0)
        day_text.append(f"**{idx}. ${t}** `{side}` — Entry: `{el}–{eh}` | TP: `{tp1}` | SL: `{sl}` (R:R {rr:.1f})")

    embed.add_field(name="⚡ 6 SCALP CALLS (15m Timeframe)", value="\n".join(scalp_text) if scalp_text else "None", inline=False)
    embed.add_field(name="🏛 4 DAY TRADE CALLS (4h Timeframe)", value="\n".join(day_text) if day_text else "None", inline=False)

    view = CallSelectView(all_calls)
    return embed, view


def _build_research_message(ticker: str) -> tuple[discord.Embed, list[discord.File]]:
    t_clean = ticker.upper().replace("USDT", "").replace("$", "")
    import market_research
    import chart
    from scanner import fetch_ohlcv

    sig = market_research.build_signal_for_timeframe(t_clean, tf_minutes=15)
    note = market_research.generate_market_research(sig)

    df = fetch_ohlcv(t_clean, tf_minutes=15, limit=80)
    png_bytes = chart.generate_chart(
        df=df,
        ticker=t_clean,
        side=sig.get("side", "BUY"),
        tf="15m",
        entry_low=sig.get("entry_low"),
        entry_high=sig.get("entry_high"),
        tp1=sig.get("tp1"),
        tp2=sig.get("tp2"),
        sl=sig.get("sl"),
        live_price=sig.get("live_price"),
    )

    files = []
    if png_bytes and len(png_bytes) > 1000:
        files.append(discord.File(io.BytesIO(png_bytes), filename=f"{t_clean}_15m_chart.png"))

    embed = discord.Embed(
        title=f"📊 Market Research: {t_clean}/USDT (15m {sig.get('side')})",
        description=note[:4000],
        color=0x00FF88 if sig.get("side") == "BUY" else 0xFF3366,
    )
    if files:
        embed.set_image(url=f"attachment://{t_clean}_15m_chart.png")

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
        title="📋 Winz Active Tracked Calls",
        description="Tracking Take-Profits, Stop-Losses, and Gain Milestones against live Bybit prices.",
        color=0xFEE75C,
    )
    if not tracked:
        embed.description = "ℹ️ No active tracked calls currently in memory. Run `/calls` to generate fresh calls!"
        return embed

    lines = []
    from scanner import fetch_live_price
    for cid, c in list(tracked.items())[:15]:
        t = c["ticker"]
        side = c["side"]
        tf = c.get("tf", "15m")
        lp = fetch_live_price(t) or 0.0
        tp1_stat = "✅ TP1" if c.get("tp1_hit") else f"TP1: {fmt_dollar(c.get('tp1'))}"
        sl_stat = "🛑 SL" if c.get("sl_hit") else f"SL: {fmt_dollar(c.get('sl'))}"
        lines.append(f"• **${t}** ({tf} `{side}`) — Mark: `{fmt_dollar(lp)}` | {tp1_stat} | {sl_stat}")

    embed.description = "\n".join(lines)
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
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="10 Hourly Crypto Calls | /calls"))


# Slash Commands
@bot.tree.command(name="help", description="Winz Discord Help & Guide")
async def slash_help(interaction: discord.Interaction):
    await interaction.response.send_message(embed=_build_help_embed())


@bot.tree.command(name="setchannel", description="Set this channel for automated calls & milestone alerts")
async def slash_setchannel(interaction: discord.Interaction):
    cid = _set_channel_id(str(interaction.channel_id))
    await interaction.response.send_message(f"✅ **Winz Channel Set!** Automated hourly calls and trade milestone alerts will broadcast here: <#{cid}>.")


@bot.tree.command(name="calls", description="Display the 10 hourly calls with interactive asset dropdown")
async def slash_calls(interaction: discord.Interaction):
    await interaction.response.defer()
    embed, view = _build_calls_message()
    await interaction.followup.send(embed=embed, view=view)


@bot.tree.command(name="research", description="Generate live institutional research & chart for a crypto asset")
async def slash_research(interaction: discord.Interaction, ticker: str = "BTC"):
    await interaction.response.defer()
    embed, files = _build_research_message(ticker)
    await interaction.followup.send(embed=embed, files=files)


@bot.tree.command(name="tweet", description="Generate 280-char Twitter/X post for an asset")
async def slash_tweet(interaction: discord.Interaction, ticker: str = "BTC"):
    await interaction.response.send_message(_build_tweet_message(ticker))


@bot.tree.command(name="tracked", description="View live milestone status of active calls")
async def slash_tracked(interaction: discord.Interaction):
    await interaction.response.send_message(embed=_build_tracked_embed())


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
    await ctx.send("🔍 *Compiling latest 10 market calls (6 Scalps + 4 Day Trades)...*")
    embed, view = _build_calls_message()
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
        try:
            loop.run_until_complete(bot.start(token))
        except Exception as exc:
            log.error("Discord bot runner error: %s", exc)

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

