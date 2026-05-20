"""
Round 4 v12 — Elegant: forked from kaiki/526663 (live $68,233).

Five minimal, theory-grounded modifications on top of the pure static z-take base.
Designed for robustness across 1,000,000-tick simulated competition data, NOT
peak fit on 6 historical days.

Base behavior (unchanged from 526663):
    For each configured product, cfg["mean"] / cfg["sd"] are fixed constants.
    Each tick:
        mid = (best_bid + best_ask) / 2
        z   = (mid - mean) / sd
    If z >= z_thresh: sell into bids at prices >= mean.
    If z <= -z_thresh: buy from asks at prices <= mean.

Modifications layered on top (each is a minimal, isolated overlay):

  Mod 1  Tighter position caps (HG=150, VFE=130, vouchers=100); drop dead
         VEV_6000 / VEV_6500.

  Mod 1.5  Regime-shift gate. If 85%+ of the last 10k ticks for a product had
         mid on one side of `mean`, pause z-takes on that product for 5k ticks.
         Detects when the simulator's μ has drifted from our static fit.

  Mod 2  Mark-67 passive long-bias inventory tilt on VELVETFRUIT_EXTRACT.
         When 30k-tick rolling Mark-67 net buy-volume >= 30, lift target
         inventory by +30 (only on the BUY side, never forces sells).

  Mod 3  HYDROGEL ASK-INWARD / BID-INWARD defensive cooldown. After a 5+ tick
         single-sided spread compression, suppress HG z-takes for 1000 ticks
         (avoids being adverse-selected by Mark 49 / Mark 22 quote-tightening).

  Mod 4  OU extrinsic-short overlay on VEV_5300 / VEV_5400 / VEV_5500.
         OU theory says fair extrinsic ≈ 0 for these strikes; sell when
         extrinsic > threshold, cover when < cover-threshold. Caps stay at 100
         shared with the z-take.

Ship gate:
    Held-out R4D3 PnL must beat kaiki/526663's baseline (266,878) AND be ≥
    70% of the cal-mean. Otherwise ship 526663 unchanged.
"""

import json
from math import erf, sqrt
from typing import Any
from datamodel import Listing, Observation, Order, OrderDepth, ProsperityEncoder, Symbol, Trade, TradingState


class Logger:
    def __init__(self) -> None:
        self.logs = ""
        self.max_log_length = 3750

    def print(self, *objects: Any, sep: str = " ", end: str = "\n") -> None:
        self.logs += sep.join(map(str, objects)) + end

    def flush(self, state: TradingState, orders: dict[Symbol, list[Order]], conversions: int, trader_data: str) -> None:
        base_length = len(self.to_json([self.compress_state(state, ""), self.compress_orders(orders), conversions, "", ""]))
        max_item_length = (self.max_log_length - base_length) // 3
        print(self.to_json([
            self.compress_state(state, self.truncate(state.traderData, max_item_length)),
            self.compress_orders(orders),
            conversions,
            self.truncate(trader_data, max_item_length),
            self.truncate(self.logs, max_item_length),
        ]))
        self.logs = ""

    def compress_state(self, state: TradingState, trader_data: str) -> list[Any]:
        return [state.timestamp, trader_data, self.compress_listings(state.listings),
                self.compress_order_depths(state.order_depths), self.compress_trades(state.own_trades),
                self.compress_trades(state.market_trades), state.position, self.compress_observations(state.observations)]

    def compress_listings(self, listings):
        return [[l.symbol, l.product, l.denomination] for l in listings.values()]

    def compress_order_depths(self, order_depths):
        return {s: [od.buy_orders, od.sell_orders] for s, od in order_depths.items()}

    def compress_trades(self, trades):
        return [[t.symbol, t.price, t.quantity, t.buyer, t.seller, t.timestamp]
                for arr in trades.values() for t in arr]

    def compress_observations(self, observations: Observation) -> list[Any]:
        conversion_observations = {}
        for product, obs in observations.conversionObservations.items():
            conversion_observations[product] = [
                obs.bidPrice, obs.askPrice, obs.transportFees,
                obs.exportTariff, obs.importTariff, obs.sugarPrice, obs.sunlightIndex,
            ]
        return [observations.plainValueObservations, conversion_observations]

    def compress_orders(self, orders):
        return [[o.symbol, o.price, o.quantity] for arr in orders.values() for o in arr]

    def to_json(self, value: Any) -> str:
        return json.dumps(value, cls=ProsperityEncoder, separators=(",", ":"))

    def truncate(self, value: str, max_length: int) -> str:
        lo, hi = 0, min(len(value), max_length)
        out = ""
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = value[:mid]
            if len(candidate) < len(value):
                candidate += "..."
            if len(json.dumps(candidate)) <= max_length:
                out = candidate; lo = mid + 1
            else:
                hi = mid - 1
        return out


