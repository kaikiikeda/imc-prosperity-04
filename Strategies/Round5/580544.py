"""
v11a — v10 + SQUARE standalone Bollinger MR + RECT@CIRCLE-200 threshold tune.

NEW vs v10:
  + MICROCHIP_SQUARE Bollinger MR W=1500 z=2.5  (3-day backtest min +2,313)
    Single-product MR: SQUARE has high tick vol (16-22) but no consistent drift
    (+2456/+3438/-2278 sign-flips), so deviations from rolling mean revert.
    W=1500 anchors mean against intraday drift; z=2.5 fires only on rare extremes.
  + RECT@CIRCLE-200 threshold 50 → 100  (3-day backtest min +675 → +2,857)
    Higher threshold filters small CIRCLE moves that don't predict RECT reliably.

THIS VARIANT (v11a) vs v11b: v11a = SQUARE standalone Bollinger; v11b = SQUARE@CIRCLE-100 lead-lag.

──────────────────────────────────────────────────────────────────────
v10 baseline — 3-day-cross-validated hyperparameter tuning.

Tuned hyperparameters via grid search on days 2/3/4 with minimax selection
(pick params where worst-of-3-days PnL is highest, requiring all-positive days).
This avoids day-4-only overfit.

CHANGES vs v9:

  Bollinger MR layers (W, z_in retuned):
    PEBBLES_XL              W=200  z=2.50  (UNCHANGED — already optimal: min +21,530)
    SLEEP_POD_NYLON         W=750  z=2.00  (was W=500, z=2.00)   — all 3 days +
    MICROCHIP_TRIANGLE      W=1000 z=2.50  (was W=500, z=1.50)   — fixes d2/d3 losses
    TRANSLATOR_GRAPHITE_MIST W=750 K=1.50  (was W=500, K=2.00)   — fixes d3/d4 losses

  EWMA TAKE layers (per-product alpha and edge):
    SNACKPACK_CHOCOLATE     α=0.0002 e=40  (was α=0.0002 e=20)
    SNACKPACK_VANILLA       α=0.0001 e=40  (was α=0.0002 e=20)
    SNACKPACK_STRAWBERRY    α=0.0005 e=40  (was α=0.0002 e=20)
    SNACKPACK_RASPBERRY     α=0.0001 e=40  (was α=0.0002 e=20)
    SNACKPACK_PISTACHIO     α=0.0010 e=25  (was α=0.0010 e=30)

UNCHANGED FROM v9:
  - 4 drift trades with per-product trailing-stop + auto-resume
  - RECTANGLE @ CIRCLE-200 lead-lag
  - DROPPED: BLUE/GRAY OBI (no signal in book), EB MM (penny-jump bleeds)

EXPECTED v10 SANDBOX (day-4 first 1k):
  Per backtest, full-day-4 estimates total ~+150k-200k across all layers.
  Sandbox (1k of that) ~ +25k-40k. Roughly comparable to v8 +37k baseline,
  with significantly more robust per-day behaviour on days 2/3 (live = 10 days).

THEORETICAL SOUNDNESS:
  Hyperparameters chosen by 3-day cross-validation (worst-day-positive then highest median).
  No hardcoded means/sigmas. All thresholds are window sizes and z-scores.
  Drift stops use real-time best_mid as anchor (not hardcoded reference values).
"""
from typing import Dict, List
import jsonpickle
from datamodel import TradingState, Order, OrderDepth


def _bba(od):
    if not od or not od.buy_orders or not od.sell_orders:
        return None, None, None, None
    bid = max(od.buy_orders); ask = min(od.sell_orders)
    return bid, ask, od.buy_orders[bid], -od.sell_orders[ask]


def _obi(od):
    if not od or not od.buy_orders or not od.sell_orders: return 0.0
    bv = sum(od.buy_orders.values())
    av = sum(abs(v) for v in od.sell_orders.values())
    return (bv - av) / (bv + av) if bv + av > 0 else 0.0


