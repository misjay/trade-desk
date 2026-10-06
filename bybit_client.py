"""
bybit_client.py — Production Bybit V5 REST Client for Trade Desk.

Supports:
  - LIVE: Bybit V5 Mainnet (https://api.bybit.com)
  - DEMO: Bybit Demo Trading (https://api-demo.bybit.com) or Testnet (https://api-testnet.bybit.com)
  - PAPER: Local simulation engine against real-time Bybit market orderbooks and mark prices

Features:
  - HMAC SHA256 request signing
  - Market data: tickers, orderbook depth, funding rate, klines
  - Account info: wallet balance (USDT equity), base coin inventory (for spot sells)
  - Trading: isolated margin, leverage configuration, limit (post-only) and market orders, TP/SL
  - Orderbook slippage & spread calculator
  - Instrument info caching (tick size, lot step, min order qty)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)

# Endpoints
_BYBIT_MAINNET = "https://api.bybit.com"
_BYBIT_DEMO = "https://api-demo.bybit.com"
_BYBIT_TESTNET = "https://api-testnet.bybit.com"


class BybitClient:
    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        mode: str = "demo",          # "demo" or "live"
        demo_env: str = "paper",     # "paper", "demo", or "testnet"
        recv_window: str = "5000",
    ):
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.mode = mode.lower()
        self.demo_env = demo_env.lower()
        self.recv_window = recv_window
        self.session = requests.Session()

        # Cache for instrument info: symbol -> {tick_size, qty_step, min_qty, max_qty, min_notional}
        self._instruments_cache: Dict[str, Dict[str, float]] = {}

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def is_paper(self) -> bool:
        if self.is_live:
            return False
        if self.demo_env == "paper":
            return True
        if not self.api_key or not self.api_secret or self.api_key.startswith("your_"):
            return True
        return False

    @property
    def base_url(self) -> str:
        if self.is_live:
            return _BYBIT_MAINNET
        if self.demo_env in ("testnet", "demo"):
            return _BYBIT_TESTNET
        # For paper trading, market data is fetched from Mainnet for genuine liquidity
        return _BYBIT_MAINNET

    # ── Signing ───────────────────────────────────────────────────────────────
    def _sign(self, timestamp: str, payload_str: str) -> str:
        if not self.api_secret:
            return ""
        param_str = timestamp + self.api_key + self.recv_window + payload_str
        return hmac.new(
            self.api_secret.encode("utf-8"),
            param_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _headers(self, timestamp: str, payload_str: str) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-BAPI-RECV-WINDOW": self.recv_window,
        }
        if self.api_key and self.api_secret:
            headers["X-BAPI-API-KEY"] = self.api_key
            headers["X-BAPI-TIMESTAMP"] = timestamp
            headers["X-BAPI-SIGN"] = self._sign(timestamp, payload_str)
        return headers

    # ── HTTP Requests ─────────────────────────────────────────────────────────
    def request(self, method: str, path: str, params: Optional[dict] = None, data: Optional[dict] = None) -> Optional[dict]:
        ts = str(int(time.time() * 1000))
        hosts = [self.base_url]
        if "testnet" in self.base_url:
            if "bytick" in self.base_url:
                hosts.append("https://api-testnet.bybit.com")
            else:
                hosts.append("https://api-testnet.bytick.com")
        else:
            if "bytick" in self.base_url:
                hosts.append("https://api.bybit.com")
            else:
                hosts.append("https://api.bytick.com")

        last_exc = None
        for host in hosts:
            url = f"{host}{path}"
            payload_str = ""

            if method.upper() == "GET":
                query_str = urlencode(params or {})
                payload_str = query_str
                if query_str:
                    url = f"{url}?{query_str}"
                headers = self._headers(ts, payload_str)
                try:
                    r = self.session.get(url, headers=headers, timeout=5)
                    r.raise_for_status()
                    return r.json()
                except Exception as exc:
                    last_exc = exc
                    continue
            else:
                body = data if data is not None else (params or {})
                payload_str = json.dumps(body, separators=(",", ":"))
                headers = self._headers(ts, payload_str)
                try:
                    r = self.session.post(url, data=payload_str, headers=headers, timeout=5)
                    r.raise_for_status()
                    return r.json()
                except Exception as exc:
                    last_exc = exc
                    continue

        if last_exc:
            log.error("Bybit %s %s failed on all hosts: %s", method.upper(), path, last_exc)
        return None

    # ── Market Data ───────────────────────────────────────────────────────────
    def get_ticker(self, symbol: str, category: str = "linear") -> Optional[dict]:
        """Fetch latest price, mark price, funding rate, 24h volume for symbol."""
        res = self.request("GET", "/v5/market/tickers", {"category": category, "symbol": symbol})
        if not res or res.get("retCode") != 0:
            return None
        items = res.get("result", {}).get("list", [])
        if not items:
            return None
        t = items[0]
        return {
            "symbol": t.get("symbol"),
            "last_price": float(t.get("lastPrice", 0)),
            "mark_price": float(t.get("markPrice", t.get("lastPrice", 0))),
            "funding_rate": float(t.get("fundingRate", 0)) if t.get("fundingRate") else 0.0,
            "next_funding_time": t.get("nextFundingTime"),
            "bid1": float(t.get("bid1Price", 0)) if t.get("bid1Price") else 0.0,
            "ask1": float(t.get("ask1Price", 0)) if t.get("ask1Price") else 0.0,
            "turnover_24h": float(t.get("turnover24h", 0)) if t.get("turnover24h") else 0.0,
            "volume_24h": float(t.get("volume24h", 0)) if t.get("volume24h") else 0.0,
        }

    def get_orderbook(self, symbol: str, category: str = "linear", limit: int = 25) -> Optional[dict]:
        """Fetch orderbook depth and compute top bid/ask spread and book metrics."""
        res = self.request("GET", "/v5/market/orderbook", {"category": category, "symbol": symbol, "limit": limit})
        if not res or res.get("retCode") != 0:
            return None
        result = res.get("result", {})
        raw_bids = result.get("b", [])
        raw_asks = result.get("a", [])
        if not raw_bids or not raw_asks:
            return None

        bids = [[float(p), float(s)] for p, s in raw_bids]
        asks = [[float(p), float(s)] for p, s in raw_asks]

        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid = (best_bid + best_ask) / 2
        spread = best_ask - best_bid
        spread_pct = (spread / mid) * 100 if mid > 0 else 0.0

        return {
            "symbol": symbol,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "spread": spread,
            "spread_pct": spread_pct,
            "bids": bids,
            "asks": asks,
        }

    def compute_book_slippage(self, orderbook: dict, side: str, order_qty: float) -> Tuple[float, float]:
        """
        Compute effective fill price and percentage slippage from mid for a given order size.
        side: 'BUY' fills into asks; 'SELL' fills into bids.
        Returns: (effective_price, slippage_pct)
        """
        mid = orderbook["mid"]
        book = orderbook["asks"] if side.upper() == "BUY" else orderbook["bids"]
        if not book or order_qty <= 0:
            return (mid, 0.0)

        accum_qty = 0.0
        accum_cost = 0.0
        for price, size in book:
            take = min(size, order_qty - accum_qty)
            accum_qty += take
            accum_cost += take * price
            if accum_qty >= order_qty:
                break

        if accum_qty == 0:
            return (mid, 0.0)

        effective_price = accum_cost / accum_qty
        slip_pct = abs(effective_price - mid) / mid * 100
        return (effective_price, slip_pct)

    def compute_depth_imbalance(self, orderbook: dict, depth_levels: int = 25) -> Dict[str, Any]:
        """
        Compute order book depth imbalance (bid vs ask liquidity pressure).
        ratio = bid_notional / (bid_notional + ask_notional)
        ratio >= 0.58: Institutional Bid Wall / Absorption at Support (Long Confluence)
        ratio <= 0.42: Heavy Ask Pressure / Wall at Resistance (Short Confluence)
        """
        if not orderbook:
            return {"ratio": 0.5, "state": "BALANCED", "bid_notional": 0.0, "ask_notional": 0.0}

        bids = orderbook.get("bids", [])[:depth_levels]
        asks = orderbook.get("asks", [])[:depth_levels]

        bid_notional = sum(p * s for p, s in bids)
        ask_notional = sum(p * s for p, s in asks)
        total = bid_notional + ask_notional

        if total <= 0:
            return {"ratio": 0.5, "state": "BALANCED", "bid_notional": 0.0, "ask_notional": 0.0}

        ratio = bid_notional / total
        if ratio >= 0.58:
            state_str = "BID_HEAVY"
        elif ratio <= 0.42:
            state_str = "ASK_HEAVY"
        else:
            state_str = "BALANCED"

        return {
            "ratio": round(ratio, 4),
            "state": state_str,
            "bid_notional": round(bid_notional, 2),
            "ask_notional": round(ask_notional, 2),
        }

    def find_liquidity_wall(self, orderbook: dict, side: str, target_price: float, tolerance_pct: float = 0.015) -> Optional[float]:
        """
        Locate significant limit walls in the order book near a target TP price.
        If a massive cluster (>= 3x average level size) sits right at/near the target,
        return a front-run price just inside the wall to ensure clean execution before rejection.
        - side == 'BUY': We are LONG, selling into ASKS at TP. Front-run by placing slightly BELOW the ask wall.
        - side == 'SELL': We are SHORT, buying into BIDS at TP. Front-run by placing slightly ABOVE the bid wall.
        """
        if not orderbook:
            return None

        # Look in asks if taking profit on a LONG, bids if taking profit on a SHORT
        levels = orderbook.get("asks", []) if side.upper() == "BUY" else orderbook.get("bids", [])
        if not levels or len(levels) < 3:
            return None

        # Calculate average level size
        avg_notional = sum(p * s for p, s in levels) / len(levels)
        if avg_notional <= 0:
            return None

        best_wall_price = None
        for price, size in levels:
            notional = price * size
            # Check if within tolerance band of target_price
            dist_pct = abs(price - target_price) / target_price if target_price > 0 else 1.0
            if dist_pct <= tolerance_pct and notional >= (avg_notional * 2.5):
                # Detected heavy liquidity wall!
                if side.upper() == "BUY" and price <= target_price:
                    # Place 0.05% below the ask wall
                    best_wall_price = price * 0.9995
                elif side.upper() == "SELL" and price >= target_price:
                    # Place 0.05% above the bid wall
                    best_wall_price = price * 1.0005
                break

        return best_wall_price

    def get_open_interest_delta(self, symbol: str) -> Dict[str, Any]:
        """
        Fetch Bybit Open Interest and compute 15m delta:
        Returns: {
          "oi_current": float,
          "oi_prev": float,
          "delta_pct": float,
          "is_liquidation_flush": bool,
          "is_fakeout_risk": bool
        }
        """
        res = self.request("GET", "/v5/market/open-interest", {
            "category": "linear",
            "symbol": symbol,
            "intervalTime": "15min",
            "limit": 3,
        })
        if not res or res.get("retCode") != 0:
            return {"delta_pct": 0.0, "is_liquidation_flush": False, "is_fakeout_risk": False}

        oi_list = res.get("result", {}).get("list", [])
        if len(oi_list) < 2:
            return {"delta_pct": 0.0, "is_liquidation_flush": False, "is_fakeout_risk": False}

        try:
            curr_oi = float(oi_list[0].get("openInterest", 0.0) or 0.0)
            prev_oi = float(oi_list[1].get("openInterest", 0.0) or 0.0)
            if prev_oi <= 0:
                return {"delta_pct": 0.0, "is_liquidation_flush": False, "is_fakeout_risk": False}

            delta_pct = (curr_oi - prev_oi) / prev_oi * 100.0
            is_flush = (delta_pct <= -1.5)
            is_fakeout = (delta_pct <= -1.0)

            return {
                "oi_current": curr_oi,
                "oi_prev": prev_oi,
                "delta_pct": round(delta_pct, 2),
                "is_liquidation_flush": is_flush,
                "is_fakeout_risk": is_fakeout,
            }
        except Exception as exc:
            log.debug("Error parsing OI for %s: %s", symbol, exc)
            return {"delta_pct": 0.0, "is_liquidation_flush": False, "is_fakeout_risk": False}

    def get_klines(self, symbol: str, interval: str = "15", limit: int = 100, category: str = "linear") -> List[dict]:
        """Fetch historical klines. Bybit returns descending, reversed to ascending."""
        res = self.request("GET", "/v5/market/kline", {
            "category": category,
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        })
        if not res or res.get("retCode") != 0:
            # Fallback to spot if linear not found
            res = self.request("GET", "/v5/market/kline", {
                "category": "spot",
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
            })
            if not res or res.get("retCode") != 0:
                return []

        raw_list = res.get("result", {}).get("list", [])
        if not raw_list:
            return []

        # Reverse to ascending (oldest first)
        raw_list = list(reversed(raw_list))
        records = []
        for k in raw_list:
            # [startTime, openPrice, highPrice, lowPrice, closePrice, volume, turnover]
            records.append({
                "open_time": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "turnover": float(k[6]) if len(k) > 6 else 0.0,
            })
        return records

    # ── Instrument Info & Quantization ────────────────────────────────────────
    def get_instrument_info(self, symbol: str, category: str = "linear") -> Dict[str, float]:
        """Retrieve and cache tick size, step size, min order qty."""
        key = f"{category}:{symbol}"
        if key in self._instruments_cache:
            return self._instruments_cache[key]

        res = self.request("GET", "/v5/market/instruments-info", {"category": category, "symbol": symbol})
        info = {
            "tick_size": 0.01,
            "qty_step": 0.001,
            "min_qty": 0.001,
            "max_qty": 1000000.0,
            "min_notional": 5.0,
        }
        if res and res.get("retCode") == 0:
            items = res.get("result", {}).get("list", [])
            if items:
                item = items[0]
                pf = item.get("priceFilter", {})
                lf = item.get("lotSizeFilter", {})
                if "tickSize" in pf and pf["tickSize"]:
                    info["tick_size"] = float(pf["tickSize"])
                if "qtyStep" in lf and lf["qtyStep"]:
                    info["qty_step"] = float(lf["qtyStep"])
                elif "basePrecision" in lf and lf["basePrecision"]:
                    info["qty_step"] = float(lf["basePrecision"])
                if "minOrderQty" in lf and lf["minOrderQty"]:
                    info["min_qty"] = float(lf["minOrderQty"])
                if "maxOrderQty" in lf and lf["maxOrderQty"]:
                    info["max_qty"] = float(lf["maxOrderQty"])
                if "minNotionalValue" in lf and lf["minNotionalValue"]:
                    info["min_notional"] = float(lf["minNotionalValue"])

        self._instruments_cache[key] = info
        return info

    def quantize_price(self, symbol: str, price: float, category: str = "linear") -> float:
        """Round price to valid tick size."""
        info = self.get_instrument_info(symbol, category)
        tick = info["tick_size"]
        if tick <= 0:
            return price
        decimals = max(0, int(round(-math.log10(tick))))
        units = round(round(price / tick, 8))
        rounded = units * tick
        return round(rounded, decimals)

    def quantize_qty(self, symbol: str, qty: float, category: str = "linear") -> float:
        """Round qty down to nearest qty step."""
        info = self.get_instrument_info(symbol, category)
        step = info["qty_step"]
        if step <= 0:
            return qty
        decimals = max(0, int(round(-math.log10(step))))
        units = math.floor(round(qty / step, 8))
        rounded = units * step
        return round(rounded, decimals)

    # ── Account Info ──────────────────────────────────────────────────────────
    def get_wallet_balance(self, coin: str = "USDT") -> Dict[str, float]:
        """
        Fetch equity and available balance from Unified Trading Account (UTA).
        If in paper mode without keys, returns fallback defaults.
        """
        if self.is_paper:
            # Paper mode handles balance locally in state
            return {"equity": 0.0, "available": 0.0, "paper": True}

        res = self.request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": coin})
        if not res or res.get("retCode") != 0:
            # Try classic CONTRACT account
            res = self.request("GET", "/v5/account/wallet-balance", {"accountType": "CONTRACT", "coin": coin})

        if res and res.get("retCode") == 0:
            list_data = res.get("result", {}).get("list", [])
            if list_data:
                acct = list_data[0]
                total_equity = float(acct.get("totalEquity", 0) or 0)
                coins = acct.get("coin", [])
                for c in coins:
                    if c.get("coin") == coin:
                        eq = float(c.get("equity", 0) or total_equity)
                        wb = float(c.get("walletBalance", 0) or 0)
                        pos_im = float(c.get("totalPositionIM", 0) or 0)
                        ord_im = float(c.get("totalOrderIM", 0) or 0)
                        raw_avail = c.get("availableToWithdraw")
                        if raw_avail not in (None, ""):
                            avail = float(raw_avail)
                        else:
                            avail = max(0.0, wb - pos_im - ord_im)
                        return {"equity": eq if eq > 0 else total_equity, "available": avail, "paper": False}
                if total_equity > 0:
                    return {"equity": total_equity, "available": total_equity, "paper": False}

        log.warning("Could not fetch Bybit wallet balance: %s", res)
        return {"equity": 0.0, "available": 0.0, "paper": False}

    def get_coin_inventory(self, coin: str) -> float:
        """
        Fetch balance of base coin for Spot inventory check before Spot sell.
        Rule: 'Spot Sell only if user holds the coin. If flat, drop the spot sell.'
        """
        if self.is_paper:
            return 0.0

        res = self.request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": coin})
        if res and res.get("retCode") == 0:
            list_data = res.get("result", {}).get("list", [])
            if list_data:
                for c in list_data[0].get("coin", []):
                    if c.get("coin") == coin:
                        return float(c.get("walletBalance", 0) or 0.0)
        return 0.0

    # ── Trading Operations ────────────────────────────────────────────────────
    def set_isolated_margin_and_leverage(self, symbol: str, leverage: int, category: str = "linear") -> bool:
        """Configure isolated margin (tradeMode=1) and leverage on Bybit."""
        if self.is_paper:
            return True

        lev_str = str(int(leverage))
        # Switch to isolated margin
        self.request("POST", "/v5/position/switch-isolated", {
            "category": category,
            "symbol": symbol,
            "tradeMode": 1,
            "buyLeverage": lev_str,
            "sellLeverage": lev_str,
        })

        # Set leverage
        resp = self.request("POST", "/v5/position/set-leverage", {
            "category": category,
            "symbol": symbol,
            "buyLeverage": lev_str,
            "sellLeverage": lev_str,
        })
        if resp and resp.get("retCode") in (0, 110043):  # 110043 = not modified
            log.info("Set %s isolated leverage to %sx on Bybit", symbol, lev_str)
            return True
        log.warning("Failed to set leverage for %s on Bybit: %s", symbol, resp)
        return False

    def place_order(
        self,
        category: str,
        symbol: str,
        side: str,                  # "Buy" or "Sell"
        order_type: str,            # "Limit" or "Market"
        qty: float,
        price: Optional[float] = None,
        time_in_force: str = "PostOnly",
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        reduce_only: bool = False,
    ) -> Optional[dict]:
        """
        Create order on Bybit V5.
        Default limit order uses 'PostOnly' to guarantee maker execution at the shelf.
        """
        if self.is_paper:
            # Paper execution handled by simulator
            return None

        # Quantize price and qty
        q_qty = self.quantize_qty(symbol, qty, category)
        info = self.get_instrument_info(symbol, category)
        if q_qty < info["min_qty"]:
            log.error("Order qty %.6f for %s is below minOrderQty %.6f", q_qty, symbol, info["min_qty"])
            return None

        params: Dict[str, Any] = {
            "category": category,
            "symbol": symbol,
            "side": side.capitalize(),
            "orderType": order_type,
            "qty": str(q_qty),
        }

        if reduce_only:
            params["reduceOnly"] = True

        if category == "spot" and order_type.lower() == "market":
            params["marketUnit"] = "baseCoin"

        if order_type.lower() == "limit":
            if price is None:
                log.error("Price required for Limit order on %s", symbol)
                return None
            q_price = self.quantize_price(symbol, price, category)
            params["price"] = str(q_price)
            params["timeInForce"] = time_in_force
        else:
            params["timeInForce"] = "IOC"

        # Perp SL & TP
        if category == "linear":
            if stop_loss is not None:
                q_sl = self.quantize_price(symbol, stop_loss, category)
                params["stopLoss"] = str(q_sl)
                params["slOrderType"] = "Market"
            if take_profit is not None:
                q_tp = self.quantize_price(symbol, take_profit, category)
                params["takeProfit"] = str(q_tp)
                params["tpOrderType"] = "Market"
            if stop_loss is not None or take_profit is not None:
                params["tpslMode"] = "Full"

        resp = self.request("POST", "/v5/order/create", data=params)
        if resp and resp.get("retCode") == 0:
            order_id = resp.get("result", {}).get("orderId")
            log.info("Bybit %s order placed: %s %s qty=%s @%s ID=%s",
                     category, symbol, side, params.get("qty"), params.get("price", "MKT"), order_id)
            return resp.get("result")

        log.error("Bybit order FAILED for %s %s: retCode=%s retMsg=%s params=%s",
                  category, symbol,
                  resp.get("retCode") if resp else "None",
                  resp.get("retMsg") if resp else "No response",
                  params)
        return None

    def cancel_order(self, category: str, symbol: str, order_id: str) -> bool:
        """Cancel working order by orderId."""
        if self.is_paper:
            return True
        resp = self.request("POST", "/v5/order/cancel", {
            "category": category,
            "symbol": symbol,
            "orderId": order_id,
        })
        return bool(resp and resp.get("retCode") == 0)

    def cancel_all_orders(self, category: str = "linear", symbol: Optional[str] = None) -> List[str]:
        """Cancel all resting orders on Bybit for given category and optional symbol."""
        if self.is_paper:
            return []
        params: Dict[str, Any] = {"category": category}
        if symbol:
            params["symbol"] = symbol
        elif category == "linear":
            params["settleCoin"] = "USDT"

        res = self.request("POST", "/v5/order/cancel-all", params)
        if res and res.get("retCode") == 0:
            order_list = res.get("result", {}).get("list", [])
            return [o.get("orderId") for o in order_list if o.get("orderId")]
        log.warning("cancel_all_orders failed for %s (%s): %s", category, symbol, res)
        return []

    def get_open_orders(self, category: str = "linear", symbol: Optional[str] = None) -> List[dict]:
        """Fetch open/unfilled resting orders from Bybit."""
        if self.is_paper:
            return []
        params: Dict[str, Any] = {"category": category, "openOnly": 0}
        if symbol:
            params["symbol"] = symbol
        res = self.request("GET", "/v5/order/realtime", params)
        if not res or res.get("retCode") != 0:
            return []
        return res.get("result", {}).get("list", [])

    def check_connection(self) -> Dict[str, Any]:
        """Test authentication, fetch permissions, and query balance."""
        if self.is_paper:
            return {
                "status": "PAPER_MODE",
                "message": "Running in local paper simulation mode. Set valid BYBIT_API_KEY / SECRET in .env to send real orders to Bybit.",
                "base_url": self.base_url,
                "has_keys": bool(self.api_key and not self.api_key.startswith("your_")),
            }

        # 1. Test API key info
        api_res = self.request("GET", "/v5/user/query-api")
        if not api_res:
            return {
                "status": "CONNECTION_FAILED",
                "message": f"Could not reach Bybit endpoint {self.base_url}",
                "base_url": self.base_url,
            }

        if api_res.get("retCode") != 0:
            return {
                "status": "AUTH_ERROR",
                "retCode": api_res.get("retCode"),
                "retMsg": api_res.get("retMsg"),
                "message": f"Bybit authentication failed: [{api_res.get('retCode')}] {api_res.get('retMsg')}",
                "base_url": self.base_url,
                "masked_key": (self.api_key[:6] + "..." + self.api_key[-4:]) if len(self.api_key) > 10 else self.api_key,
            }

        result = api_res.get("result", {})
        perms = result.get("permissions", {})

        # 2. Test Wallet Balance
        bal = self.get_wallet_balance("USDT")

        return {
            "status": "AUTHENTICATED",
            "base_url": self.base_url,
            "mode": self.mode,
            "demo_env": self.demo_env,
            "masked_key": (self.api_key[:6] + "..." + self.api_key[-4:]) if len(self.api_key) > 10 else self.api_key,
            "permissions": perms,
            "equity_usdt": bal.get("equity", 0.0),
            "available_usdt": bal.get("available", 0.0),
            "message": f"Successfully connected to Bybit ({self.base_url})! Equity: ${bal.get('equity', 0.0):,.2f} USDT",
        }

    def get_active_positions(self, category: str = "linear") -> List[dict]:
        """Fetch all non-zero open positions directly from Bybit."""
        if self.is_paper:
            return []
        res = self.request("GET", "/v5/position/list", {"category": category, "settleCoin": "USDT"})
        if not res or res.get("retCode") != 0:
            return []
        active = []
        for p in res.get("result", {}).get("list", []):
            size = float(p.get("size", 0) or 0)
            if size > 0:
                active.append({
                    "symbol": p.get("symbol"),
                    "side": p.get("side"),  # "Buy" or "Sell"
                    "size": size,
                    "entry_price": float(p.get("avgPrice", 0) or 0),
                    "mark_price": float(p.get("markPrice", 0) or 0),
                    "unrealised_pnl": float(p.get("unrealisedPnl", 0) or 0),
                    "stop_loss": float(p.get("stopLoss", 0) or 0) if p.get("stopLoss") else None,
                    "take_profit": float(p.get("takeProfit", 0) or 0) if p.get("takeProfit") else None,
                    "leverage": int(float(p.get("leverage", 1) or 1)),
                    "position_idx": p.get("positionIdx", 0),
                    "updated_time": p.get("updatedTime"),
                })
        return active

    def get_closed_pnl(self, category: str = "linear", limit: int = 20) -> List[dict]:
        """Fetch recently closed positions and PnL directly from Bybit."""
        if self.is_paper:
            return []
        res = self.request("GET", "/v5/position/closed-pnl", {"category": category, "limit": limit})
        if not res or res.get("retCode") != 0:
            return []
        records = []
        for item in res.get("result", {}).get("list", []):
            records.append({
                "order_id": item.get("orderId"),
                "symbol": item.get("symbol"),
                "side": item.get("side"),
                "qty": float(item.get("qty", 0) or 0),
                "closed_pnl": float(item.get("closedPnl", 0) or 0),
                "entry_price": float(item.get("avgEntryPrice", 0) or 0),
                "exit_price": float(item.get("avgExitPrice", 0) or 0),
                "updated_time": item.get("updatedTime"),
                "exec_type": item.get("execType"),
            })
        return records

    def close_position_market(self, symbol: str, side: str, qty: float, category: str = "linear") -> Optional[dict]:
        """
        Close a position immediately on Bybit using a market order with reduceOnly=True.
        side: pass the CLOSE side (if Long, pass 'Sell'; if Short, pass 'Buy').
        """
        if self.is_paper:
            return None
        q_qty = self.quantize_qty(symbol, qty, category)
        params = {
            "category": category,
            "symbol": symbol,
            "side": side.capitalize(),
            "orderType": "Market",
            "qty": str(q_qty),
            "reduceOnly": True,
            "timeInForce": "IOC",
        }
        resp = self.request("POST", "/v5/order/create", data=params)
        if resp and resp.get("retCode") == 0:
            log.info("Closed position on Bybit: %s %s qty=%s", symbol, side, q_qty)
            return resp.get("result")
        log.error("Failed to close position on Bybit for %s: %s", symbol, resp)
        return None

    def set_trading_stop(self, symbol: str, stop_loss: Optional[float] = None, take_profit: Optional[float] = None, category: str = "linear") -> bool:
        """Update native Stop Loss and Take Profit on Bybit for an open position."""
        if self.is_paper:
            return True
        params: Dict[str, Any] = {
            "category": category,
            "symbol": symbol,
            "positionIdx": 0,
            "tpslMode": "Full",
        }
        if stop_loss is not None:
            params["stopLoss"] = str(self.quantize_price(symbol, stop_loss, category))
            params["slOrderType"] = "Market"
        if take_profit is not None:
            params["takeProfit"] = str(self.quantize_price(symbol, take_profit, category))
            params["tpOrderType"] = "Market"
        resp = self.request("POST", "/v5/position/trading-stop", data=params)
        return bool(resp and resp.get("retCode") == 0)