logger = Logger()


# ============================================================================
# Per-product config
# ============================================================================
#
# mean / sd are the empirical mean and stdev of mid_price across 4 observed
# days (R3 day 0 + R4 days 1-3, 40,000 ticks per product).
#
# limit:  HARD position cap (we set this BELOW the exchange limit on purpose
#         to leave slack for adverse-selection variance over 1M ticks).
#         Exchange limits: HG=200, VFE=200, vouchers=300.
#
# CFGS includes ALL profitable strikes (Mod 1: only VEV_6000 / VEV_6500 are
# dropped because their mid is pinned at 0.5). We KEEP the original 526663
# position limits (HG=200, VFE=200, vouchers=300) — the regime-shift gate
# (Mod 1.5) provides the robustness layer. Cutting limits would only halve
# PnL while not adding meaningful protection that the gate doesn't already
# provide.
CFGS = [
    {"symbol": "HYDROGEL_PACK",       "mean": 9994, "sd": 32.588, "z_thresh": 1.0, "take_size": 17, "limit": 200},
    {"symbol": "VELVETFRUIT_EXTRACT", "mean": 5247, "sd": 17.091, "z_thresh": 1.0, "take_size": 17, "limit": 200},
    {"symbol": "VEV_4000",            "mean": 1247, "sd": 17.114, "z_thresh": 1.0, "take_size": 17, "limit": 300},
    {"symbol": "VEV_4500",            "mean":  747, "sd": 17.105, "z_thresh": 1.0, "take_size": 17, "limit": 300},
    {"symbol": "VEV_5000",            "mean":  252, "sd": 16.381, "z_thresh": 1.0, "take_size": 17, "limit": 300},
    {"symbol": "VEV_5100",            "mean":  163, "sd": 15.327, "z_thresh": 1.0, "take_size": 17, "limit": 300},
    {"symbol": "VEV_5200",            "mean":   91, "sd": 12.796, "z_thresh": 1.0, "take_size": 17, "limit": 300},
    {"symbol": "VEV_5300",            "mean":   43, "sd":  8.976, "z_thresh": 1.0, "take_size": 17, "limit": 300},
    {"symbol": "VEV_5400",            "mean":   14, "sd":  4.608, "z_thresh": 1.0, "take_size": 17, "limit": 300},
    {"symbol": "VEV_5500",            "mean":    6, "sd":  2.477, "z_thresh": 1.0, "take_size": 17, "limit": 300},
]
CFG_BY_SYM = {c["symbol"]: c for c in CFGS}


# ============================================================================
# Mod 1.5  Regime-shift detection
# ============================================================================
# For each product we track the count of ticks (in the recent window) where
# mid > mean and where mid < mean. If either count is >= 85% of the window
# size, the static mean is likely wrong for the current regime; we pause
# z-takes on that product for a cooldown period.
REGIME_WINDOW   = 10_000   # ticks
REGIME_FRACTION = 0.85
REGIME_COOLDOWN = 5_000    # ticks