class Trader:
    LIMIT = 10

    # ── Pebbles XL Bollinger (UNCHANGED — already optimal at minimax) ──
    XL = "PEBBLES_XL"
    XL_W = 200
    XL_Z_IN = 2.5
    XL_Z_OUT = 0.0
    XL_Z_STOP = 4.5
    XL_WARMUP = 100

    # ── Snackpack EWMA TAKE (per-product alpha/edge — TUNED) ──
    # Format: {product: (alpha, edge)}
    SNACK_CONFIG = {
        "SNACKPACK_CHOCOLATE":  (0.0002, 40),
        "SNACKPACK_VANILLA":    (0.0001, 40),
        "SNACKPACK_STRAWBERRY": (0.0005, 40),
        "SNACKPACK_RASPBERRY":  (0.0001, 40),
        "SNACKPACK_PISTACHIO":  (0.0010, 25),  # OBI-gated
    }
    SNACK_WARMUP = 200
    PIS_OBI_GATED = "SNACKPACK_PISTACHIO"

    # ── TRIANGLE z-MR (TUNED: W=500→1000, z=1.5→2.5) ──
    TRI = "MICROCHIP_TRIANGLE"
    TRI_W = 1000
    TRI_Z_IN = 2.5
    TRI_Z_OUT = 0.0
    TRI_Z_STOP = 4.0
    TRI_WARMUP = 200

    # ── DRIFT TRADES with per-product stop + auto-resume (UNCHANGED) ──
    DRIFT_CONFIG = {
        "MICROCHIP_OVAL":      {"side": "short", "stop": 1500},
        "UV_VISOR_AMBER":      {"side": "short", "stop":  900},
        "UV_VISOR_RED":        {"side": "long",  "stop": 1300},
        "OXYGEN_SHAKE_GARLIC": {"side": "long",  "stop": 1200},
    }

    # ── NYL Bollinger (TUNED: W=500→750) ──
    NYL = "SLEEP_POD_NYLON"
    NYL_W = 750
    NYL_Z_IN = 2.0
    NYL_Z_OUT = 0.0
    NYL_Z_STOP = 4.0

    # ── RECTANGLE follows CIRCLE @ lag 200 (TUNED: thresh 50 → 100 in v11) ──
    LEADER = "MICROCHIP_CIRCLE"
    RECT = "MICROCHIP_RECTANGLE"
    RECT_LAG = 200; RECT_THRESH = 100; RECT_SIZE = 5; RECT_CAP = 5

    # ── MIST Bollinger (TUNED: W=500→750, K=2→1.5) ──
    MIST = "TRANSLATOR_GRAPHITE_MIST"
    MIST_W = 750
    MIST_K = 1.5
    MIST_STOP = 4.0
    MIST_CAP = 5
    MIST_WARMUP = 200

    # ── NEW v11a: SQUARE standalone Bollinger MR W=1500 z=2.5 ──
    SQR = "MICROCHIP_SQUARE"
    SQR_W = 1500
    SQR_Z_IN = 2.5
    SQR_Z_OUT = 0.0
    SQR_Z_STOP = 4.0
    SQR_WARMUP = 750

    # ════════════════════════════════════════════════════════════
    def run(self, state: TradingState):
        try: mem = jsonpickle.decode(state.traderData) if state.traderData else {}
        except Exception: mem = {}
        if not isinstance(mem, dict): mem = {}
        result: Dict[str, List[Order]] = {}
        positions = state.position

        # ─── Pebbles XL Bollinger MR ───
        xl_buf = mem.setdefault("xl_buf", [])
        od_xl = state.order_depths.get(self.XL)
        if od_xl is not None:
            bid_xl, ask_xl, bv_xl, av_xl = _bba(od_xl)
            if bid_xl is not None:
                mid_xl = 0.5 * (bid_xl + ask_xl)
                xl_buf.append(mid_xl)
                if len(xl_buf) > self.XL_W: xl_buf = xl_buf[-self.XL_W:]
                mem["xl_buf"] = xl_buf
                if len(xl_buf) >= self.XL_WARMUP:
                    n = len(xl_buf); mn = sum(xl_buf) / n
                    sd = (sum((x - mn) ** 2 for x in xl_buf) / n) ** 0.5
                    if sd > 0:
                        z = (mid_xl - mn) / sd
                        pos = positions.get(self.XL, 0)
                        orders = result.setdefault(self.XL, [])
                        if pos > 0 and (z >= self.XL_Z_OUT or z <= -self.XL_Z_STOP):
                            orders.append(Order(self.XL, bid_xl, -pos))
                        elif pos < 0 and (z <= self.XL_Z_OUT or z >= self.XL_Z_STOP):
                            orders.append(Order(self.XL, ask_xl, -pos))
                        elif pos == 0:
                            if z <= -self.XL_Z_IN:
                                q = min(self.LIMIT, av_xl)
                                if q > 0: orders.append(Order(self.XL, ask_xl, q))
                            elif z >= self.XL_Z_IN:
                                q = min(self.LIMIT, bv_xl)
                                if q > 0: orders.append(Order(self.XL, bid_xl, -q))

        # ─── Snackpack EWMA TAKE (per-product alpha/edge) ───
        snack_state = mem.setdefault("snack", {})
        for product, (alpha, edge) in self.SNACK_CONFIG.items():
            od = state.order_depths.get(product)
            if od is None: continue
            bid, ask, bv, av = _bba(od)
            if bid is None: continue
            mid = 0.5 * (bid + ask)
            ps = snack_state.setdefault(product, {"fair": None, "n": 0})
            ps["fair"] = mid if ps["fair"] is None else (1 - alpha) * ps["fair"] + alpha * mid
            ps["n"] += 1
            if ps["n"] < self.SNACK_WARMUP: continue
            pos = positions.get(product, 0); orders = result.setdefault(product, [])

            # PIS uses OBI gate to avoid wrong-direction takes
            obi_gate_buy = True; obi_gate_sell = True
            if product == self.PIS_OBI_GATED:
                obi_v = _obi(od)
                obi_gate_buy = obi_v >= 0
                obi_gate_sell = obi_v <= 0

            if ask < ps["fair"] - edge and pos < self.LIMIT and obi_gate_buy:
                q = min(self.LIMIT - pos, av)
                if q > 0: orders.append(Order(product, ask, q))
            if bid > ps["fair"] + edge and pos > -self.LIMIT and obi_gate_sell:
                q = min(self.LIMIT + pos, bv)
                if q > 0: orders.append(Order(product, bid, -q))
        mem["snack"] = snack_state

        # ─── TRIANGLE z-MR (TUNED W=1000 z=2.5) ───
        tri_buf = mem.setdefault("tri_buf", [])
        od = state.order_depths.get(self.TRI)
        if od is not None:
            bid, ask, bv, av = _bba(od)
            if bid is not None:
                mid = 0.5 * (bid + ask)
                tri_buf.append(mid)
                if len(tri_buf) > self.TRI_W: tri_buf = tri_buf[-self.TRI_W:]
                mem["tri_buf"] = tri_buf
                if len(tri_buf) >= self.TRI_WARMUP:
                    n = len(tri_buf); mn = sum(tri_buf) / n
                    sd = (sum((x - mn) ** 2 for x in tri_buf) / n) ** 0.5
                    if sd > 0:
                        z = (mid - mn) / sd
                        pos = positions.get(self.TRI, 0); orders = result.setdefault(self.TRI, [])
                        if pos > 0 and (z >= self.TRI_Z_OUT or z <= -self.TRI_Z_STOP):
                            orders.append(Order(self.TRI, bid, -pos))
                        elif pos < 0 and (z <= self.TRI_Z_OUT or z >= self.TRI_Z_STOP):
                            orders.append(Order(self.TRI, ask, -pos))
                        elif pos == 0:
                            if z <= -self.TRI_Z_IN:
                                q = min(self.LIMIT, av)
                                if q > 0: orders.append(Order(self.TRI, ask, q))
                            elif z >= self.TRI_Z_IN:
                                q = min(self.LIMIT, bv)
                                if q > 0: orders.append(Order(self.TRI, bid, -q))

        # ─── DRIFT TRADES with stop + auto-resume ───
        drift_state = mem.setdefault("drift", {})
        for prod, cfg in self.DRIFT_CONFIG.items():
            od = state.order_depths.get(prod)
            if od is None: continue
            bid, ask, bv, av = _bba(od)
            if bid is None: continue
            mid = 0.5 * (bid + ask)
            ds = drift_state.setdefault(prod, {"best_mid": None})
            side = cfg["side"]
            stop = cfg["stop"]

            if ds["best_mid"] is None:
                ds["best_mid"] = mid
            else:
                if side == "short":
                    if mid < ds["best_mid"]: ds["best_mid"] = mid
                else:
                    if mid > ds["best_mid"]: ds["best_mid"] = mid

            if side == "short":
                in_zone = mid <= ds["best_mid"] + stop
                target_pos = -self.LIMIT if in_zone else 0
            else:
                in_zone = mid >= ds["best_mid"] - stop
                target_pos = +self.LIMIT if in_zone else 0

            pos = positions.get(prod, 0)
            orders = result.setdefault(prod, [])
            delta = target_pos - pos
            if delta > 0:
                q = min(delta, av)
                if q > 0: orders.append(Order(prod, ask, q))
            elif delta < 0:
                q = min(-delta, bv)
                if q > 0: orders.append(Order(prod, bid, -q))
        mem["drift"] = drift_state

        # ─── NYL Bollinger (TUNED W=750) ───
        nyl_buf = mem.setdefault("nyl_buf", [])
        od = state.order_depths.get(self.NYL)
        if od is not None:
            bid, ask, bv, av = _bba(od)
            if bid is not None:
                mid = 0.5 * (bid + ask)
                nyl_buf.append(mid)
                if len(nyl_buf) > self.NYL_W: nyl_buf = nyl_buf[-self.NYL_W:]
                mem["nyl_buf"] = nyl_buf
                if len(nyl_buf) >= self.NYL_W // 2:
                    n = len(nyl_buf); mn = sum(nyl_buf) / n
                    sd = (sum((x - mn) ** 2 for x in nyl_buf) / n) ** 0.5
                    if sd > 0:
                        z = (mid - mn) / sd
                        pos = positions.get(self.NYL, 0); orders = result.setdefault(self.NYL, [])
                        if pos > 0 and (z >= self.NYL_Z_OUT or z <= -self.NYL_Z_STOP):
                            orders.append(Order(self.NYL, bid, -pos))
                        elif pos < 0 and (z <= self.NYL_Z_OUT or z >= self.NYL_Z_STOP):
                            orders.append(Order(self.NYL, ask, -pos))
                        elif pos == 0:
                            if z <= -self.NYL_Z_IN:
                                q = min(self.LIMIT, av)
                                if q > 0: orders.append(Order(self.NYL, ask, q))
                            elif z >= self.NYL_Z_IN:
                                q = min(self.LIMIT, bv)
                                if q > 0: orders.append(Order(self.NYL, bid, -q))

        # ─── RECTANGLE follows CIRCLE @ lag 200 ───
        ll_buf = mem.setdefault("ll_buf", [])
        od_lead = state.order_depths.get(self.LEADER)
        if od_lead is not None:
            bid_l, ask_l, _, _ = _bba(od_lead)
            if bid_l is not None:
                mid_l = 0.5 * (bid_l + ask_l)
                ll_buf.append(mid_l)
                if len(ll_buf) > self.RECT_LAG + 5: ll_buf = ll_buf[-(self.RECT_LAG + 5):]
                mem["ll_buf"] = ll_buf
                if len(ll_buf) >= self.RECT_LAG:
                    leader_change = ll_buf[-1] - ll_buf[-self.RECT_LAG]
                    od_f = state.order_depths.get(self.RECT)
                    if od_f is not None:
                        bid_f, ask_f, bv_f, av_f = _bba(od_f)
                        if bid_f is not None:
                            pos_f = positions.get(self.RECT, 0)
                            orders = result.setdefault(self.RECT, [])
                            if pos_f > 0 and leader_change < 0:
                                orders.append(Order(self.RECT, bid_f, -pos_f))
                            elif pos_f < 0 and leader_change > 0:
                                orders.append(Order(self.RECT, ask_f, -pos_f))
                            elif pos_f == 0:
                                if leader_change >= self.RECT_THRESH:
                                    q = min(self.RECT_SIZE, av_f)
                                    if q > 0: orders.append(Order(self.RECT, ask_f, q))
                                elif leader_change <= -self.RECT_THRESH:
                                    q = min(self.RECT_SIZE, bv_f)
                                    if q > 0: orders.append(Order(self.RECT, bid_f, -q))

        # ─── MIST Bollinger (TUNED W=750 K=1.5) ───
        mist_buf = mem.setdefault("mist_buf", [])
        od = state.order_depths.get(self.MIST)
        if od is not None:
            bid, ask, bv, av = _bba(od)
            if bid is not None:
                mid = 0.5 * (bid + ask)
                mist_buf.append(mid)
                if len(mist_buf) > self.MIST_W: mist_buf = mist_buf[-self.MIST_W:]
                mem["mist_buf"] = mist_buf
                if len(mist_buf) >= self.MIST_WARMUP:
                    n = len(mist_buf); mn = sum(mist_buf) / n
                    sd = (sum((x - mn) ** 2 for x in mist_buf) / n) ** 0.5
                    if sd > 0:
                        z = (mid - mn) / sd
                        pos = positions.get(self.MIST, 0); orders = result.setdefault(self.MIST, [])
                        if pos > 0 and (z >= 0 or z <= -self.MIST_STOP):
                            orders.append(Order(self.MIST, bid, -pos))
                        elif pos < 0 and (z <= 0 or z >= self.MIST_STOP):
                            orders.append(Order(self.MIST, ask, -pos))
                        elif pos == 0:
                            if z <= -self.MIST_K:
                                q = min(self.MIST_CAP, av)
                                if q > 0: orders.append(Order(self.MIST, ask, q))
                            elif z >= self.MIST_K:
                                q = min(self.MIST_CAP, bv)
                                if q > 0: orders.append(Order(self.MIST, bid, -q))

        # ─── NEW v11a: SQUARE standalone Bollinger MR W=1500 z=2.5 ───
        sqr_buf = mem.setdefault("sqr_buf", [])
        od = state.order_depths.get(self.SQR)
        if od is not None:
            bid, ask, bv, av = _bba(od)
            if bid is not None:
                mid = 0.5 * (bid + ask)
                sqr_buf.append(mid)
                if len(sqr_buf) > self.SQR_W: sqr_buf = sqr_buf[-self.SQR_W:]
                mem["sqr_buf"] = sqr_buf
                if len(sqr_buf) >= self.SQR_WARMUP:
                    n = len(sqr_buf); mn = sum(sqr_buf) / n
                    sd = (sum((x - mn) ** 2 for x in sqr_buf) / n) ** 0.5
                    if sd > 0:
                        z = (mid - mn) / sd
                        pos = positions.get(self.SQR, 0)
                        orders = result.setdefault(self.SQR, [])
                        if pos > 0 and (z >= self.SQR_Z_OUT or z <= -self.SQR_Z_STOP):
                            orders.append(Order(self.SQR, bid, -pos))
                        elif pos < 0 and (z <= self.SQR_Z_OUT or z >= self.SQR_Z_STOP):
                            orders.append(Order(self.SQR, ask, -pos))
                        elif pos == 0:
                            if z <= -self.SQR_Z_IN:
                                q = min(self.LIMIT, av)
                                if q > 0: orders.append(Order(self.SQR, ask, q))
                            elif z >= self.SQR_Z_IN:
                                q = min(self.LIMIT, bv)
                                if q > 0: orders.append(Order(self.SQR, bid, -q))

        # ─── persist mem ───
        try:
            traderData = jsonpickle.encode(mem)
            if len(traderData) > 12000:
                for k in ("tri_buf", "mist_buf", "nyl_buf", "sqr_buf"):
                    if k in mem and len(mem[k]) > 1500:
                        mem[k] = mem[k][-1500:]
                if "ll_buf" in mem and len(mem["ll_buf"]) > 250:
                    mem["ll_buf"] = mem["ll_buf"][-250:]
                traderData = jsonpickle.encode(mem)
        except Exception: traderData = ""

        return result, 0, traderData