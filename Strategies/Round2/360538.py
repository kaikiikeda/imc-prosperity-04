"""
IMC Prosperity 4 — Round 1 Trader v15
======================================
v11 scored 10,655 (asym at 10008/9992 gave +45)

v15 pushes asym prices MORE aggressive:
- Asym bullish sell: 10008 -> 10010 (when asks empty, quote further from fair)
- Asym bearish buy: 9992 -> 9990 (when bids empty, quote further from fair)

Logic: when one side of book is empty, WE are the only quote on that side.
Market takers will hit us at whatever price we choose.
Higher sell / lower buy = more profit per unit.

Risk: if market takers have price limits, we miss fills.
Evidence: at t=69800 (asym bullish), market bought at 10008 (we filled v11).
          At 10010, they might still buy if their limit is >10010.

Expected: +14 (3 trades * 2 points improvement) if all fills still happen
         -45 (lose v11's asym edge) if NO asym fills happen
BEST CASE: ~10,700

NEW EDGE: When ask side is empty/missing, price moves UP by ~8 points in next 5 ticks.
          When bid side is empty/missing, price moves DOWN by ~8 points.
          
These signals appear ~375 times per day on OSMIUM and ~365 on PEPPER.
Signal correlation with future moves: 0.54-0.64.

Strategy:
- Ask empty -> BULLISH: shift our sell quote HIGH (don't sell into rally), buy aggressive
- Bid empty -> BEARISH: shift our buy quote LOW (don't buy into decline), sell aggressive

"""

from datamodel import Listing, Observation, Order, OrderDepth, ProsperityEncoder, Symbol, Trade, TradingState
import json
import math
from typing import Any, Dict, List, Optional, Tuple

LIMITS = {"ASH_COATED_OSMIUM": 80, "INTARIAN_PEPPER_ROOT": 80}

OLS_CHECK_INTERVAL = 30
OLS_WINDOW = 50
R2_THRESHOLD = 0.90
SLOPE_MIN = 0.0003
FAIL_STREAK_TO_DEGRADE = 3
ASSUMED_SLOPE = 0.001
BUFFER_MAX = 80

PEPPER_LINEAR = {
    "disregard_edge": 1, "join_edge": 2, "default_edge": 2,
    "soft_position_limit": 80, "no_sell": True, "long_bias": True,
    "target_position_frac": 0.95, "multi_level": True,
    "level_splits": [0.50, 0.30, 0.20], "level_offsets": [0, 1, 2],
    "spread_ref": 13,
}

PEPPER_MM = {
    "take_width": 1, "clear_width": 0, "disregard_edge": 1,
    "join_edge": 2, "default_edge": 2, "soft_position_limit": 78,
    "prevent_adverse": True, "adverse_volume": 20,
    "reversion_beta": 0.10, "trend_bias_per_tick": 1.0,
    "trend_lookahead_ticks": 3, "long_bias": True,
    "target_position_frac": 0.95,
    "early_aggression_trigger": 0.3, "early_aggression_take": 2,
    "early_aggression_clear": 2,
}