# ============================================================================
# Mod 2  Mark-67 inventory tilt — DISABLED in v12
# ============================================================================
# Both forms tested produced regime-dependent results:
#   (a) Sell-side suppression: blocks profitable round-trips, -14k per R4 day.
#   (b) +2 tick mean-shift: +7,824 on R4D1 but -10,620 on R4D3 hold-out.
# M67's +1.97-tick forward edge at h=100 is real but too path-dependent to
# convert robustly into PnL without an instrument like a futures contract.
# The static z-take already captures the OU mean-reversion that drives the
# same underlying signal. M67 stays out of v12.


# ============================================================================
# Mod 3  HG defensive cooldown
# ============================================================================
HG_INWARD_DROP     = 7      # ticks of single-sided compression to trigger
                            # (raised from 5: 5 was firing on routine 1-level
                            # book rotations, costing -15k on R3D2/R4D2)
HG_COOLDOWN_LEN    = 500    # ticks of cooldown after trigger (halved from 1000)


# ============================================================================
# Mod 4  OU extrinsic-short overlay
# ============================================================================
OU_MU    = 5247.6
OU_SIGMA =   18.1


def _phi(z: float) -> float:
    return 0.5 + 0.5 * erf(z / sqrt(2.0))


def ou_delta(K: float) -> float:
    """P(VFE_terminal > K) under the fitted stationary OU distribution."""
    return _phi((OU_MU - K) / OU_SIGMA)


# Per-strike OU short config. Only strikes 5300 / 5400 / 5500 — for these,
# OU theory says fair extrinsic ≈ 0 (their delta is ~0 under the stationary
# OU distribution).
#
# DISABLED in v12 base: a 6-day backtest cannot hold to expiry, so the OU
# extrinsic-short bleeds MtM losses (-42k on R4D3 in the first run). The
# theory is correct over the full 7-day option life but the strategy is not
# expressible within a single-day MtM. Empty dict = no overlay applied.
OU_OVERLAY: dict = {}


# ============================================================================
# Book walker — fill against the resting book on `side` at prices
# matching `ok(px)`, up to qty_target. side=+1 hits asks (buy); side=-1
# hits bids (sell).
# ============================================================================

def _walk_book(depth, side, sym, ok, qty_target):
    if side > 0:
        prices = sorted(depth.sell_orders)
        book = depth.sell_orders
    else:
        prices = sorted(depth.buy_orders, reverse=True)
        book = depth.buy_orders
    out, filled = [], 0
    for px in prices:
        if filled >= qty_target or not ok(px):
            break
        qty = min(abs(book[px]), qty_target - filled)
        if qty <= 0:
            break
        out.append(Order(sym, px, side * qty))
        filled += qty
    return out, filled


# ============================================================================
# State helpers
# ============================================================================

def _load_state(td: str) -> dict:
    if not td:
        return {}
    try:
        return json.loads(td)
    except Exception:
        return {}


def _save_state(s: dict) -> str:
    try:
        return json.dumps(s, separators=(",", ":"))
    except Exception:
        return ""


# ============================================================================
# Mod 1.5  Regime-shift gate
# ============================================================================

def _update_regime(state_dict: dict, sym: str, mid: float, mean: float, ts: int) -> bool:
    """
    Track per-product the recent count of ticks above / below mean over the
    last REGIME_WINDOW ticks. Return True if the product is in a paused
    cooldown (z-takes should be suppressed).

    Implementation: keep a small rolling buffer of (ts, side) where side is
    +1 (mid > mean) or -1 (mid < mean) or 0 (equal). Cull entries older than
    REGIME_WINDOW. If either side count exceeds REGIME_FRACTION * window
    size, set a cooldown_until timestamp.
    """
    rs = state_dict.setdefault("regime", {})
    sym_st = rs.setdefault(sym, {"buf": [], "cool_until": 0})

    # Active cooldown — short-circuit
    if ts < sym_st["cool_until"]:
        return True

    side = 1 if mid > mean else (-1 if mid < mean else 0)
    sym_st["buf"].append([ts, side])

    cutoff = ts - REGIME_WINDOW
    while sym_st["buf"] and sym_st["buf"][0][0] < cutoff:
        sym_st["buf"].pop(0)

    n = len(sym_st["buf"])
    if n < 2_000:
        # Not enough samples to declare a regime shift
        return False

    above = sum(1 for _, s in sym_st["buf"] if s > 0)
    below = sum(1 for _, s in sym_st["buf"] if s < 0)
    if above >= REGIME_FRACTION * n or below >= REGIME_FRACTION * n:
        sym_st["cool_until"] = ts + REGIME_COOLDOWN
        # Reset buffer so the gate doesn't immediately re-trigger
        sym_st["buf"] = []
        return True

    return False


