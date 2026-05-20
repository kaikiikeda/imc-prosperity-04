"""
quote_engine — adaptive market-making for the cosmic spice exchange.

Three product groups handled simultaneously:
  - hydrogel pack: Ornstein-Uhlenbeck pivot with imbalance & impulse bias
  - velvetfruit extract: rolling-mean band with bootstrap warmup
  - voucher chain (8 strikes): per-strike rolling-mean band

A configurable "circuit breaker" near end-of-horizon switches the engine into
liquidate-only mode to lock in unrealized PnL before close.
"""

from math import floor as _floor, ceil as _ceil, log as _log, sqrt as _sqrt, erf as _erf, exp as _exp
import json as _json
from typing import Optional as _Optional, Dict as _Dict, List as _List

try:
    from prosperity3bt.datamodel import (
        Listing as _Listing, Observation as _Observation, Order as _Order,
        OrderDepth as _OrderDepth, ProsperityEncoder as _ProsperityEncoder,
        Symbol as _Symbol, Trade as _Trade, TradingState as _TradingState,
    )
except ModuleNotFoundError:
    from datamodel import (
        Listing as _Listing, Observation as _Observation, Order as _Order,
        OrderDepth as _OrderDepth, ProsperityEncoder as _ProsperityEncoder,
        Symbol as _Symbol, Trade as _Trade, TradingState as _TradingState,
    )

# Re-expose Order under the official name for the matching engine.
Order = _Order


# ===========================================================================
# Math helpers — closed-form Black-Scholes for European calls.
# ===========================================================================

def _phi(z):
    """Standard normal CDF via erf."""
    return 0.5 + 0.5 * _erf(z / _sqrt(2.0))


def _bs_european_call(spot, strike, t_years, vol, rate=0.0):
    """Black-Scholes European call price; falls back to intrinsic if degenerate."""
    if t_years <= 0 or vol <= 0 or spot <= 0:
        return max(spot - strike, 0.0)
    sigma_t = vol * _sqrt(t_years)
    a = (_log(spot / strike) + (rate + 0.5 * vol * vol) * t_years) / sigma_t
    b = a - sigma_t
    return spot * _phi(a) - strike * _exp(-rate * t_years) * _phi(b)


# ===========================================================================
# Compact JSON logger — required emit format for the harness.
# ===========================================================================

class _Telemetry:
    """Bounded JSON logger: emits one compact line per tick to stdout."""

    LIMIT = 3750

    def __init__(self):
        self._buf = ""

    def write(self, *parts, sep=" ", end="\n"):
        self._buf += sep.join(str(p) for p in parts) + end

    def emit(self, st, books, conv, td):
        scaffold = self._serialize([self._pack_state(st, ""), self._pack_orders(books), conv, "", ""])
        slot = (self.LIMIT - len(scaffold)) // 3
        print(self._serialize([
            self._pack_state(st, self._cap(st.traderData, slot)),
            self._pack_orders(books),
            conv,
            self._cap(td, slot),
            self._cap(self._buf, slot),
        ]))
        self._buf = ""

    def _pack_state(self, st, td):
        return [
            st.timestamp,
            td,
            [[ln.symbol, ln.product, ln.denomination] for ln in st.listings.values()],
            {sym: [od.buy_orders, od.sell_orders] for sym, od in st.order_depths.items()},
            self._pack_trades(st.own_trades),
            self._pack_trades(st.market_trades),
            st.position,
            self._pack_obs(st.observations),
        ]

    def _pack_trades(self, mp):
        flat = []
        for ts in mp.values():
            for t in ts:
                flat.append([t.symbol, t.price, t.quantity, t.buyer, t.seller, t.timestamp])
        return flat

    def _pack_obs(self, ob):
        conv = {}
        for k, v in ob.conversionObservations.items():
            conv[k] = [v.bidPrice, v.askPrice, v.transportFees, v.exportTariff, v.importTariff]
        return [ob.plainValueObservations, conv]

    def _pack_orders(self, mp):
        flat = []
        for ol in mp.values():
            for o in ol:
                flat.append([o.symbol, o.price, o.quantity])
        return flat

    def _serialize(self, v):
        return _json.dumps(v, cls=_ProsperityEncoder, separators=(",", ":"))

    def _cap(self, txt, n):
        s = txt or ""
        lo, hi = 0, min(len(s), n)
        best = ""
        while lo <= hi:
            mid = (lo + hi) >> 1
            cand = s[:mid] + ("..." if mid < len(s) else "")
            if len(_json.dumps(cand)) <= n:
                best = cand
                lo = mid + 1
            else:
                hi = mid - 1
        return best


