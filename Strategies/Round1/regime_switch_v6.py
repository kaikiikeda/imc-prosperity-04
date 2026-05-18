"""
regime_switch_v6.py — v5 + symmetric SHORT_LINEAR branch.

v5's design flaw (flagged by teammate): only handles upward trends.
If PEPPER drops in a linear fashion, v5 stays in MM with target=32
long exposure, bleeding through the downtrend.

v6 adds a symmetric SHORT_LINEAR regime:
  - Upgrades to LONG_LINEAR when OLS confirms slope > +0.0003
  - Upgrades to SHORT_LINEAR when OLS confirms slope < -0.0003
  - Both require 3 consecutive passes (same streak logic)
  - Mirror safety valves:
      LONG_LINEAR:  book_mid < fair - 50  →  LIQUIDATE
      SHORT_LINEAR: book_mid > fair + 50  →  LIQUIDATE

FOUR-STATE MACHINE:
  MM  ↔  LONG_LINEAR   (trend up, accumulate long)
  MM  ↔  SHORT_LINEAR  (trend down, accumulate short)
  Any LINEAR  →  LIQUIDATE  (one-way latch on safety valve trip)

TRADE-OFF vs v5:
  + Protection against downward trend days (the gap in v5)
  + Opportunity to profit from downward trends (not just survive them)
  - More complex code (4 regimes, more parameter sets, mirror logic)
  - Short-side risk: if price reverses hard after building -76, loss
    can be large. Safety valve mitigates but doesn't eliminate.
  - Untested path: training data is all uptrend, no empirical
    validation for SHORT_LINEAR behavior

CONVENTIONS:
  - target_position_frac can be NEGATIVE now (e.g., -0.95 = -76 target)
  - long_bias=True still means "biased long" (mutually exclusive with
    short_bias=True)
  - no_sell blocks sells; no_buy blocks buys (mirror flags)
"""

from datamodel import Listing, Observation, Order, OrderDepth, ProsperityEncoder, Symbol, Trade, TradingState
import json
import math
from typing import Any, Dict, List, Optional, Tuple


# ╔══════════════════════════════════════════════════════════════╗
# ║                 PRODUCT CONSTANTS                            ║
# ╚══════════════════════════════════════════════════════════════╝

LIMITS = {"ASH_COATED_OSMIUM": 80, "INTARIAN_PEPPER_ROOT": 80}

# ╔══════════════════════════════════════════════════════════════╗
# ║   REGIME DETECTION CONFIG (asymmetric thresholds)            ║
# ║                                                              ║
# ║   Upgrade MM → LINEAR (hard to trigger):                     ║
# ║     - R² > R2_THRESHOLD_UP                                  ║
# ║     - slope > SLOPE_MIN_UP                                  ║
# ║     - CONSECUTIVE pass_streak >= UPGRADE_PASS_STREAK         ║
# ║                                                              ║
# ║   Downgrade LINEAR → MM (easy to trigger):                   ║
# ║     - R² < R2_THRESHOLD_DOWN (single fail)                  ║
# ║     - OR slope < SLOPE_MIN_DOWN (single fail)               ║
# ║     - OR book_mid < fair - SAFETY_VALVE_OFFSET (immediate)  ║
# ║                                                              ║
# ║   Buffer: rolling last OLS_WINDOW points, OLS every          ║
# ║   OLS_CHECK_INTERVAL ticks (same cadence as v3).             ║
# ╚══════════════════════════════════════════════════════════════╝

OLS_CHECK_INTERVAL = 20
OLS_WINDOW = 150
BUFFER_MAX = 200
DAY_END_TIMESTAMP = 1_000_000

# Upgrade thresholds (MM → LONG_LINEAR)
R2_THRESHOLD_UP = 0.90
SLOPE_MIN_UP = 0.0003
UPGRADE_PASS_STREAK = 3

# Upgrade thresholds (MM → SHORT_LINEAR) — intentionally harder
SLOPE_MIN_UP_SHORT = 0.0003
UPGRADE_PASS_STREAK_SHORT = 3

# Downgrade thresholds (LINEAR → MM) — intentionally looser
R2_THRESHOLD_DOWN = 0.80
SLOPE_MIN_DOWN = 0.0001

# Safety valve (hard trigger)
SAFETY_VALVE_OFFSET = 50

# LINEAR take_width dynamic cap
TAKE_WIDTH_MIN = 2       # was 1 — need to outbid competitors in live
TAKE_WIDTH_MAX = 10

# Emergency liquidation (when drift_broken fires in LINEAR)
LIQUIDATE_TAKE_WIDTH = 3
LIQUIDATE_CLEAR_WIDTH = 3

# Pre-upgrade position cap: when first entering LINEAR, don't immediately
# jump to target_frac=0.95; give 1 OLS cycle for confirmation
POST_UPGRADE_RAMP_TICKS = 30
POST_UPGRADE_POS_FRAC = 0.60   # first 30 ticks after upgrade, cap to 60%