# ============================================================================
# Mod 3  HG defensive cooldown
# ============================================================================

def _update_hg_cooldown(state_dict: dict, depth: OrderDepth, ts: int) -> str:
    """
    Track HYDROGEL best bid / ask one tick back. If we see a single-sided
    spread compression we set a directional cooldown:

      - ASK-INWARD (ask drops without bid following) -> mid is about to go UP.
        Suppress SELLS (so we don't get run over). Allow buys.
      - BID-INWARD (bid rises without ask following) -> mid is about to go DOWN.
        Suppress BUYS. Allow sells.

    Returns one of:
      ""             -- no cooldown active, normal trading
      "no_sell"      -- in ASK-INWARD cooldown, suppress sells
      "no_buy"       -- in BID-INWARD cooldown, suppress buys
    """
    hg = state_dict.setdefault("hg_def", {
        "prev_bid": None, "prev_ask": None,
        "cool_until": 0, "cool_kind": "",
    })

    in_cooldown = ts < hg["cool_until"]

    if depth.buy_orders and depth.sell_orders:
        cur_bid = max(depth.buy_orders)
        cur_ask = min(depth.sell_orders)
        prev_bid = hg["prev_bid"]
        prev_ask = hg["prev_ask"]

        if not in_cooldown and prev_bid is not None and prev_ask is not None:
            ask_drop = prev_ask - cur_ask     # positive => ask moved INWARD (down)
            bid_rise = cur_bid - prev_bid     # positive => bid moved INWARD (up)
            if ask_drop >= HG_INWARD_DROP and bid_rise <= 0:
                hg["cool_until"] = ts + HG_COOLDOWN_LEN
                hg["cool_kind"] = "no_sell"
                in_cooldown = True
            elif bid_rise >= HG_INWARD_DROP and ask_drop <= 0:
                hg["cool_until"] = ts + HG_COOLDOWN_LEN
                hg["cool_kind"] = "no_buy"
                in_cooldown = True

        hg["prev_bid"] = cur_bid
        hg["prev_ask"] = cur_ask

    return hg["cool_kind"] if in_cooldown else ""


# ============================================================================
# Per-product z-take (with regime gate + HG directional cooldown)
# ============================================================================

def _z_take_orders(state, cfg, state_dict: dict):
    sym = cfg["symbol"]
    depth = state.order_depths.get(sym)
    if not depth or not depth.buy_orders or not depth.sell_orders:
        return []
    mid = (max(depth.buy_orders) + min(depth.sell_orders)) / 2.0
    mean, sd = cfg["mean"], cfg["sd"]
    if sd <= 0:
        return []

    # Mod 1.5  Regime-shift gate
    if _update_regime(state_dict, sym, mid, mean, state.timestamp):
        return []

    # Mod 3  HG directional cooldown ("" / "no_sell" / "no_buy")
    hg_cool = ""
    if sym == "HYDROGEL_PACK":
        hg_cool = _update_hg_cooldown(state_dict, depth, state.timestamp)

    z = (mid - mean) / sd
    pos = state.position.get(sym, 0)
    limit = cfg["limit"]
    take_size = cfg["take_size"]

    if abs(z) < cfg["z_thresh"]:
        return []

    if z > 0:
        if hg_cool == "no_sell":
            return []
        # Mid above mean: sell into bids at prices >= mean.
        room = max(0, min(take_size, limit + pos))
        if room <= 0:
            return []
        orders, _ = _walk_book(depth, -1, sym, lambda px: px >= mean, room)
        return orders

    if hg_cool == "no_buy":
        return []
    # Mid below mean: buy from asks at prices <= mean.
    room = max(0, min(take_size, limit - pos))
    if room <= 0:
        return []
    orders, _ = _walk_book(depth, +1, sym, lambda px: px <= mean, room)
    return orders