_emitter = _Telemetry()


# ===========================================================================
# Trader — all logic lives here. Required class name for the harness.
# ===========================================================================

class Trader:
    # ---- product universe -------------------------------------------------
    MAX_LONG_OR_SHORT = {"HYDROGEL_PACK": 200, "VELVETFRUIT_EXTRACT": 200}
    VOUCHER_LIMIT = 300
    VOUCHER_AT_MONEY = {
        "VEV_5000": 5000, "VEV_5100": 5100, "VEV_5200": 5200,
        "VEV_5300": 5300, "VEV_5400": 5400, "VEV_5500": 5500,
    }
    VOUCHER_IN_MONEY = {"VEV_4000": 4000, "VEV_4500": 4500}
    VOUCHER_DEEP_OTM = {"VEV_6000": 6000, "VEV_6500": 6500}

    # ---- option pricing inputs (BS branch is currently dormant) ----------
    VOL_TABLE = {
        "VEV_4000": 0.60, "VEV_4500": 0.42,
        "VEV_5000": 0.30, "VEV_5100": 0.30, "VEV_5200": 0.30,
        "VEV_5300": 0.30, "VEV_5400": 0.30, "VEV_5500": 0.31,
        "VEV_6000": 0.40, "VEV_6500": 0.55,
    }
    INVENTORY_PER_STRIKE = {
        "VEV_4000": 120, "VEV_4500": 150,
        "VEV_5000": 280, "VEV_5100": 280, "VEV_5200": 250,
        "VEV_5300": 150, "VEV_5400": 80, "VEV_5500": 40,
    }
    BURST_PER_STRIKE = {
        "VEV_4000": 10, "VEV_4500": 12,
        "VEV_5000": 25, "VEV_5100": 25, "VEV_5200": 22,
        "VEV_5300": 15, "VEV_5400": 8, "VEV_5500": 5,
    }
    TIME_TO_EXPIRY_YR = 6.76 / 252.0

    # ---- bootstrap & impulse handling -----------------------------------
    BOOTSTRAP_LEN = 50
    BOOTSTRAP_BAND_BOOST = 5.0
    IMPULSE_GATE = 1.5
    IMPULSE_PUSHBACK = 0.30

    # ---- per-product fair-value tweaks ----------------------------------
    HYD_PIVOT = 9968
    EXT_PIVOT = 5240   # asymmetric static anchor (actual mean ~5246-5255)
    HYD_IMB_BIAS = -15
    EXT_IMB_BIAS = 0

    # ---- quote-ladder geometry (for HG/VF make-orders) ------------------
    QUOTE_TIER_OFFSETS = [1, 2, 3]
    QUOTE_TIER_SIZE = {
        "HYDROGEL_PACK": [50, 40, 30],
        "VELVETFRUIT_EXTRACT": [40, 35, 25],
    }

    # ---- end-of-horizon circuit breaker ---------------------------------
    # Modes: 'none' | 'A' | 'B' | 'C' | 'D'
    #   A: drawdown-pct kill — latches close-only when PnL drops > LOCK_PCT*peak
    #   B: drawdown-abs trim — non-latching; halve soft caps when drawdown > LOCK_ABS
    #   C: time + drawdown   — at LOCK_TICK, if PnL < (1-LOCK_PCT)*peak → flatten
    #   D: time only         — at LOCK_TICK, force close-only/flatten
    CIRCUIT_TYPE: str = "D"
    CIRCUIT_DD_FRAC: float = 0.25
    CIRCUIT_DD_DOLLAR: float = 20000.0
    CIRCUIT_BAR: int = 9815
    CIRCUIT_INV_FRAC: float = 0.5
    CIRCUIT_PEAK_FLOOR: float = 50000.0

    def __init__(self):
        # ---- HG (hydrogel) per-instance state ---------------------------
        self.hyd_band = 14
        self.hyd_skew = -0.05
        self.hyd_inv_cap = 200
        self.hyd_burst = 35
        self.hyd_quote_burst = 70
        self.hyd_last_mid: _Optional[float] = None

        # ---- VF (velvetfruit) per-instance state -----------------------
        self.ext_history: _List[float] = []
        self.ext_lookback = 1500
        self.ext_band = 8
        self.ext_skew = 0.0
        self.ext_inv_cap = 200
        self.ext_burst = 40
        self.ext_quote_burst = 60
        self.ext_last_mid: _Optional[float] = None

        # ---- voucher rolling-mean state --------------------------------
        self.vch_history: dict = {}
        self.vch_lookback = 9000
        self.vch_band = 10
        self.vch_inv_cap = 300
        self.vch_burst = 22
        self.vch_skew = 0.0

        # ---- voucher BS-branch (dormant) -------------------------------
        self.vch_band_abs = 3.0
        self.vch_band_rel = 0.03
        self.vch_bs_skew = 0.05

        # ---- deep-OTM "wings" (dormant) --------------------------------
        self.wing_inv_cap = 150
        self.wing_offer_floor = 1
        self.wing_quote_burst = 50

        # ---- circuit-breaker bookkeeping --------------------------------
        self._realized: _Dict[str, float] = {}
        self._peak_pnl: float = 0.0
        self._cb_active: bool = False
        self._tick = 0

    # ---- traderData (de)serialization (per-tick continuity) -------------

    def _serialize_state(self):
        return _json.dumps({
            "vf": self.ext_history,
            "hpm": self.hyd_last_mid,
            "vpm": self.ext_last_mid,
        })

    def _restore_state(self, blob):
        if not blob:
            return
        try:
            obj = _json.loads(blob)
            if not isinstance(obj, dict):
                return
            arr = obj.get("vf")
            if isinstance(arr, list):
                self.ext_history = [float(x) for x in arr]
            v = obj.get("hpm")
            if v is not None:
                self.hyd_last_mid = float(v)
            v = obj.get("vpm")
            if v is not None:
                self.ext_last_mid = float(v)
        except Exception:
            pass

    # ---- primary entry point --------------------------------------------

    def run(self, st):
        self._restore_state(st.traderData)
        self._tick += 1

        # Track realized cash + mark-to-market PnL for the circuit breaker.
        self._absorb_fills(st)
        snapshot_pnl = self._mtm_total(st)
        if snapshot_pnl > self._peak_pnl:
            self._peak_pnl = snapshot_pnl
        self._update_cb_state(snapshot_pnl)

        # Build orders product-by-product.
        out: _Dict[str, list] = {}
        self._trade_hydrogel(st, out)
        self._trade_velvetfruit(st, out)
        self._trade_options_bs(st, out)         # disabled — kept for completeness
        self._trade_vouchers_mr(st, out)
        self._trade_itm_bs(st, out)             # disabled
        self._trade_wings(st, out)              # disabled

        # Circuit breaker rewrites the orders dict if active.
        out = self._enforce_circuit_breaker(st, out)

        td = self._serialize_state()
        _emitter.emit(st, out, 0, td)
        return out, 0, td

    # ---- circuit-breaker plumbing ---------------------------------------

    def _absorb_fills(self, st):
        """Walk this tick's own_trades; accumulate realized cash per product."""
        for sym, lst in st.own_trades.items():
            run_sum = self._realized.get(sym, 0.0)
            for tr in lst:
                if tr.buyer == "SUBMISSION":
                    run_sum -= tr.price * tr.quantity
                else:
                    run_sum += tr.price * tr.quantity
            self._realized[sym] = run_sum

    def _mtm_total(self, st) -> float:
        s = 0.0
        for sym, od in st.order_depths.items():
            if not od.buy_orders or not od.sell_orders:
                continue
            mid = (max(od.buy_orders.keys()) + min(od.sell_orders.keys())) / 2.0
            s += self._realized.get(sym, 0.0) + st.position.get(sym, 0) * mid
        return s

    def _update_cb_state(self, pnl_now: float):
        m = self.CIRCUIT_TYPE
        if m == "none":
            return
        if m == "A":
            if (self._peak_pnl >= self.CIRCUIT_PEAK_FLOOR and
                    pnl_now < (1.0 - self.CIRCUIT_DD_FRAC) * self._peak_pnl):
                self._cb_active = True
        elif m == "B":
            if self._peak_pnl >= self.CIRCUIT_PEAK_FLOOR:
                self._cb_active = (self._peak_pnl - pnl_now) > self.CIRCUIT_DD_DOLLAR
            else:
                self._cb_active = False
        elif m == "C":
            if (self._tick >= self.CIRCUIT_BAR and
                    self._peak_pnl >= self.CIRCUIT_PEAK_FLOOR and
                    pnl_now < (1.0 - self.CIRCUIT_DD_FRAC) * self._peak_pnl):
                self._cb_active = True
        elif m == "D":
            self._cb_active = (self._tick >= self.CIRCUIT_BAR)

    def _enforce_circuit_breaker(self, st, out):
        if self.CIRCUIT_TYPE == "none" or not self._cb_active:
            return out
        m = self.CIRCUIT_TYPE
        if m in ("A", "C", "D"):
            return self._close_only_filter(st, out, force_flatten=(m in ("C", "D")))
        if m == "B":
            return self._inv_cap_trim(st, out)
        return out

    def _close_only_filter(self, st, out, *, force_flatten: bool):
        rebuilt: _Dict[str, list] = {}
        for sym, ol in out.items():
            pos = st.position.get(sym, 0)
            keep: list = []
            for o in ol:
                if pos == 0:
                    continue
                if pos > 0 and o.quantity < 0:
                    q = max(o.quantity, -pos)
                    if q != 0:
                        keep.append(_Order(o.symbol, o.price, q))
                elif pos < 0 and o.quantity > 0:
                    q = min(o.quantity, -pos)
                    if q != 0:
                        keep.append(_Order(o.symbol, o.price, q))
            if keep:
                rebuilt[sym] = keep
        if force_flatten:
            for sym, pos in st.position.items():
                if pos == 0 or sym in rebuilt:
                    continue
                if sym not in st.order_depths:
                    continue
                od = st.order_depths[sym]
                if pos > 0:
                    if od.buy_orders:
                        rebuilt[sym] = [_Order(sym, max(od.buy_orders.keys()), -pos)]
                else:
                    if od.sell_orders:
                        rebuilt[sym] = [_Order(sym, min(od.sell_orders.keys()), -pos)]
        return rebuilt

    def _inv_cap_trim(self, st, out):
        rebuilt: _Dict[str, list] = {}
        for sym, ol in out.items():
            pos = st.position.get(sym, 0)
            if sym == "HYDROGEL_PACK":
                cap = int(self.hyd_inv_cap * self.CIRCUIT_INV_FRAC)
            elif sym == "VELVETFRUIT_EXTRACT":
                cap = int(self.ext_inv_cap * self.CIRCUIT_INV_FRAC)
            elif sym in self.INVENTORY_PER_STRIKE:
                cap = int(self.vch_inv_cap * self.CIRCUIT_INV_FRAC)
            else:
                cap = int(150 * self.CIRCUIT_INV_FRAC)
            keep: list = []
            for o in ol:
                if o.quantity > 0:
                    headroom = max(0, cap - pos)
                    q = min(o.quantity, headroom)
                    if q > 0:
                        keep.append(_Order(o.symbol, o.price, q))
                elif o.quantity < 0:
                    headroom = max(0, cap + pos)
                    q = max(o.quantity, -headroom)
                    if q < 0:
                        keep.append(_Order(o.symbol, o.price, q))
            if keep:
                rebuilt[sym] = keep
        return rebuilt

    # ---- shared signal helpers ------------------------------------------

    def _book_imbalance(self, od) -> float:
        bb = max(od.buy_orders.keys())
        ba = min(od.sell_orders.keys())
        bv = od.buy_orders[bb]
        av = abs(od.sell_orders[ba])
        denom = bv + av
        return ((bv - av) / denom) if denom > 0 else 0.0

    def _impulse_pushback(self, mid_now, mid_prev) -> float:
        if mid_prev is None:
            return 0.0
        delta = mid_now - mid_prev
        if abs(delta) < self.IMPULSE_GATE:
            return 0.0
        return -self.IMPULSE_PUSHBACK * delta

    def _post_quote_tiers(self, sym, bid, ask, fair_skew, room_buy, room_sell):
        emitted: list = []
        sizes = self.QUOTE_TIER_SIZE.get(sym, [30, 20, 10])
        for off, sz in zip(self.QUOTE_TIER_OFFSETS, sizes):
            if room_buy <= 0:
                break
            px = bid + off
            if px >= fair_skew:
                break
            if px >= ask:
                break
            q = min(room_buy, sz)
            if q > 0:
                emitted.append(_Order(sym, px, q))
                room_buy -= q
        for off, sz in zip(self.QUOTE_TIER_OFFSETS, sizes):
            if room_sell <= 0:
                break
            px = ask - off
            if px <= fair_skew:
                break
            if px <= bid:
                break
            q = min(room_sell, sz)
            if q > 0:
                emitted.append(_Order(sym, px, -q))
                room_sell -= q
        return emitted

    # ---- HG: OU pivot + book-imbalance + impulse pushback ---------------

    def _trade_hydrogel(self, st, out):
        sym = "HYDROGEL_PACK"
        if sym not in st.order_depths:
            return
        od = st.order_depths[sym]
        if not od.buy_orders or not od.sell_orders:
            return
        bid = max(od.buy_orders.keys())
        ask = min(od.sell_orders.keys())
        bid_vol = od.buy_orders[bid]
        ask_vol = abs(od.sell_orders[ask])
        mid = (bid + ask) / 2.0

        imb = self._book_imbalance(od)
        push = self._impulse_pushback(mid, self.hyd_last_mid)
        self.hyd_last_mid = mid

        fair = self.HYD_PIVOT + self.HYD_IMB_BIAS * imb + push
        pos = st.position.get(sym, 0)
        fair_sk = fair - self.hyd_skew * pos

        cap_hard = self.MAX_LONG_OR_SHORT[sym]
        room_buy = cap_hard - pos
        room_sell = cap_hard + pos
        b = self.hyd_band

        moves: list = []
        if ask < fair_sk - b and pos < self.hyd_inv_cap:
            allowable = min(room_buy, self.hyd_inv_cap - pos + 30)
            qty = min(ask_vol, allowable, self.hyd_burst)
            if qty > 0:
                moves.append(_Order(sym, ask, qty))
                room_buy -= qty
        if bid > fair_sk + b and pos > -self.hyd_inv_cap:
            allowable = min(room_sell, self.hyd_inv_cap + pos + 30)
            qty = min(bid_vol, allowable, self.hyd_burst)
            if qty > 0:
                moves.append(_Order(sym, bid, -qty))
                room_sell -= qty

        moves.extend(self._post_quote_tiers(sym, bid, ask, fair_sk, room_buy, room_sell))
        if moves:
            out[sym] = moves

    # ---- VF: rolling-mean band with bootstrap-period band boost ---------

    def _trade_velvetfruit(self, st, out):
        sym = "VELVETFRUIT_EXTRACT"
        if sym not in st.order_depths:
            return
        od = st.order_depths[sym]
        if not od.buy_orders or not od.sell_orders:
            return
        bid = max(od.buy_orders.keys())
        ask = min(od.sell_orders.keys())
        bid_vol = od.buy_orders[bid]
        ask_vol = abs(od.sell_orders[ask])
        mid = (bid + ask) / 2.0

        self.ext_history.append(mid)
        if len(self.ext_history) > self.ext_lookback:
            del self.ext_history[: len(self.ext_history) - self.ext_lookback]

        imb = self._book_imbalance(od)
        push = self._impulse_pushback(mid, self.ext_last_mid)
        self.ext_last_mid = mid

        # Static anchor below actual mean — asymmetric MR exploit
        fair = self.EXT_PIVOT + self.EXT_IMB_BIAS * imb + push
        n = len(self.ext_history)
        b = self.ext_band * self.BOOTSTRAP_BAND_BOOST if n < self.BOOTSTRAP_LEN else self.ext_band

        pos = st.position.get(sym, 0)
        fair_sk = fair - self.ext_skew * pos

        cap_hard = self.MAX_LONG_OR_SHORT[sym]
        room_buy = cap_hard - pos
        room_sell = cap_hard + pos

        moves: list = []
        if ask < fair_sk - b and pos < self.ext_inv_cap:
            allowable = min(room_buy, self.ext_inv_cap - pos + 30)
            qty = min(ask_vol, allowable, self.ext_burst)
            if qty > 0:
                moves.append(_Order(sym, ask, qty))
                room_buy -= qty
        if bid > fair_sk + b and pos > -self.ext_inv_cap:
            allowable = min(room_sell, self.ext_inv_cap + pos + 30)
            qty = min(bid_vol, allowable, self.ext_burst)
            if qty > 0:
                moves.append(_Order(sym, bid, -qty))
                room_sell -= qty

        if n >= self.BOOTSTRAP_LEN:
            moves.extend(self._post_quote_tiers(sym, bid, ask, fair_sk, room_buy, room_sell))

        if moves:
            out[sym] = moves

    # ---- Voucher MR: rolling-mean per-strike band -----------------------

    def _trade_vouchers_mr(self, st, out):
        chain = ["VEV_4000", "VEV_4500", "VEV_5000", "VEV_5100",
                 "VEV_5200", "VEV_5300", "VEV_5400", "VEV_5500"]
        for sym in chain:
            if sym not in st.order_depths:
                continue
            od = st.order_depths[sym]
            if not od.buy_orders or not od.sell_orders:
                continue
            bid = max(od.buy_orders.keys())
            ask = min(od.sell_orders.keys())
            bid_vol = od.buy_orders[bid]
            ask_vol = abs(od.sell_orders[ask])
            mid = (bid + ask) / 2.0

            buf = self.vch_history.setdefault(sym, [])
            buf.append(mid)
            if len(buf) > self.vch_lookback:
                self.vch_history[sym] = buf[-self.vch_lookback:]
            if len(buf) < 20:
                continue

            avg = sum(buf) / len(buf)
            pos = st.position.get(sym, 0)
            fair_sk = avg - self.vch_skew * pos

            moves: list = []
            if ask < fair_sk - self.vch_band and pos < self.vch_inv_cap:
                room = min(300 - pos, self.vch_inv_cap - pos + 15)
                qty = min(ask_vol, room, self.vch_burst)
                if qty > 0:
                    moves.append(_Order(sym, ask, qty))
            if bid > fair_sk + self.vch_band and pos > -self.vch_inv_cap:
                room = min(300 + pos, self.vch_inv_cap + pos + 15)
                qty = min(bid_vol, room, self.vch_burst)
                if qty > 0:
                    moves.append(_Order(sym, bid, -qty))
            if moves:
                out[sym] = moves

    # ---- Voucher BS branch (DORMANT — kept for parity) ------------------

    def _trade_options_bs(self, st, out):
        return  # branch is disabled in this configuration
        if "VELVETFRUIT_EXTRACT" not in st.order_depths:
            return
        u = st.order_depths["VELVETFRUIT_EXTRACT"]
        if not u.buy_orders or not u.sell_orders:
            return
        spot = (max(u.buy_orders.keys()) + min(u.sell_orders.keys())) / 2.0
        cap_hard = self.VOUCHER_LIMIT
        for sym, k in self.VOUCHER_AT_MONEY.items():
            if sym not in st.order_depths:
                continue
            od = st.order_depths[sym]
            if not od.buy_orders or not od.sell_orders:
                continue
            bid = max(od.buy_orders.keys())
            ask = min(od.sell_orders.keys())
            bid_vol = od.buy_orders[bid]
            ask_vol = abs(od.sell_orders[ask])
            sigma = self.VOL_TABLE.get(sym, 0.24)
            theo = _bs_european_call(spot, k, self.TIME_TO_EXPIRY_YR, sigma)
            pos = st.position.get(sym, 0)
            theo_sk = theo - self.vch_bs_skew * pos
            band = max(self.vch_band_abs, self.vch_band_rel * max(theo, 1.0))
            cap_inv = self.INVENTORY_PER_STRIKE.get(sym, 50)
            cap_burst = self.BURST_PER_STRIKE.get(sym, 10)
            moves: list = []
            if ask < theo_sk - band and pos < cap_inv:
                room = min(cap_hard - pos, cap_inv - pos + 30)
                q = min(ask_vol, room, cap_burst)
                if q > 0:
                    moves.append(_Order(sym, ask, q))
            if bid > theo_sk + band and pos > -cap_inv:
                room = min(cap_hard + pos, cap_inv + pos + 30)
                q = min(bid_vol, room, cap_burst)
                if q > 0:
                    moves.append(_Order(sym, bid, -q))
            spread = ask - bid
            if spread >= 2:
                buy_room = min(cap_hard - pos, cap_inv - pos)
                sell_room = min(cap_hard + pos, cap_inv + pos)
                if buy_room > 0:
                    qb = min(bid + 1, int(_floor(theo_sk)) - 1)
                    if bid < qb < ask and qb < theo_sk:
                        q = min(buy_room, 6)
                        if q > 0:
                            moves.append(_Order(sym, qb, q))
                if sell_room > 0:
                    qa = max(ask - 1, int(_ceil(theo_sk)) + 1)
                    if bid < qa < ask and qa > theo_sk:
                        q = min(sell_room, 6)
                        if q > 0:
                            moves.append(_Order(sym, qa, -q))
            if moves:
                out[sym] = moves

    # ---- ITM BS branch (DORMANT) ----------------------------------------

    def _trade_itm_bs(self, st, out):
        return  # branch is disabled in this configuration
        if "VELVETFRUIT_EXTRACT" not in st.order_depths:
            return
        u = st.order_depths["VELVETFRUIT_EXTRACT"]
        if not u.buy_orders or not u.sell_orders:
            return
        spot = (max(u.buy_orders.keys()) + min(u.sell_orders.keys())) / 2.0
        cap_hard = self.VOUCHER_LIMIT
        for sym, k in self.VOUCHER_IN_MONEY.items():
            if sym not in st.order_depths:
                continue
            od = st.order_depths[sym]
            if not od.buy_orders or not od.sell_orders:
                continue
            bid = max(od.buy_orders.keys())
            ask = min(od.sell_orders.keys())
            bid_vol = od.buy_orders[bid]
            ask_vol = abs(od.sell_orders[ask])
            sigma = self.VOL_TABLE.get(sym, 0.40)
            theo = _bs_european_call(spot, k, self.TIME_TO_EXPIRY_YR, sigma)
            theo = max(theo, max(spot - k, 0.0))
            pos = st.position.get(sym, 0)
            theo_sk = theo - self.vch_bs_skew * pos
            band = max(2.5, 0.004 * max(theo, 1.0))
            cap_inv = self.INVENTORY_PER_STRIKE.get(sym, 80)
            cap_burst = self.BURST_PER_STRIKE.get(sym, 8)
            moves: list = []
            if ask < theo_sk - band and pos < cap_inv:
                room = min(cap_hard - pos, cap_inv - pos + 15)
                q = min(ask_vol, room, cap_burst)
                if q > 0:
                    moves.append(_Order(sym, ask, q))
            if bid > theo_sk + band and pos > -cap_inv:
                room = min(cap_hard + pos, cap_inv + pos + 15)
                q = min(bid_vol, room, cap_burst)
                if q > 0:
                    moves.append(_Order(sym, bid, -q))
            if moves:
                out[sym] = moves

    # ---- Deep-OTM wing seller (DORMANT) ---------------------------------

    def _trade_wings(self, st, out):
        return  # branch is disabled in this configuration
        for sym in self.VOUCHER_DEEP_OTM:
            if sym not in st.order_depths:
                continue
            od = st.order_depths[sym]
            if not od.sell_orders:
                continue
            ask = min(od.sell_orders.keys())
            if ask != self.wing_offer_floor:
                continue
            pos = st.position.get(sym, 0)
            cap_hard = self.VOUCHER_LIMIT
            sell_room = cap_hard + pos
            if sell_room <= 0 or pos <= -self.wing_inv_cap:
                continue
            q = min(sell_room, self.wing_inv_cap + pos, self.wing_quote_burst)
            if q > 0:
                out.setdefault(sym, []).append(_Order(sym, self.wing_offer_floor, -q))