# ╔══════════════════════════════════════════════════════════════╗
# ║   PEPPER PARAMS — Default MM (113218-style: MM + long tilt)  ║
# ║                                                              ║
# ║   This is the "base strategy" that runs whenever we're not   ║
# ║   in confirmed LINEAR regime.                                ║
# ║                                                              ║
# ║   Copied from 113218's PEPPER_PARAMS — the neutral MM with   ║
# ║   long_bias=True, target=0.4, trend_bias=+3.                 ║
# ╚══════════════════════════════════════════════════════════════╝

PEPPER_MM_DEFAULT = {
    "take_width": 1,              # pay 1 tick to compete for fills in live
    "clear_width": 0,
    "disregard_edge": 1,
    "join_edge": 2,
    "default_edge": 2,            # tighter passive quotes (was 3)
    "soft_position_limit": 70,
    "prevent_adverse": True,
    "adverse_volume": 20,
    "reversion_beta": 0.10,
    "trend_bias_per_tick": 1.0,
    "trend_lookahead_ticks": 3,
    "long_bias": True,
    "target_position_frac": 0.70,  # ~56 units (was 32 — build faster)
    "early_aggression_trigger": 0.3,
    "early_aggression_take": 2,
    "early_aggression_clear": 1,
}


# ╔══════════════════════════════════════════════════════════════╗
# ║   PEPPER PARAMS — LINEAR upgrade (v3-style)                  ║
# ║                                                              ║
# ║   Triggered only when OLS confirms trend for 3 consecutive   ║
# ║   checks. Aggressive target, no_sell, multi-level bidding.  ║
# ╚══════════════════════════════════════════════════════════════╝

PEPPER_LINEAR = {
    "disregard_edge": 1,
    "join_edge": 2,
    "default_edge": 2,
    "soft_position_limit": 80,
    "no_sell": True,
    "long_bias": True,
    "target_position_frac": 0.95,
    "multi_level": True,
    "level_splits": [0.50, 0.30, 0.20],
    "level_offsets": [0, 1, 2],
    "spread_ref": 13,
}


# ╔══════════════════════════════════════════════════════════════╗
# ║   PEPPER PARAMS — SHORT_LINEAR (v6: asymmetric vs LONG)      ║
# ║                                                              ║
# ║   Harder to enter (5 consecutive passes, steeper slope min), ║
# ║   lower max exposure (-0.60 = -48 initially, ramps to -0.80 ║
# ║   = -64 after POST_SHORT_CONFIRM_TICKS if OLS still holds). ║
# ╚══════════════════════════════════════════════════════════════╝

PEPPER_SHORT_LINEAR = {
    "disregard_edge": 1,
    "join_edge": 2,
    "default_edge": 2,
    "soft_position_limit": 70,
    "no_buy": True,
    "short_bias": True,
    "target_position_frac": -0.60,   # NEGATIVE: target = -48 (capped vs LONG's -76)
    "multi_level": True,
    "level_splits": [0.50, 0.30, 0.20],
    "level_offsets": [0, 1, 2],
    "spread_ref": 13,
}

PEPPER_SHORT_LINEAR_CONFIRMED = {
    "disregard_edge": 1,
    "join_edge": 2,
    "default_edge": 2,
    "soft_position_limit": 80,
    "no_buy": True,
    "short_bias": True,
    "target_position_frac": -0.80,   # ramp to -64 after confirmation
    "multi_level": True,
    "level_splits": [0.50, 0.30, 0.20],
    "level_offsets": [0, 1, 2],
    "spread_ref": 13,
}

POST_SHORT_CONFIRM_TICKS = 100  # ticks after SHORT upgrade before ramping to full target


# ╔══════════════════════════════════════════════════════════════╗
# ║   PEPPER PARAMS — Emergency liquidation (drift_broken)       ║
# ║                                                              ║
# ║   Used when safety valve trips in LINEAR mode. Exit inventory║
# ║   aggressively, don't re-enter LINEAR for the rest of day.  ║
# ║   (drift_broken is a one-way latch — no recovery to LINEAR.) ║
# ╚══════════════════════════════════════════════════════════════╝

PEPPER_LIQUIDATE = {
    "take_width": 3,
    "clear_width": 3,
    "disregard_edge": 1,
    "join_edge": 2,
    "default_edge": 2,
    "soft_position_limit": 60,
    "prevent_adverse": True,
    "adverse_volume": 20,
    "reversion_beta": 0.15,
    "trend_bias_per_tick": 0,
    "trend_lookahead_ticks": 0,
    "long_bias": False,
    "target_position_frac": 0,
    "early_aggression_trigger": 0,
    "early_aggression_take": 1,
    "early_aggression_clear": 0,
}


# ╔══════════════════════════════════════════════════════════════╗
# ║   ASH_COATED_OSMIUM CONFIG (unchanged from v3)               ║
# ╚══════════════════════════════════════════════════════════════╝