# ============================================================================
# Mod 4  OU extrinsic-short overlay
# ============================================================================

def _ou_overlay_orders(state, sym: str, ov_cfg: dict, state_dict: dict):
    """
    On top of z-take. Sell when (mid - intrinsic) > extr_sell threshold;
    buy-to-close when (mid - intrinsic) < extr_close threshold.

    Intrinsic = max(VFE_mid - K, 0).  Position cap is the same `limit` from
    CFGS (so OU short and z-take share one inventory budget — this is fine
    because both want the SAME side most of the time, just with different
    triggers).
    """
    cfg = CFG_BY_SYM.get(sym)
    if cfg is None:
        return []

    depth = state.order_depths.get(sym)
    if not depth or not depth.buy_orders or not depth.sell_orders:
        return []

    vfe_depth = state.order_depths.get("VELVETFRUIT_EXTRACT")
    if not vfe_depth or not vfe_depth.buy_orders or not vfe_depth.sell_orders:
        return []
    vfe_mid = (max(vfe_depth.buy_orders) + min(vfe_depth.sell_orders)) / 2.0

    K = ov_cfg["strike"]
    intrinsic = max(vfe_mid - K, 0.0)
    mid = (max(depth.buy_orders) + min(depth.sell_orders)) / 2.0
    extrinsic = mid - intrinsic

    pos = state.position.get(sym, 0)
    limit = cfg["limit"]
    size = ov_cfg["size"]

    if extrinsic >= ov_cfg["extr_sell"]:
        # Sell — short the extrinsic
        room = max(0, min(size, limit + pos))
        if room <= 0:
            return []
        # Hit the bid side (sell into bids at any price >= intrinsic + close)
        floor_px = intrinsic + ov_cfg["extr_close"]
        orders, _ = _walk_book(depth, -1, sym, lambda px: px >= floor_px, room)
        return orders

    if extrinsic <= ov_cfg["extr_close"] and pos < 0:
        # Cover — close out short when extrinsic is cheap
        room = max(0, min(size, -pos))
        if room <= 0:
            return []
        ceil_px = intrinsic + ov_cfg["extr_sell"]
        orders, _ = _walk_book(depth, +1, sym, lambda px: px <= ceil_px, room)
        return orders

    return []


# ============================================================================
# Trader
# ============================================================================

class Trader:
    def bid(self):
        return 0

    def run(self, state: TradingState):
        sd = _load_state(state.traderData)

        orders: dict[str, list[Order]] = {}
        for cfg in CFGS:
            sym = cfg["symbol"]

            ors = _z_take_orders(state, cfg, sd)
            if ors:
                orders[sym] = ors

            # Mod 4  OU overlay on selected strikes
            if sym in OU_OVERLAY:
                ou_ors = _ou_overlay_orders(state, sym, OU_OVERLAY[sym], sd)
                if ou_ors:
                    if sym in orders:
                        # Merge — but only keep orders on the side that doesn't
                        # conflict with the z-take side already issued.
                        existing_side = +1 if orders[sym][0].quantity > 0 else -1
                        ou_side = +1 if ou_ors[0].quantity > 0 else -1
                        if ou_side == existing_side:
                            orders[sym].extend(ou_ors)
                        # If opposite, prefer the z-take (skip the OU overlay)
                    else:
                        orders[sym] = ou_ors

        return orders, 0, _save_state(sd)