class Trader:
    def __init__(self):
        self.orders: Dict[str, List[Order]] = {}
        self.conversions = 0
        self.traderData = ""
        self.osmium_position = 0
        self.osmium_buy_orders = 0
        self.osmium_sell_orders = 0

    def load_state(self, raw):
        if raw and raw not in ("", "SAMPLE"):
            try: return json.loads(raw)
            except: pass
        return {}

    def save_state(self, state):
        return json.dumps(state, separators=(",", ":"))

    def run_ols(self, pts):
        n = len(pts)
        if n < 10: return None
        ts = [p[0] for p in pts]; ms = [p[1] for p in pts]
        mt = sum(ts) / n; mm = sum(ms) / n
        num = sum((t - mt) * (m - mm) for t, m in zip(ts, ms))
        den = sum((t - mt) ** 2 for t in ts)
        if den == 0: return None
        slope = num / den; intercept = mm - slope * mt
        ss_res = sum((m - (intercept + slope * t)) ** 2 for t, m in zip(ts, ms))
        ss_tot = sum((m - mm) ** 2 for m in ms)
        r_sq = 1 - ss_res / ss_tot if ss_tot > 0 else 0
        return slope, intercept, r_sq

    def pepper_fair_mm(self, od, last_fair, timestamp):
        params = PEPPER_MM
        if not od.buy_orders or not od.sell_orders:
            return last_fair if last_fair else 10000.0
        if params["prevent_adverse"]:
            fb = {p: v for p, v in od.buy_orders.items() if abs(v) < params["adverse_volume"]}
            fa = {p: v for p, v in od.sell_orders.items() if abs(v) < params["adverse_volume"]}
        else:
            fb = dict(od.buy_orders); fa = dict(od.sell_orders)
        if not fb: fb = dict(od.buy_orders)
        if not fa: fa = dict(od.sell_orders)
        bb = max(fb.keys()); ba = min(fa.keys()); bm = (bb + ba) / 2
        bw = max(fb.keys(), key=lambda p: fb[p])
        aw = min(fa.keys(), key=lambda p: abs(fa[p]))
        wm = (bw + aw) / 2
        raw = bm if (bw == bb and aw == ba) else 0.6 * wm + 0.4 * bm
        raw += params["trend_bias_per_tick"] * params["trend_lookahead_ticks"]
        if last_fair is not None and last_fair > 0:
            return (1 - params["reversion_beta"]) * raw + params["reversion_beta"] * last_fair
        return raw

    def pepper_take_width(self, timestamp, slope):
        remaining = (1_000_000 - timestamp) * slope
        return max(1, min(10, int(remaining * 0.01)))

    def take_best_orders_linear(self, product, od, fair, position, bought, sold, take_width):
        limit = LIMITS[product]
        buy_threshold = math.floor(fair) + take_width
        for ap in sorted(od.sell_orders.keys()):
            if ap > buy_threshold: break
            av = -od.sell_orders[ap]
            room = limit - position - bought
            sz = min(av, room)
            if sz > 0:
                self.orders[product].append(Order(product, ap, sz)); bought += sz
        return bought, sold

    def take_best_orders_mm(self, product, od, fair, position, bought, sold, params):
        limit = LIMITS[product]
        take_w = params["take_width"]
        buy_t = math.floor(fair) - take_w + 1
        sell_t = math.ceil(fair) + take_w - 1
        for ap in sorted(od.sell_orders.keys()):
            if ap > buy_t: break
            av = -od.sell_orders[ap]
            room = limit - position - bought
            sz = min(av, room)
            if sz > 0:
                self.orders[product].append(Order(product, ap, sz)); bought += sz
        for bp in sorted(od.buy_orders.keys(), reverse=True):
            if bp < sell_t: break
            bv = od.buy_orders[bp]
            room = limit + position - sold
            sz = min(bv, room)
            if sz > 0:
                self.orders[product].append(Order(product, bp, -sz)); sold += sz
        return bought, sold

    def clear_position_order(self, product, od, fair, position, bought, sold, params):
        limit = LIMITS[product]
        current_pos = position + bought - sold
        target_pos = 0
        if params.get("long_bias", False):
            target_pos = int(limit * params.get("target_position_frac", 0))
        if params.get("no_sell", False) and current_pos > target_pos:
            return bought, sold
        if current_pos > target_pos and not params.get("no_sell", False):
            excess = current_pos - target_pos
            sell_t = math.ceil(fair)
            for bp in sorted(od.buy_orders.keys(), reverse=True):
                if bp < sell_t: break
                bv = od.buy_orders[bp]
                room = min(bv, excess, limit + position - sold)
                if room > 0:
                    self.orders[product].append(Order(product, bp, -room))
                    sold += room; excess -= room
        elif current_pos < target_pos:
            deficit = target_pos - current_pos
            buy_t = math.floor(fair) + 1
            for ap in sorted(od.sell_orders.keys()):
                if ap > buy_t: break
                av = -od.sell_orders[ap]
                room = min(av, deficit, limit - position - bought)
                if room > 0:
                    self.orders[product].append(Order(product, ap, room))
                    bought += room; deficit -= room
        return bought, sold

    def make_orders(self, product, od, fair, position, bought, sold, params,
                    asym_bullish=False, asym_bearish=False):
        """Modified with ASYMMETRIC BOOK SIGNAL.
        asym_bullish: ask is missing/empty - price likely rising - don't sell cheap
        asym_bearish: bid is missing/empty - price likely falling - don't buy expensive
        """
        limit = LIMITS[product]
        disregard = params["disregard_edge"]
        join = params["join_edge"]
        default = params["default_edge"]
        soft_limit = params["soft_position_limit"]
        current_pos = position + bought - sold
        target_pos = 0
        if params.get("long_bias", False):
            target_pos = int(limit * params.get("target_position_frac", 0))

        buy_price = round(fair) - default
        for bp in sorted(od.buy_orders.keys(), reverse=True):
            if bp < fair - disregard:
                buy_price = bp + 1
                break
        buy_price = min(buy_price, int(math.floor(fair)) - 1)

        sell_price = round(fair) + default
        for ap in sorted(od.sell_orders.keys()):
            if ap > fair + disregard:
                sell_price = ap - 1
                break
        sell_price = max(sell_price, int(math.ceil(fair)) + 1)

        if buy_price >= sell_price:
            buy_price = int(math.floor(fair)) - 2
            sell_price = int(math.ceil(fair)) + 2

        max_buy = limit - position - bought
        max_sell = limit + position - sold

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

        if params.get("no_sell", False):
            max_sell = 0
        if params.get("long_bias", False) and not params.get("no_sell", False):
            if current_pos < target_pos:
                max_sell = 0
        
        # ASYMMETRIC BOOK SIGNAL adjustments
        if asym_bullish:
            # Ask side empty - price rising expected
            # Shift sell UP (get better price) and REDUCE sell size (don't sell into rally)
            sell_price = max(sell_price, int(math.ceil(fair)) + 5)
            max_sell = max_sell // 3
        if asym_bearish:
            # Bid side empty - price falling expected
            # Shift buy DOWN and REDUCE buy size
            buy_price = min(buy_price, int(math.floor(fair)) - 5)
            max_buy = max_buy // 3

        if params.get("multi_level", False) and max_buy > 0:
            cur_bb = max(od.buy_orders.keys()) if od.buy_orders else buy_price
            cur_ba = min(od.sell_orders.keys()) if od.sell_orders else sell_price
            current_spread = cur_ba - cur_bb
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
                    bought += level_size; remaining -= level_size
        elif max_buy > 0:
            self.orders[product].append(Order(product, buy_price, max_buy))
            bought += max_buy

        if max_sell > 0:
            self.orders[product].append(Order(product, sell_price, -max_sell))
            sold += max_sell

        return bought, sold

    def _osm_search_buys(self, state, acceptable):
        product = "ASH_COATED_OSMIUM"
        od = state.order_depths[product]
        if not od.sell_orders: return
        pos = state.position.get(product, 0)
        for ask, amt in list(od.sell_orders.items()):
            if int(ask) < acceptable or (
                abs(ask - acceptable) < 1 and pos < 0 and abs(pos - amt) < abs(pos)
            ):
                sz = min(LIMITS[product] - self.osmium_position - self.osmium_buy_orders, -amt)
                if sz > 0:
                    self.osmium_buy_orders += sz
                    self.orders[product].append(Order(product, ask, sz))

    def _osm_search_sells(self, state, acceptable):
        product = "ASH_COATED_OSMIUM"
        od = state.order_depths[product]
        if not od.buy_orders: return
        pos = state.position.get(product, 0)
        for bid, amt in list(od.buy_orders.items()):
            if int(bid) > acceptable or (
                abs(bid - acceptable) < 1 and pos > 0 and abs(pos - amt) < abs(pos)
            ):
                sz = min(self.osmium_position + LIMITS[product] - self.osmium_sell_orders, amt)
                if sz > 0:
                    self.osmium_sell_orders += sz
                    self.orders[product].append(Order(product, bid, -sz))

    def _osm_get_bid_below(self, state, product, price):
        for bid, _ in state.order_depths[product].buy_orders.items():
            if bid < price: return bid
        return None

    def _osm_get_ask_above(self, state, product, price):
        for ask, _ in state.order_depths[product].sell_orders.items():
            if ask > price: return ask
        return None

    def trade_osmium(self, state):
        """OSMIUM with asymmetric book signal."""
        product = "ASH_COATED_OSMIUM"
        self.orders[product] = []
        od = state.order_depths.get(product)
        if od is None: return
        self.osmium_position = state.position.get(product, 0)
        self.osmium_buy_orders = 0
        self.osmium_sell_orders = 0
        fair = 10000

        # Detect asymmetric book BEFORE taking (signal is strongest then)
        # Use full book, not post-take
        has_any_ask = len(od.sell_orders) > 0
        has_any_bid = len(od.buy_orders) > 0
        asym_bullish = not has_any_ask
        asym_bearish = not has_any_bid

        self._osm_search_buys(state, fair)
        self._osm_search_sells(state, fair)

        best_ask = self._osm_get_ask_above(state, product, fair)
        best_bid = self._osm_get_bid_below(state, product, fair)

        buy_price = 9993
        sell_price = 10003

        if best_ask is not None and best_bid is not None:
            sell_price = best_ask - 1
            buy_price = best_bid + 1

        # Apply asymmetric signal
        if asym_bullish:
            # Ask side empty - price rising expected
            sell_price = max(sell_price, 10010)  # stay high
        if asym_bearish:
            buy_price = min(buy_price, 9990)  # stay low

        max_buy = LIMITS[product] - self.osmium_position - self.osmium_buy_orders
        max_sell = self.osmium_position + LIMITS[product] - self.osmium_sell_orders

        # Reduce sell size when bullish signal
        if asym_bullish:
            max_sell = max_sell // 3
        if asym_bearish:
            max_buy = max_buy // 3

        if max_sell > 0:
            self.orders[product].append(Order(product, sell_price, -max_sell))
        if max_buy > 0:
            self.orders[product].append(Order(product, buy_price, max_buy))

    def trade_pepper(self, state, persistent):
        product = "INTARIAN_PEPPER_ROOT"
        self.orders[product] = []
        od = state.order_depths.get(product)
        if od is None or not od.buy_orders or not od.sell_orders:
            # ASYMMETRIC: if one side empty, that's the signal!
            # But need od to not be None
            if od is None: return
            # Handle asym case here via simple logic
            # For now, skip if not both sides
            return
        position = state.position.get(product, 0)
        best_bid = max(od.buy_orders.keys())
        best_ask = min(od.sell_orders.keys())
        book_mid = (best_bid + best_ask) / 2.0

        # Detect asymmetric book for PEPPER too
        has_any_ask = len(od.sell_orders) > 0
        has_any_bid = len(od.buy_orders) > 0
        asym_bullish = not has_any_ask
        asym_bearish = not has_any_bid

        if "pepper_regime" not in persistent:
            persistent["pepper_regime"] = "LINEAR"
            persistent["calib_slope"] = ASSUMED_SLOPE
            persistent["calib_intercept"] = book_mid - ASSUMED_SLOPE * state.timestamp
            persistent["buf"] = []
            persistent["fail_streak"] = 0
            persistent["ticks_seen"] = 0

        buf = persistent.setdefault("buf", [])
        buf.append([state.timestamp, book_mid])
        if len(buf) > BUFFER_MAX:
            del buf[: len(buf) - BUFFER_MAX]
        persistent["ticks_seen"] = persistent.get("ticks_seen", 0) + 1

        if persistent["pepper_regime"] == "LINEAR" and \
                persistent["ticks_seen"] % OLS_CHECK_INTERVAL == 0:
            window = buf[-OLS_WINDOW:]
            fit = self.run_ols(window)
            if fit is not None:
                slope, intercept, r_sq = fit
                persistent["calib_r2"] = r_sq
                check_failed = (r_sq < R2_THRESHOLD) or (slope < SLOPE_MIN)
                if check_failed:
                    persistent["fail_streak"] = persistent.get("fail_streak", 0) + 1
                    if persistent["fail_streak"] >= FAIL_STREAK_TO_DEGRADE:
                        persistent["pepper_regime"] = "MM"
                else:
                    persistent["calib_slope"] = slope
                    persistent["calib_intercept"] = intercept
                    persistent["fail_streak"] = 0

        regime = persistent["pepper_regime"]

        if regime == "LINEAR":
            slope = persistent.get("calib_slope", 0.001)
            intercept = persistent.get("calib_intercept", 12000)
            fair = intercept + slope * state.timestamp

            if not persistent.get("drift_broken", False):
                if book_mid < fair - 50:
                    persistent["drift_broken"] = True
                    persistent["pepper_regime"] = "MM"

            if not persistent.get("drift_broken", False):
                params = PEPPER_LINEAR
                take_width = self.pepper_take_width(state.timestamp, slope)
                bought, sold = 0, 0
                bought, sold = self.take_best_orders_linear(
                    product, od, fair, position, bought, sold, take_width)
                bought, sold = self.clear_position_order(
                    product, od, fair, position, bought, sold, params)
                bought, sold = self.make_orders(
                    product, od, fair, position, bought, sold, params,
                    asym_bullish=asym_bullish, asym_bearish=asym_bearish)
                return

        last_fair = persistent.get("pf", None)
        fair = self.pepper_fair_mm(od, last_fair, state.timestamp)
        persistent["pf"] = round(fair, 2)
        params = dict(PEPPER_MM)
        target = int(LIMITS[product] * params["target_position_frac"])
        deficit = target - position
        if deficit > target * params["early_aggression_trigger"]:
            params["take_width"] = params["early_aggression_take"]
            params["clear_width"] = params["early_aggression_clear"]

        bought, sold = 0, 0
        bought, sold = self.take_best_orders_mm(
            product, od, fair, position, bought, sold, params)
        bought, sold = self.clear_position_order(
            product, od, fair, position, bought, sold, params)
        bought, sold = self.make_orders(
            product, od, fair, position, bought, sold, params,
            asym_bullish=asym_bullish, asym_bearish=asym_bearish)

    def run(self, state):
        persistent = self.load_state(state.traderData)
        self.orders = {}
        self.conversions = 0
        for p in state.order_depths:
            self.orders[p] = []
        if "ASH_COATED_OSMIUM" in state.order_depths:
            self.trade_osmium(state)
        if "INTARIAN_PEPPER_ROOT" in state.order_depths:
            self.trade_pepper(state, persistent)
        self.traderData = self.save_state(persistent)
        return self.orders, self.conversions, self.traderData