OSM_FAIR = 10000
OSM_FALLBACK_BUY = 9993
OSM_FALLBACK_SELL = 10003


# ╔══════════════════════════════════════════════════════════════╗
# ║                      TRADER                                  ║
# ╚══════════════════════════════════════════════════════════════╝

class Trader:
    def __init__(self):
        self.orders: Dict[str, List[Order]] = {}
        self.conversions = 0
        self.traderData = ""

        self.osmium_position = 0
        self.osmium_buy_orders = 0
        self.osmium_sell_orders = 0

    # ──────────────────────────────────────────────
    # STATE PERSISTENCE
    # ──────────────────────────────────────────────

    def load_state(self, raw: str) -> dict:
        if raw and raw not in ("", "SAMPLE"):
            try:
                return json.loads(raw)
            except Exception:
                pass
        return {}

    def save_state(self, state: dict) -> str:
        return json.dumps(state, separators=(",", ":"))

    # ──────────────────────────────────────────────
    # OLS
    # ──────────────────────────────────────────────

    def run_ols(self, pts: list) -> Optional[Tuple[float, float, float]]:
        n = len(pts)
        if n < 10:
            return None
        ts = [p[0] for p in pts]
        ms = [p[1] for p in pts]
        mt = sum(ts) / n
        mm = sum(ms) / n
        num = sum((t - mt) * (m - mm) for t, m in zip(ts, ms))
        den = sum((t - mt) ** 2 for t in ts)
        if den == 0:
            return None
        slope = num / den
        intercept = mm - slope * mt
        ss_res = sum((m - (intercept + slope * t)) ** 2 for t, m in zip(ts, ms))
        ss_tot = sum((m - mm) ** 2 for m in ms)
        r_sq = 1 - ss_res / ss_tot if ss_tot > 0 else 0
        return slope, intercept, r_sq

    # ──────────────────────────────────────────────
    # FAIR VALUE — shared by MM_DEFAULT and LIQUIDATE
    # ──────────────────────────────────────────────

    def pepper_fair_mm(self, od: OrderDepth, last_fair: Optional[float],
                       params: dict) -> float:
        """Orderbook-derived fair with adverse filter + optional trend bias."""
        if not od.buy_orders or not od.sell_orders:
            return last_fair if last_fair else 10000.0

        if params["prevent_adverse"]:
            filtered_bids = {
                p: v for p, v in od.buy_orders.items()
                if abs(v) < params["adverse_volume"]
            }
            filtered_asks = {
                p: v for p, v in od.sell_orders.items()
                if abs(v) < params["adverse_volume"]
            }
        else:
            filtered_bids = dict(od.buy_orders)
            filtered_asks = dict(od.sell_orders)

        if not filtered_bids:
            filtered_bids = dict(od.buy_orders)
        if not filtered_asks:
            filtered_asks = dict(od.sell_orders)

        best_bid = max(filtered_bids.keys())
        best_ask = min(filtered_asks.keys())
        book_mid = (best_bid + best_ask) / 2.0

        bid_wall_price = max(filtered_bids.keys(), key=lambda p: filtered_bids[p])
        ask_wall_price = min(filtered_asks.keys(), key=lambda p: abs(filtered_asks[p]))
        wall_mid = (bid_wall_price + ask_wall_price) / 2.0

        if bid_wall_price == best_bid and ask_wall_price == best_ask:
            raw_fair = book_mid
        else:
            raw_fair = 0.6 * wall_mid + 0.4 * book_mid

        trend_bias = params["trend_bias_per_tick"] * params["trend_lookahead_ticks"]
        raw_fair += trend_bias

        if last_fair is not None and last_fair > 0:
            beta = params["reversion_beta"]
            fair = (1 - beta) * raw_fair + beta * last_fair
        else:
            fair = raw_fair

        return fair

    # ──────────────────────────────────────────────
    # FAIR VALUE — LINEAR (formula-based, from OLS fit)
    # ──────────────────────────────────────────────

    def pepper_fair_linear(self, persistent: dict, timestamp: int) -> float:
        slope = persistent.get("calib_slope", 0.001)
        intercept = persistent.get("calib_intercept", 12000)
        return intercept + slope * timestamp

    # ──────────────────────────────────────────────
    # LINEAR take_width (dynamic)
    # ──────────────────────────────────────────────

    def pepper_take_width(self, timestamp: int, slope: float) -> int:
        """Dynamic take_width based on remaining predicted price move.
        Uses absolute slope so this works for both LONG and SHORT regimes.
        """
        remaining = (DAY_END_TIMESTAMP - timestamp) * abs(slope)
        return max(TAKE_WIDTH_MIN, min(TAKE_WIDTH_MAX, int(remaining * 0.01)))

    # ──────────────────────────────────────────────
    # STAGE 1: TAKE BEST ORDERS
    # ──────────────────────────────────────────────

    def take_best_orders_linear(
        self, product: str, od: OrderDepth, fair: float,
        position: int, bought: int, sold: int, take_width: int
    ) -> Tuple[int, int]:
        limit = LIMITS[product]
        buy_threshold = math.floor(fair) + take_width

        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price > buy_threshold:
                break
            ask_vol = -od.sell_orders[ask_price]
            room = limit - position - bought
            size = min(ask_vol, room)
            if size > 0:
                self.orders[product].append(Order(product, ask_price, size))
                bought += size

        return bought, sold

    def take_best_orders_short_linear(
        self, product: str, od: OrderDepth, fair: float,
        position: int, bought: int, sold: int, take_width: int
    ) -> Tuple[int, int]:
        """Mirror of take_best_orders_linear for building short positions.
        
        Instead of buying cheap asks, we sell to expensive bids.
        sell_threshold = ceil(fair) - take_width
        """
        limit = LIMITS[product]
        sell_threshold = math.ceil(fair) - take_width

        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price < sell_threshold:
                break
            bid_vol = od.buy_orders[bid_price]
            room = limit + position - sold    # how much more we can short
            size = min(bid_vol, room)
            if size > 0:
                self.orders[product].append(Order(product, bid_price, -size))
                sold += size

        return bought, sold

    def take_best_orders_mm(
        self, product: str, od: OrderDepth, fair: float,
        position: int, bought: int, sold: int, params: dict
    ) -> Tuple[int, int]:
        limit = LIMITS[product]
        take_w = params["take_width"]

        buy_threshold = math.floor(fair) - take_w + 1
        sell_threshold = math.ceil(fair) + take_w - 1

        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price > buy_threshold:
                break
            ask_vol = -od.sell_orders[ask_price]
            room = limit - position - bought
            size = min(ask_vol, room)
            if size > 0:
                self.orders[product].append(Order(product, ask_price, size))
                bought += size

        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price < sell_threshold:
                break
            bid_vol = od.buy_orders[bid_price]
            room = limit + position - sold
            size = min(bid_vol, room)
            if size > 0:
                self.orders[product].append(Order(product, bid_price, -size))
                sold += size

        return bought, sold

    # ──────────────────────────────────────────────
    # STAGE 2: CLEAR POSITION
    # ──────────────────────────────────────────────

    def clear_position_order(
        self, product: str, od: OrderDepth, fair: float,
        position: int, bought: int, sold: int, params: dict
    ) -> Tuple[int, int]:
        """Bring inventory toward target. Supports positive and negative targets.
        
        - target_pos > 0: lean long. Sell when above target, buy when below.
        - target_pos < 0: lean short. Buy when above (less short), sell when below.
        - target_pos = 0: flat. Sell when long, buy when short.
        
        no_sell: block sells (used by LONG_LINEAR to never exit longs)
        no_buy: block buys (used by SHORT_LINEAR to never exit shorts)
        """
        limit = LIMITS[product]
        current_pos = position + bought - sold

        # Determine target based on bias flags
        target_pos = 0
        if params.get("long_bias", False):
            target_pos = int(limit * params.get("target_position_frac", 0))
        elif params.get("short_bias", False):
            target_pos = int(limit * params.get("target_position_frac", 0))
            # target_position_frac is negative for short_bias, so this is negative

        # no_sell + over-target: do nothing (LONG_LINEAR when still accumulating)
        if params.get("no_sell", False) and current_pos > target_pos:
            return bought, sold

        # no_buy + under-target: do nothing (SHORT_LINEAR when still accumulating short)
        if params.get("no_buy", False) and current_pos < target_pos:
            return bought, sold

        # Over target: sell down
        if current_pos > target_pos and not params.get("no_sell", False):
            excess = current_pos - target_pos
            sell_threshold = math.ceil(fair)
            for bid_price in sorted(od.buy_orders.keys(), reverse=True):
                if bid_price < sell_threshold:
                    break
                bid_vol = od.buy_orders[bid_price]
                room = min(bid_vol, excess, limit + position - sold)
                if room > 0:
                    self.orders[product].append(Order(product, bid_price, -room))
                    sold += room
                    excess -= room

        # Under target: buy up
        elif current_pos < target_pos and not params.get("no_buy", False):
            deficit = target_pos - current_pos
            buy_threshold = math.floor(fair) + 1
            for ask_price in sorted(od.sell_orders.keys()):
                if ask_price > buy_threshold:
                    break
                ask_vol = -od.sell_orders[ask_price]
                room = min(ask_vol, deficit, limit - position - bought)
                if room > 0:
                    self.orders[product].append(Order(product, ask_price, room))
                    bought += room
                    deficit -= room

        return bought, sold

    # ──────────────────────────────────────────────
    # STAGE 3: MAKE ORDERS
    # ──────────────────────────────────────────────

    def make_orders(
        self, product: str, od: OrderDepth, fair: float,
        position: int, bought: int, sold: int, params: dict
    ) -> Tuple[int, int]:
        """Symmetric market maker. Supports:
        - long_bias: target > 0, build phase blocks sells
        - short_bias: target < 0, build phase blocks buys
        - no_sell: always block sells (LONG_LINEAR)
        - no_buy: always block buys (SHORT_LINEAR)
        - multi_level: split orders across 3 price levels
          (applied to BUYS for long_bias, to SELLS for short_bias)
        """
        limit = LIMITS[product]
        disregard = params["disregard_edge"]
        join = params["join_edge"]
        default = params["default_edge"]
        soft_limit = params["soft_position_limit"]

        current_pos = position + bought - sold
        target_pos = 0
        if params.get("long_bias", False) or params.get("short_bias", False):
            target_pos = int(limit * params.get("target_position_frac", 0))

        # Buy price (penny-jump)
        buy_price = round(fair) - default
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price < fair - disregard:
                if bid_price >= fair - join:
                    buy_price = bid_price + 1
                else:
                    buy_price = bid_price + 1
                break
        buy_price = min(buy_price, int(math.floor(fair)) - 1)

        # Sell price (penny-jump)
        sell_price = round(fair) + default
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price > fair + disregard:
                if ask_price <= fair + join:
                    sell_price = ask_price - 1
                else:
                    sell_price = ask_price - 1
                break
        sell_price = max(sell_price, int(math.ceil(fair)) + 1)

        if buy_price >= sell_price:
            buy_price = int(math.floor(fair)) - 2
            sell_price = int(math.ceil(fair)) + 2

        # Sizes
        max_buy = limit - position - bought
        max_sell = limit + position - sold

        # Inventory skew — symmetric, measured vs target
        pos_vs_target = current_pos - target_pos
        if abs(pos_vs_target) > soft_limit:
            if pos_vs_target > 0:
                max_buy = 0
                sell_price = max(int(math.ceil(fair)) + 1, sell_price - 2)
            else:
                max_sell = 0
                buy_price = min(int(math.floor(fair)) - 1, buy_price + 2)
        elif abs(pos_vs_target) > soft_limit * 0.6:
            skew_frac = (abs(pos_vs_target) - soft_limit * 0.6) / (soft_limit * 0.4)
            if pos_vs_target > 0:
                max_buy = max(0, int(max_buy * (1 - skew_frac * 0.7)))
                sell_price = max(int(math.ceil(fair)) + 1,
                                 sell_price - round(skew_frac * 2))
            else:
                max_sell = max(0, int(max_sell * (1 - skew_frac * 0.7)))
                buy_price = min(int(math.floor(fair)) - 1,
                                buy_price + round(skew_frac * 2))

        # No-sell guard (LONG_LINEAR)
        if params.get("no_sell", False):
            max_sell = 0

        # No-buy guard (SHORT_LINEAR) — mirror of no_sell
        if params.get("no_buy", False):
            max_buy = 0

        # Build phase guard (MM with long_bias): block sells while building long
        if params.get("long_bias", False) and not params.get("no_sell", False):
            if current_pos < target_pos:
                max_sell = 0

        # Mirror: MM with short_bias: block buys while building short
        if params.get("short_bias", False) and not params.get("no_buy", False):
            if current_pos > target_pos:
                max_buy = 0

        # Place orders — multi_level applies to whichever side we're accumulating
        # Long side: multi-level on buys
        if params.get("multi_level", False) and max_buy > 0 and \
                params.get("long_bias", False):
            cur_best_bid = max(od.buy_orders.keys()) if od.buy_orders else buy_price
            cur_best_ask = min(od.sell_orders.keys()) if od.sell_orders else sell_price
            current_spread = cur_best_ask - cur_best_bid
            spread_ref = params.get("spread_ref", 13)
            spread_factor = current_spread / spread_ref if spread_ref > 0 else 1.0

            splits = params["level_splits"]
            offsets = params["level_offsets"]
            remaining = max_buy

            for i, (frac, offset) in enumerate(zip(splits, offsets)):
                adj_offset = max(0, round(offset * spread_factor))
                level_price = buy_price - adj_offset
                if i == len(splits) - 1:
                    level_size = remaining
                else:
                    level_size = max(1, int(max_buy * frac))
                    level_size = min(level_size, remaining)
                if level_size > 0 and remaining > 0:
                    self.orders[product].append(Order(product, level_price, level_size))
                    bought += level_size
                    remaining -= level_size
        elif max_buy > 0:
            self.orders[product].append(Order(product, buy_price, max_buy))
            bought += max_buy

        # Short side: multi-level on sells (mirror)
        if params.get("multi_level", False) and max_sell > 0 and \
                params.get("short_bias", False):
            cur_best_bid = max(od.buy_orders.keys()) if od.buy_orders else buy_price
            cur_best_ask = min(od.sell_orders.keys()) if od.sell_orders else sell_price
            current_spread = cur_best_ask - cur_best_bid
            spread_ref = params.get("spread_ref", 13)
            spread_factor = current_spread / spread_ref if spread_ref > 0 else 1.0

            splits = params["level_splits"]
            offsets = params["level_offsets"]
            remaining = max_sell

            for i, (frac, offset) in enumerate(zip(splits, offsets)):
                adj_offset = max(0, round(offset * spread_factor))
                level_price = sell_price + adj_offset   # note: + for sells
                if i == len(splits) - 1:
                    level_size = remaining
                else:
                    level_size = max(1, int(max_sell * frac))
                    level_size = min(level_size, remaining)
                if level_size > 0 and remaining > 0:
                    self.orders[product].append(Order(product, level_price, -level_size))
                    sold += level_size
                    remaining -= level_size
        elif max_sell > 0:
            self.orders[product].append(Order(product, sell_price, -max_sell))
            sold += max_sell

        return bought, sold

    # ──────────────────────────────────────────────
    # OSMIUM (unchanged from v3)
    # ──────────────────────────────────────────────

    def _osm_search_buys(self, state: TradingState, acceptable_price: int):
        product = "ASH_COATED_OSMIUM"
        od = state.order_depths[product]
        if not od.sell_orders:
            return
        pos = state.position.get(product, 0)
        for ask, amount in list(od.sell_orders.items()):
            if int(ask) < acceptable_price or (
                abs(ask - acceptable_price) < 1
                and pos < 0
                and abs(pos - amount) < abs(pos)
            ):
                size = min(
                    LIMITS[product] - self.osmium_position - self.osmium_buy_orders,
                    -amount,
                )
                if size > 0:
                    self.osmium_buy_orders += size
                    self.orders[product].append(Order(product, ask, size))

    def _osm_search_sells(self, state: TradingState, acceptable_price: int):
        product = "ASH_COATED_OSMIUM"
        od = state.order_depths[product]
        if not od.buy_orders:
            return
        pos = state.position.get(product, 0)
        for bid, amount in list(od.buy_orders.items()):
            if int(bid) > acceptable_price or (
                abs(bid - acceptable_price) < 1
                and pos > 0
                and abs(pos - amount) < abs(pos)
            ):
                size = min(
                    self.osmium_position + LIMITS[product] - self.osmium_sell_orders,
                    amount,
                )
                if size > 0:
                    self.osmium_sell_orders += size
                    self.orders[product].append(Order(product, bid, -size))

    def _osm_get_bid_below(self, state: TradingState, product: str, price: int):
        for bid, _ in state.order_depths[product].buy_orders.items():
            if bid < price:
                return bid
        return None

    def _osm_get_ask_above(self, state: TradingState, product: str, price: int):
        for ask, _ in state.order_depths[product].sell_orders.items():
            if ask > price:
                return ask
        return None

    def trade_osmium(self, state: TradingState):
        product = "ASH_COATED_OSMIUM"
        self.orders[product] = []
        od = state.order_depths.get(product)
        if od is None:
            return

        self.osmium_position = state.position.get(product, 0)
        self.osmium_buy_orders = 0
        self.osmium_sell_orders = 0

        fair = OSM_FAIR
        self._osm_search_buys(state, fair)
        self._osm_search_sells(state, fair)

        best_ask = self._osm_get_ask_above(state, product, fair)
        best_bid = self._osm_get_bid_below(state, product, fair)

        buy_price = OSM_FALLBACK_BUY
        sell_price = OSM_FALLBACK_SELL
        if best_ask is not None and best_bid is not None:
            sell_price = best_ask - 1
            buy_price = best_bid + 1

        max_buy = LIMITS[product] - self.osmium_position - self.osmium_buy_orders
        max_sell = self.osmium_position + LIMITS[product] - self.osmium_sell_orders

        if max_sell > 0:
            self.orders[product].append(Order(product, sell_price, -max_sell))
        if max_buy > 0:
            self.orders[product].append(Order(product, buy_price, max_buy))

    # ──────────────────────────────────────────────
    # PEPPER — bidirectional regime switching
    # ──────────────────────────────────────────────

    def trade_pepper(self, state: TradingState, persistent: dict):
        product = "INTARIAN_PEPPER_ROOT"
        self.orders[product] = []
        od = state.order_depths.get(product)
        if od is None or not od.buy_orders or not od.sell_orders:
            return

        position = state.position.get(product, 0)
        best_bid = max(od.buy_orders.keys())
        best_ask = min(od.sell_orders.keys())
        book_mid = (best_bid + best_ask) / 2.0

        # ═══════════════════════════════════════════
        # Tick 0: initialize state. DEFAULT REGIME = MM.
        # v6: separate pass_streaks for LONG vs SHORT direction
        # ═══════════════════════════════════════════
        if "pepper_regime" not in persistent:
            persistent["pepper_regime"] = "MM"
            persistent["buf"] = []
            persistent["long_pass_streak"] = 0    # ← v6: separate streak
            persistent["short_pass_streak"] = 0   # ← v6: new, for SHORT detection
            persistent["ticks_seen"] = 0
            persistent["ticks_since_upgrade"] = 0
            persistent["drift_broken"] = False

        # Update rolling buffer
        buf = persistent.setdefault("buf", [])
        buf.append([state.timestamp, book_mid])
        if len(buf) > BUFFER_MAX:
            del buf[: len(buf) - BUFFER_MAX]
        persistent["ticks_seen"] = persistent.get("ticks_seen", 0) + 1
        ticks_seen = persistent["ticks_seen"]

        # ═══════════════════════════════════════════
        # OLS check every OLS_CHECK_INTERVAL ticks
        # v6: asymmetric thresholds + bidirectional (LONG / SHORT)
        # ═══════════════════════════════════════════
        if ticks_seen % OLS_CHECK_INTERVAL == 0 and not persistent.get("drift_broken"):
            window = buf[-OLS_WINDOW:]
            fit = self.run_ols(window)
            if fit is not None:
                slope, intercept, r_sq = fit
                persistent["calib_r2"] = r_sq
                persistent["last_ols_slope"] = slope
                persistent["last_ols_intercept"] = intercept

                if persistent["pepper_regime"] == "MM":
                    # ── MM: check upgrade criteria (both directions) ──
                    long_pass = (r_sq > R2_THRESHOLD_UP) and \
                                (slope > SLOPE_MIN_UP)
                    short_pass = (r_sq > R2_THRESHOLD_UP) and \
                                 (slope < -SLOPE_MIN_UP_SHORT)

                    # Track separate streaks for each direction
                    if long_pass:
                        persistent["long_pass_streak"] = \
                            persistent.get("long_pass_streak", 0) + 1
                        persistent["short_pass_streak"] = 0  # reset other
                        if persistent["long_pass_streak"] >= UPGRADE_PASS_STREAK:
                            persistent["pepper_regime"] = "LINEAR"
                            persistent["calib_slope"] = slope
                            persistent["calib_intercept"] = intercept
                            persistent["ticks_since_upgrade"] = 0
                            persistent["long_pass_streak"] = 0
                    elif short_pass:
                        persistent["short_pass_streak"] = \
                            persistent.get("short_pass_streak", 0) + 1
                        persistent["long_pass_streak"] = 0  # reset other
                        if persistent["short_pass_streak"] >= UPGRADE_PASS_STREAK_SHORT:
                            persistent["pepper_regime"] = "SHORT_LINEAR"
                            persistent["calib_slope"] = slope
                            persistent["calib_intercept"] = intercept
                            persistent["ticks_since_upgrade"] = 0
                            persistent["short_pass_streak"] = 0
                    else:
                        # Neither direction passed — reset both streaks
                        persistent["long_pass_streak"] = 0
                        persistent["short_pass_streak"] = 0

                elif persistent["pepper_regime"] == "LINEAR":
                    # ── LONG LINEAR: check downgrade (slope weakening or negative) ──
                    downgrade_fail = (r_sq < R2_THRESHOLD_DOWN) or \
                                     (slope < SLOPE_MIN_DOWN)
                    if downgrade_fail:
                        persistent["pepper_regime"] = "MM"
                        persistent["long_pass_streak"] = 0
                        persistent["short_pass_streak"] = 0
                    else:
                        persistent["calib_slope"] = slope
                        persistent["calib_intercept"] = intercept

                elif persistent["pepper_regime"] == "SHORT_LINEAR":
                    # ── SHORT LINEAR: check downgrade (slope weakening or positive) ──
                    # Mirror of LONG: fail if R² drops OR slope rises above -SLOPE_MIN_DOWN
                    downgrade_fail = (r_sq < R2_THRESHOLD_DOWN) or \
                                     (slope > -SLOPE_MIN_DOWN)
                    if downgrade_fail:
                        persistent["pepper_regime"] = "MM"
                        persistent["long_pass_streak"] = 0
                        persistent["short_pass_streak"] = 0
                    else:
                        persistent["calib_slope"] = slope
                        persistent["calib_intercept"] = intercept

        # Update ticks_since_upgrade counter
        if persistent["pepper_regime"] in ("LINEAR", "SHORT_LINEAR"):
            persistent["ticks_since_upgrade"] = \
                persistent.get("ticks_since_upgrade", 0) + 1

        regime = persistent["pepper_regime"]

        # ═══════════════════════════════════════════
        # LONG LINEAR regime
        # ═══════════════════════════════════════════
        if regime == "LINEAR":
            fair = self.pepper_fair_linear(persistent, state.timestamp)

            # Safety valve: price fell far below trend → drift_broken
            if book_mid < fair - SAFETY_VALVE_OFFSET:
                persistent["drift_broken"] = True
                persistent["pepper_regime"] = "MM"

            if not persistent["drift_broken"]:
                # Post-upgrade ramp
                ticks_since = persistent.get("ticks_since_upgrade", 0)
                if ticks_since <= POST_UPGRADE_RAMP_TICKS:
                    params = dict(PEPPER_LINEAR)
                    params["target_position_frac"] = POST_UPGRADE_POS_FRAC
                    params["soft_position_limit"] = \
                        int(LIMITS[product] * POST_UPGRADE_POS_FRAC * 0.85)
                    take_width = min(
                        self.pepper_take_width(
                            state.timestamp,
                            persistent.get("calib_slope", 0.001)
                        ),
                        3,
                    )
                else:
                    params = PEPPER_LINEAR
                    take_width = self.pepper_take_width(
                        state.timestamp,
                        persistent.get("calib_slope", 0.001)
                    )

                bought, sold = 0, 0
                bought, sold = self.take_best_orders_linear(
                    product, od, fair, position, bought, sold, take_width)
                bought, sold = self.clear_position_order(
                    product, od, fair, position, bought, sold, params)
                bought, sold = self.make_orders(
                    product, od, fair, position, bought, sold, params)
                return

        # ═══════════════════════════════════════════
        # SHORT LINEAR regime (v6 new — mirror of LONG LINEAR)
        # ═══════════════════════════════════════════
        if regime == "SHORT_LINEAR":
            fair = self.pepper_fair_linear(persistent, state.timestamp)

            # Mirror safety valve: price rose far above trend → drift_broken
            if book_mid > fair + SAFETY_VALVE_OFFSET:
                persistent["drift_broken"] = True
                persistent["pepper_regime"] = "MM"

            if not persistent["drift_broken"]:
                ticks_since = persistent.get("ticks_since_upgrade", 0)
                if ticks_since <= POST_UPGRADE_RAMP_TICKS:
                    # Phase 1: initial ramp (same as LONG — small position)
                    params = dict(PEPPER_SHORT_LINEAR)
                    params["target_position_frac"] = -POST_UPGRADE_POS_FRAC
                    params["soft_position_limit"] = \
                        int(LIMITS[product] * POST_UPGRADE_POS_FRAC * 0.85)
                    take_width = min(
                        self.pepper_take_width(
                            state.timestamp,
                            persistent.get("calib_slope", -0.001)
                        ),
                        3,
                    )
                elif ticks_since <= POST_SHORT_CONFIRM_TICKS:
                    # Phase 2: hold at initial SHORT target (-48), wait for confirmation
                    params = PEPPER_SHORT_LINEAR
                    take_width = self.pepper_take_width(
                        state.timestamp,
                        persistent.get("calib_slope", -0.001)
                    )
                else:
                    # Phase 3: confirmed — ramp to full SHORT target (-64)
                    r2 = persistent.get("calib_r2", 0)
                    if r2 >= R2_THRESHOLD_DOWN:
                        params = PEPPER_SHORT_LINEAR_CONFIRMED
                    else:
                        params = PEPPER_SHORT_LINEAR
                    take_width = self.pepper_take_width(
                        state.timestamp,
                        persistent.get("calib_slope", -0.001)
                    )

                bought, sold = 0, 0
                bought, sold = self.take_best_orders_short_linear(
                    product, od, fair, position, bought, sold, take_width)
                bought, sold = self.clear_position_order(
                    product, od, fair, position, bought, sold, params)
                bought, sold = self.make_orders(
                    product, od, fair, position, bought, sold, params)
                return

        # ═══════════════════════════════════════════
        # MM regime (default, or post-downgrade, or drift_broken liquidation)
        # ═══════════════════════════════════════════
        if persistent.get("drift_broken"):
            # Emergency liquidation — neutral MM, aggressive exit
            params = PEPPER_LIQUIDATE
        else:
            # Normal MM: 113218-style trending MM with long tilt
            params = PEPPER_MM_DEFAULT

        last_fair = persistent.get("pf", None)
        fair = self.pepper_fair_mm(od, last_fair, params)
        persistent["pf"] = round(fair, 2)

        params = dict(params)
        # Early aggression: widen take when far from target
        target = int(LIMITS[product] * params["target_position_frac"])
        deficit = target - position
        if deficit > target * params["early_aggression_trigger"] and \
                params["early_aggression_trigger"] > 0:
            params["take_width"] = params["early_aggression_take"]

        bought, sold = 0, 0
        bought, sold = self.take_best_orders_mm(
            product, od, fair, position, bought, sold, params)
        bought, sold = self.clear_position_order(
            product, od, fair, position, bought, sold, params)
        bought, sold = self.make_orders(
            product, od, fair, position, bought, sold, params)

    # ──────────────────────────────────────────────
    # MAIN
    # ──────────────────────────────────────────────

    def run(self, state: TradingState):
        persistent = self.load_state(state.traderData)
        self.orders = {}
        self.conversions = 0

        for product in state.order_depths:
            self.orders[product] = []

        if "ASH_COATED_OSMIUM" in state.order_depths:
            self.trade_osmium(state)

        if "INTARIAN_PEPPER_ROOT" in state.order_depths:
            self.trade_pepper(state, persistent)

        self.traderData = self.save_state(persistent)
        return self.orders, self.conversions, self.traderData