"""
news_sentiment.py — Real-Time News, Fundamental & Macro Sentiment Engine.
Integrates:
  1. Live Crypto Fear & Greed Index (Alternative.me API)
  2. Live Breaking Crypto News Headlines & Analysis (RSS Feeds)
  3. NLP Keyword Sentiment Scoring (Bullish, Bearish, Neutral)
  4. Asset-Specific Fundamental Drivers & Sentiment Impact
Serves both Winz Trade Desk (theses & manual calls) and Xira (macro risk filter).
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger(__name__)

_FEAR_GREED_CACHE: Dict[str, Any] = {}
_FEAR_GREED_TTL = 900  # 15 minutes

_NEWS_CACHE: List[Dict[str, Any]] = []
_NEWS_CACHE_TIME = 0
_NEWS_TTL = 600  # 10 minutes

# Bullish and Bearish Institutional Keyword Lexicon
_BULLISH_KEYWORDS = {
    "surge", "rally", "breakout", "accumulate", "inflow", "adoption", "approval",
    "etf", "partnership", "upgrade", "all-time high", "ath", "bullish", "expansion",
    "gain", "jump", "soar", "pump", "record", "backing", "recovery", "outperform",
    "liquidity", "reclaim", "support", "momentum", "buy", "holding", "treasury"
}

_BEARISH_KEYWORDS = {
    "crash", "plunge", "drop", "dump", "selloff", "liquidation", "outflow",
    "sec", "ban", "hack", "exploit", "lawsuit", "bearish", "crackdown", "fear",
    "inflation", "recession", "downside", "breakdown", "fall", "decline", "warning",
    "loss", "collapse", "fraud", "panic", "bleeding", "rejection", "drop"
}


# ── 1. Fear & Greed Index ───────────────────────────────────────────────────
def get_fear_and_greed_index() -> Dict[str, Any]:
    """
    Fetch current live Crypto Fear & Greed Index from Alternative.me.
    Cached for 15 minutes.
    """
    global _FEAR_GREED_CACHE
    now = time.time()
    if _FEAR_GREED_CACHE and (now - _FEAR_GREED_CACHE.get("cached_at", 0) < _FEAR_GREED_TTL):
        return _FEAR_GREED_CACHE

    try:
        resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            item = data.get("data", [])[0]
            val = int(item.get("value", 50))
            classification = item.get("value_classification", "Neutral")
            _FEAR_GREED_CACHE = {
                "score": val,
                "classification": classification,
                "cached_at": now,
                "timestamp": item.get("timestamp"),
            }
            return _FEAR_GREED_CACHE
    except Exception as exc:
        log.debug("Fear & Greed fetch failed: %s", exc)

    if _FEAR_GREED_CACHE:
        return _FEAR_GREED_CACHE

    return {"score": 50, "classification": "Neutral", "cached_at": now}


# ── 2. Live Crypto News RSS Feeds ───────────────────────────────────────────
def fetch_latest_crypto_news(limit: int = 6) -> List[Dict[str, Any]]:
    """
    Fetch top live breaking crypto news from decentralized RSS feeds.
    Cached for 10 minutes.
    """
    global _NEWS_CACHE, _NEWS_CACHE_TIME
    now = time.time()
    if _NEWS_CACHE and (now - _NEWS_CACHE_TIME < _NEWS_TTL):
        return _NEWS_CACHE[:limit]

    sources = [
        "https://cointelegraph.com/rss",
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
    ]

    news_items: List[Dict[str, Any]] = []

    for url in sources:
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=8)
            if r.status_code != 200:
                continue

            root = ET.fromstring(r.content)
            # Standard RSS channel -> items
            for item in root.findall(".//item")[:10]:
                title = item.findtext("title") or ""
                link = item.findtext("link") or ""
                pub_date = item.findtext("pubDate") or ""
                desc = item.findtext("description") or ""

                # Strip HTML tags from description
                clean_desc = re.sub(r"<[^>]+>", "", desc).strip()

                if title and not any(n["title"] == title for n in news_items):
                    sentiment_score, sentiment_label = score_text_sentiment(f"{title} {clean_desc}")
                    news_items.append({
                        "title": title.strip(),
                        "link": link.strip(),
                        "published": pub_date.strip(),
                        "summary": clean_desc[:200],
                        "sentiment_score": sentiment_score,
                        "sentiment": sentiment_label,
                    })
        except Exception as exc:
            log.debug("News feed fetch failed for %s: %s", url, exc)

    if news_items:
        _NEWS_CACHE = news_items
        _NEWS_CACHE_TIME = now
        return _NEWS_CACHE[:limit]

    return _NEWS_CACHE[:limit] if _NEWS_CACHE else []


# ── 3. Sentiment Scoring ────────────────────────────────────────────────────
def score_text_sentiment(text: str) -> tuple[int, str]:
    """Score sentiment based on financial institutional keyword density."""
    words = set(re.findall(r"\b\w+\b", text.lower()))
    bull_count = len(words.intersection(_BULLISH_KEYWORDS))
    bear_count = len(words.intersection(_BEARISH_KEYWORDS))

    diff = bull_count - bear_count
    if diff >= 2:
        return diff, "Bullish 🟢"
    elif diff <= -2:
        return diff, "Bearish 🔴"
    elif diff == 1:
        return diff, "Slightly Bullish 📈"
    elif diff == -1:
        return diff, "Slightly Bearish 📉"
    return 0, "Neutral ⚖️"


# ── 4. Full Market Sentiment Synthesis ──────────────────────────────────────
def get_comprehensive_sentiment(ticker: Optional[str] = None) -> Dict[str, Any]:
    """
    Synthesize live Fear & Greed index, top news flow, and ticker-specific sentiment.
    """
    fng = get_fear_and_greed_index()
    news = fetch_latest_crypto_news(limit=6)

    # Calculate overall news tone
    bull_count = sum(1 for n in news if "Bullish" in n["sentiment"])
    bear_count = sum(1 for n in news if "Bearish" in n["sentiment"])

    if bull_count > bear_count:
        news_tone = "Constructive / Risk-On"
    elif bear_count > bull_count:
        news_tone = "Defensive / Risk-Off"
    else:
        news_tone = "Balanced Consolidation"

    ticker_news = []
    if ticker:
        t_clean = ticker.upper().replace("USDT", "").replace("$", "")
        # Common synonyms
        synonyms = {t_clean}
        if t_clean == "BTC":
            synonyms.add("BITCOIN")
        elif t_clean == "ETH":
            synonyms.add("ETHEREUM")
        elif t_clean == "SOL":
            synonyms.add("SOLANA")
        elif t_clean == "XRP":
            synonyms.add("RIPPLE")

        for n in news:
            title_upper = n["title"].upper()
            if any(s in title_upper for s in synonyms):
                ticker_news.append(n)

    return {
        "fear_and_greed": fng,
        "news_tone": news_tone,
        "recent_headlines": news[:4],
        "ticker_headlines": ticker_news,
    }


# ── 5. Formatted Sentiment Report for Telegram & Discord ───────────────────
def format_sentiment_report(ticker: Optional[str] = None) -> str:
    """Format an institutional sentiment & news briefing."""
    data = get_comprehensive_sentiment(ticker)
    fng = data["fear_and_greed"]
    score = fng.get("score", 50)
    classification = fng.get("classification", "Neutral")

    # Fear and Greed bar representation
    filled = int(score / 10)
    bar = "🟩" * filled + "⬜" * (10 - filled)

    lines = [
        "🌐 *INSTITUTIONAL MACRO & SENTIMENT BRIEFING*",
        f"⏱ *Updated:* {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
        "",
        f"📊 *Crypto Fear & Greed Index:* `{score}/100` ({classification})",
        f"{bar}",
        f"• *Market Regime Tone:* `{data['news_tone']}`",
        "",
        "📰 *Breaking Market Headlines:*",
    ]

    headlines = data.get("recent_headlines", [])
    if headlines:
        for idx, h in enumerate(headlines, 1):
            lines.append(f"{idx}. *{h['title']}*")
            lines.append(f"   ↳ Tone: `{h['sentiment']}`")
    else:
        lines.append("• _Scanning live decentralized wire... Market consolidation orderly._")

    if ticker and data.get("ticker_headlines"):
        lines.append("")
        lines.append(f"🔍 *Asset Specific News (${ticker.upper()}):*")
        for h in data["ticker_headlines"]:
            lines.append(f"• *{h['title']}* (`{h['sentiment']}`)")

    lines.append("")
    lines.append("💡 *Institutional Insight:*")
    if score >= 75:
        lines.append("_Extreme Greed detected. High-leverage long crowding increases pullback risk. Protect stops._")
    elif score <= 30:
        lines.append("_Extreme Fear detected. Panic selling creates favorable asymmetrical spot accumulation zones._")
    else:
        lines.append("_Neutral market sentiment. Technical order blocks and range boundaries hold high predictive value._")

    return "\n".join(lines)
