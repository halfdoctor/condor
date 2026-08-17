#!/usr/bin/env python3
"""
Derivatives Monkey Live Dynamic Data Extractor & Signal Engine
Extracts live market intelligence (Vol Edge, GEX, Gamma Flip, Skew, Term Structure)
from derivativesmonkey.com and computes strategy entry signals for Short-Vol Iron Condors
and Long-Vol structures.
"""

import sys
import os
import json
import time
import urllib.request
import urllib.error
import argparse
from datetime import datetime, timezone

BASE_URL = "https://www.derivativesmonkey.com"

# ANSI Color Codes for Terminal Output
class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'
    DIM = '\033[2m'

class DerivativesMonkeyExtractor:
    def __init__(self, base_url: str = BASE_URL, timeout: int = 8):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) DerivativesMonkeyExtractor/1.0'
        }

    def _fetch_json(self, endpoint: str, params: dict = None) -> dict:
        url = f"{self.base_url}{endpoint}"
        if params:
            query_str = "&".join([f"{k}={v}" for k, v in params.items()])
            url = f"{url}?{query_str}"
        
        req = urllib.request.Request(url, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                content = resp.read().decode('utf-8')
                return json.loads(content)
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}: {e.reason}", "url": url}
        except urllib.error.URLError as e:
            return {"error": f"URL Error: {e.reason}", "url": url}
        except Exception as e:
            return {"error": str(e), "url": url}

    def _fetch_live_exchange_price(self, symbol: str) -> tuple:
        """Fetch instantaneous real-time spot/index price from Deribit or Binance."""
        sym = symbol.upper()
        # 1. Deribit Options Index
        try:
            req = urllib.request.Request(
                f"https://www.deribit.com/api/v2/public/get_index_price?index_name={sym.lower()}_usd",
                headers=self.headers
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                if "result" in data and "index_price" in data["result"]:
                    return float(data["result"]["index_price"]), "Deribit Index"
        except Exception:
            pass

        # 2. Binance Spot Ticker
        try:
            req = urllib.request.Request(
                f"https://api.binance.com/api/v3/ticker/price?symbol={sym}USDT",
                headers=self.headers
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                if "price" in data:
                    return float(data["price"]), "Binance Spot"
        except Exception:
            pass

        return None, "Derivatives Monkey"

    def get_live_signals(self, asset: str = "ETH") -> dict:
        asset_upper = asset.upper()
        
        # 1. Fetch real-time exchange spot price
        live_spot, spot_source = self._fetch_live_exchange_price(asset_upper)

        # 2. Fetch Greeks & Surface data from Derivatives Monkey
        ms_data = self._fetch_json("/api/market/market-summary", {"asset": asset_upper})
        gf_data = self._fetch_json("/api/market/gamma-flip", {"asset": asset_upper})
        ivr_data = self._fetch_json("/api/market/ivr-gamma-flip", {"asset": asset_upper})
        atm_rv_data = self._fetch_json("/api/market/atm-vs-rv", {"asset": asset_upper})
        skew_data = self._fetch_json("/api/market/skew-by-maturity", {"asset": asset_upper})
        exp_data = self._fetch_json("/api/market/expiry-exposure", {"asset": asset_upper})

        timestamp_iso = datetime.now(timezone.utc).isoformat()

        # Snapshot Spot from Derivatives Monkey batch Greeks calculation
        gex_snapshot_spot = ms_data.get("spot", {}).get("value") or gf_data.get("spot") or ivr_data.get("current_spot") or 0.0

        # Effective Spot Price used for live delta and clearance
        spot_val = live_spot if (live_spot is not None and live_spot > 0) else gex_snapshot_spot

        # --- 1) Volatility Edge / VRP Panel ---
        atm_iv_7d = ms_data.get("atm_iv_7d", {}).get("value")
        atm_iv_30d = ms_data.get("atm_iv_30d", {}).get("value")
        rv_val = ms_data.get("rv", {}).get("value")
        
        # 30D VRP (primary driver for 14-30 DTE condors)
        vrp_30d = None
        if atm_iv_30d is not None and rv_val is not None:
            vrp_30d = atm_iv_30d - rv_val
        elif ms_data.get("rv", {}).get("vrp") is not None:
            vrp_30d = ms_data.get("rv", {}).get("vrp")

        # 7D VRP
        vrp_7d = None
        if atm_iv_7d is not None and rv_val is not None:
            vrp_7d = atm_iv_7d - rv_val

        iv_rank = ivr_data.get("iv_rank")
        iv_percentile = ms_data.get("iv_percentile", {}).get("percentile")

        # Determine vol regime
        if (vrp_30d is not None and vrp_30d >= 4.0) or (iv_rank is not None and iv_rank >= 75):
            vol_regime = "rich"
        elif (vrp_30d is not None and vrp_30d >= 2.0) or (iv_rank is not None and iv_rank >= 50):
            vol_regime = "elevated"
        elif (vrp_30d is not None and vrp_30d >= -1.0):
            vol_regime = "fair"
        else:
            vol_regime = "cheap"

        # --- 2) Gamma Exposure (GEX) & Gamma Flip ---
        net_gamma_val = ms_data.get("net_gamma", {}).get("value")
        net_gamma_regime = gf_data.get("regime") or ms_data.get("net_gamma", {}).get("regime") or "balanced"
        
        # Live Gamma Flip Level (Priority: live gamma-flip or market-summary)
        gamma_flip_strike = gf_data.get("flip_strike") or ms_data.get("gamma_flip", {}).get("strike")
        
        distance_pct = None
        distance_usd = None
        if spot_val > 0 and gamma_flip_strike is not None:
            distance_usd = spot_val - gamma_flip_strike
            distance_pct = (distance_usd / spot_val) * 100.0

        gex_profile = gf_data.get("gex_profile", [])
        gamma_above_flip = ivr_data.get("gamma_above_flip")
        gamma_below_flip = ivr_data.get("gamma_below_flip")

        # Determine GEX Regime (positive, fragile, negative)
        spot_above_flip = False
        if gamma_flip_strike is not None and spot_val > 0:
            spot_above_flip = (spot_val >= gamma_flip_strike)

        if net_gamma_regime in ["long_gamma", "positive"] and spot_above_flip:
            gex_regime = "positive"
        elif net_gamma_regime in ["long_gamma", "positive"] and not spot_above_flip:
            gex_regime = "fragile"
        else:
            gex_regime = "negative"

        # --- 3) Skew / Moneyness / Distribution & Tail Risk ---
        pc_ratio = ms_data.get("pc", {}).get("ratio")
        raw_call_wall = ms_data.get("call_wall", {})
        raw_put_wall = ms_data.get("put_wall", {})

        # Parse GEX profile for True OTM Resistance (Call Wall) and Support (Put Wall)
        # Find major positive GEX resistance strikes above spot (> 1.03 * spot)
        res_strikes = [p for p in gex_profile if p.get("strike") and p["strike"] >= spot_val * 1.03 and p.get("net_gex", 0) > 0]
        res_strikes.sort(key=lambda x: x.get("net_gex", 0), reverse=True)
        otm_call_resistance = res_strikes[0]["strike"] if res_strikes else None

        # Find major put strike below flip (< 0.97 * flip)
        supp_strikes = [p for p in gex_profile if p.get("strike") and p["strike"] <= (gamma_flip_strike or spot_val) * 0.97]
        supp_strikes.sort(key=lambda x: x.get("strike"), reverse=True)
        otm_put_support = supp_strikes[0]["strike"] if supp_strikes else None

        skew_items = skew_data.get("items", [])
        near_rr_25d = None
        if skew_items:
            valid_rr = [item.get("rr_25d") for item in skew_items if item.get("rr_25d") is not None]
            if valid_rr:
                near_rr_25d = sum(valid_rr[:5]) / len(valid_rr[:5])

        if near_rr_25d is not None:
            if near_rr_25d < -6.0:
                skew_bias = "panic_put_skew"
            elif near_rr_25d < -3.0 or (pc_ratio is not None and pc_ratio > 0.85):
                skew_bias = "heavy_put_skew"
            elif near_rr_25d > 3.0:
                skew_bias = "heavy_call_skew"
            else:
                skew_bias = "balanced"
        else:
            skew_bias = ms_data.get("pc", {}).get("tag", "balanced")

        # --- 4) Term Structure / Expiry View (Contango vs Backwardation) ---
        term_spread = ms_data.get("term", {}).get("spread_vp")
        if term_spread is None and atm_iv_30d is not None and atm_iv_7d is not None:
            term_spread = atm_iv_30d - atm_iv_7d

        term_tag = ms_data.get("term", {}).get("tag")
        if not term_tag or term_tag not in ["contango", "backwardation"]:
            if (term_spread or 0) >= 0:
                term_tag = "contango"
            elif (term_spread or 0) >= -3.0:
                term_tag = "mild_backwardation"
            else:
                term_tag = "deep_backwardation"

        top_expiry = ms_data.get("top_expiry", {})

        # --- 5) Decision Matrix & Trade Signal Evaluation ---
        reasons = []
        rule_vol_pass = False
        rule_gex_pass = False
        rule_spot_pass = False
        rule_term_pass = False
        rule_skew_pass = False

        # Rule 1: Volatility Edge
        vrp_display = f"{vrp_30d:+.2f}" if vrp_30d is not None else "N/A"
        if vol_regime in ["rich", "elevated"] or (vrp_30d is not None and vrp_30d >= 2.0):
            rule_vol_pass = True
            reasons.append(f"[PASS] Volatility edge is {vol_regime.upper()} (30D VRP: {vrp_display} vol pts, IV Rank: {iv_rank if iv_rank is not None else 'N/A'}).")
        else:
            reasons.append(f"[FAIL] Volatility edge is {vol_regime.upper()} (30D VRP: {vrp_display} vol pts). Premium is not rich enough for selling vol.")

        # Rule 2: GEX Regime
        if gex_regime == "positive":
            rule_gex_pass = True
            reasons.append(f"[PASS] Net Gamma regime is POSITIVE ({net_gamma_regime}). Dealer hedging dampens volatility and reduces perp rebalance churn.")
        elif gex_regime == "fragile":
            reasons.append(f"[WARN] Net Gamma is positive but Spot is hovering near/below Gamma Flip level. Structurally fragile.")
        else:
            reasons.append(f"[FAIL] Net Gamma regime is NEGATIVE / BALANCED ({net_gamma_regime}). High trend / whipsaw risk.")

        # Rule 3: Spot vs Gamma Flip
        dist_display = f"{distance_pct:+.2f}%" if distance_pct is not None else "N/A"
        flip_display = f"${gamma_flip_strike:,.2f}" if gamma_flip_strike is not None else "N/A"
        if spot_above_flip and distance_pct is not None and distance_pct > 0.05:
            rule_spot_pass = True
            reasons.append(f"[PASS] Spot price (${spot_val:,.2f}) is safely above Gamma Flip ({flip_display}) by {dist_display}.")
        else:
            reasons.append(f"[FAIL] Spot price (${spot_val:,.2f}) is AT or BELOW Gamma Flip ({flip_display}).")

        # Rule 4: Term Structure (VIX / IV Contango vs Backwardation)
        spread_display = f"{term_spread:+.2f}" if term_spread is not None else "N/A"
        if term_tag == "contango" or (term_spread is not None and term_spread >= 0):
            rule_term_pass = True
            reasons.append(f"[PASS] Term Structure is in CONTANGO (30D-7D spread: {spread_display} vol pts). Positive theta capture slope.")
        elif term_tag == "mild_backwardation" or (term_spread is not None and term_spread >= -3.0):
            rule_term_pass = True
            reasons.append(f"[WARN] Term Structure in MILD BACKWARDATION (30D-7D spread: {spread_display} vol pts). VRP inversion risk; monitor or reduce size.")
        else:
            rule_term_pass = False
            reasons.append(f"[FAIL] Term Structure in DEEP BACKWARDATION (30D-7D spread: {spread_display} vol pts). Volatility stress / event risk. Avoid short vol.")

        # Rule 5: Skew / Tail Risk Assessment
        rr_display = f"{near_rr_25d:.2f}" if near_rr_25d is not None else "N/A"
        if skew_bias == "balanced":
            rule_skew_pass = True
            reasons.append(f"[PASS] Skew is BALANCED (25D Risk Reversal: {rr_display}). Standard symmetric wings allowed.")
        elif skew_bias == "heavy_put_skew":
            rule_skew_pass = True
            reasons.append(f"[WARN] HEAVY PUT SKEW detected (25D RR: {rr_display}). Heightened downside tail risk; recommend asymmetric wider put wing.")
        elif skew_bias == "heavy_call_skew":
            rule_skew_pass = True
            reasons.append(f"[PASS] CALL SKEW bias (25D RR: {rr_display}). Upside call demand dominant.")
        else:
            rule_skew_pass = False
            reasons.append(f"[FAIL] EXTREME / PANIC PUT SKEW (25D RR: {rr_display}). Downside panic; avoid standard short vol.")

        # Iron Condor signal decision
        short_vol_allowed = rule_vol_pass and rule_gex_pass and rule_spot_pass and rule_term_pass and rule_skew_pass
        long_vol_allowed = (vol_regime == "cheap") or (gex_regime == "negative") or (term_tag == "deep_backwardation")

        if short_vol_allowed:
            if skew_bias == "heavy_put_skew":
                rec_strategy = "SHORT_VOL_IRON_CONDOR_ASYMMETRIC_PUT"
            else:
                rec_strategy = "SHORT_VOL_IRON_CONDOR"
            
            base_conf = 70
            if vol_regime == "rich": base_conf += 10
            if term_tag == "contango": base_conf += 10
            if distance_pct and distance_pct > 0.5: base_conf += 5
            confidence = min(base_conf, 95)
        elif long_vol_allowed:
            rec_strategy = "LONG_VOL_STRADDLE"
            confidence = 65
        else:
            rec_strategy = "HOLD_NEUTRAL"
            confidence = 50

        # --- 6) Standardized Strike Increment Calculation ---
        # Derive standard strike increment based on asset price scale
        if spot_val > 10000: strike_step = 500
        elif spot_val > 1000: strike_step = 25
        elif spot_val > 100: strike_step = 5
        elif spot_val > 10: strike_step = 1
        else: strike_step = 0.05

        def round_strike(val, step=strike_step):
            if not val: return None
            return round(round(val / step) * step, 2)

        # Suggested Tradeable Strikes
        rec_short_call = None
        rec_long_call = None
        rec_short_put = None
        rec_long_put = None
        asymmetric_put_wing = False
        wing_note = "Standard symmetric wings (10–15 delta)"

        if spot_val > 0:
            # Short Call: Major Positive GEX Resistance >= 1.05 * spot, or default 1.06 * spot
            if otm_call_resistance and otm_call_resistance >= spot_val * 1.04:
                rec_short_call = round_strike(otm_call_resistance)
            else:
                rec_short_call = round_strike(spot_val * 1.06)

            # Long Call Wing: Placed 5% above short call
            wing_width = max(strike_step * 2, round_strike((rec_short_call - spot_val) * 0.5))
            rec_long_call = round_strike(rec_short_call + wing_width)

            # Short Put: Placed safely below Gamma Flip and <= 0.94 * spot
            if skew_bias == "heavy_put_skew":
                asymmetric_put_wing = True
                target_short_put = min((gamma_flip_strike or spot_val) * 0.94, spot_val * 0.90)
                rec_short_put = round_strike(target_short_put)
                put_wing_width = wing_width * 1.5
                wing_note = "Asymmetric Put Wing: Short put moved 1.5x wider OTM due to put skew"
            else:
                target_short_put = min((gamma_flip_strike or spot_val) * 0.96, spot_val * 0.93)
                if otm_put_support and otm_put_support <= target_short_put:
                    rec_short_put = round_strike(otm_put_support)
                else:
                    rec_short_put = round_strike(target_short_put)
                put_wing_width = wing_width

            rec_long_put = round_strike(rec_short_put - put_wing_width)

        return {
            "metadata": {
                "asset": asset_upper,
                "timestamp": timestamp_iso,
                "source_url": f"{self.base_url}/{asset.lower()}/dashboard"
            },
            "spot_price": {
                "value": spot_val,
                "live_exchange_spot": live_spot,
                "gex_snapshot_spot": gex_snapshot_spot,
                "source": spot_source
            },
            "volatility_edge": {
                "iv_7d": atm_iv_7d,
                "iv_30d": atm_iv_30d,
                "rv": rv_val,
                "vrp": vrp_30d,
                "vrp_30d": vrp_30d,
                "vrp_7d": vrp_7d,
                "iv_rank": iv_rank,
                "iv_percentile": iv_percentile,
                "vol_regime": vol_regime
            },
            "gamma_exposure": {
                "net_gamma_value": net_gamma_val,
                "net_gamma_regime": net_gamma_regime,
                "gamma_flip_level": gamma_flip_strike,
                "distance_usd": distance_usd,
                "distance_pct": distance_pct,
                "spot_above_flip": spot_above_flip,
                "gamma_above_flip": gamma_above_flip,
                "gamma_below_flip": gamma_below_flip,
                "gex_regime": gex_regime,
                "otm_call_resistance": otm_call_resistance,
                "otm_put_support": otm_put_support
            },
            "skew_and_distribution": {
                "put_call_ratio": pc_ratio,
                "max_oi_call_strike": raw_call_wall.get("strike"),
                "max_oi_put_strike": raw_put_wall.get("strike"),
                "otm_call_resistance": otm_call_resistance,
                "otm_put_support": otm_put_support,
                "avg_25d_rr": near_rr_25d,
                "skew_bias": skew_bias
            },
            "term_structure": {
                "term_spread_30d_7d": term_spread,
                "term_tag": term_tag,
                "top_expiry": top_expiry
            },
            "signal": {
                "short_vol_iron_condor_allowed": short_vol_allowed,
                "long_vol_allowed": long_vol_allowed,
                "recommended_strategy": rec_strategy,
                "confidence_score": confidence,
                "rule_evaluation": {
                    "volatility_rule_passed": rule_vol_pass,
                    "gamma_regime_rule_passed": rule_gex_pass,
                    "spot_vs_flip_rule_passed": rule_spot_pass,
                    "term_structure_rule_passed": rule_term_pass,
                    "skew_rule_passed": rule_skew_pass
                },
                "reasons": reasons,
                "suggested_strikes": {
                    "long_put_wing": rec_long_put,
                    "short_put": rec_short_put,
                    "short_call": rec_short_call,
                    "long_call_wing": rec_long_call,
                    "asymmetric_put_wing_recommended": asymmetric_put_wing,
                    "wing_note": wing_note
                }
            }
        }


def render_terminal_dashboard(signals: dict):
    asset = signals["metadata"]["asset"]
    ts = signals["metadata"]["timestamp"]
    spot = signals["spot_price"]["value"]
    
    vol = signals["volatility_edge"]
    gex = signals["gamma_exposure"]
    skew = signals["skew_and_distribution"]
    term = signals["term_structure"]
    sig = signals["signal"]

    c = Colors

    def tag_color(text, positive=True, neutral=False):
        if neutral:
            return f"{c.WARNING}{text}{c.ENDC}"
        return f"{c.OKGREEN}{text}{c.ENDC}" if positive else f"{c.FAIL}{text}{c.ENDC}"

    print("\n" + "="*75)
    print(f"{c.BOLD}{c.HEADER}  DERIVATIVES MONKEY LIVE INTELLIGENCE DASHBOARD [{asset}]{c.ENDC}")
    print(f"{c.DIM}  Extracted at: {ts} | Source: {signals['metadata']['source_url']}{c.ENDC}")
    print("="*75)
    
    spot_source = signals["spot_price"].get("source", "Live Exchange")
    gex_snap = signals["spot_price"].get("gex_snapshot_spot")
    snap_info = f" (GEX Calc Base: ${gex_snap:,.2f})" if (gex_snap and abs(gex_snap - spot) > 1.0) else ""
    print(f"\n{c.BOLD} SPOT PRICE:{c.ENDC} ${spot:,.2f} [{spot_source}]{c.DIM}{snap_info}{c.ENDC}")

    # Panel 1: Vol Edge & Term Structure
    vrp_30_str = f"{vol['vrp_30d']:+.2f} vol pts" if vol['vrp_30d'] is not None else "N/A"
    vrp_7_str = f"{vol['vrp_7d']:+.2f} vol pts" if vol['vrp_7d'] is not None else "N/A"
    iv_rank_str = f"{vol['iv_rank']:.1f}" if vol['iv_rank'] is not None else "N/A"
    vol_pass = vol['vol_regime'] in ['rich', 'elevated']
    term_sprd = term['term_spread_30d_7d']
    term_sprd_str = f"{term_sprd:+.2f} vol pts" if term_sprd is not None else "N/A"
    term_pass = term['term_tag'] == 'contango'

    print(f"\n{c.BOLD}┌─ 1. VOLATILITY EDGE & TERM STRUCTURE PANEL ───────────────────────────────┐{c.ENDC}")
    print(f"│ 30D IV: {vol['iv_30d'] or 0:.2f}%  |  7D IV: {vol['iv_7d'] or 0:.2f}%  |  RV: {vol['rv'] or 0:.2f}%")
    print(f"│ 30D VRP (IV-RV): {vrp_30_str:<10} | 7D VRP: {vrp_7_str:<10} | IV Rank: {iv_rank_str:<6}")
    print(f"│ Vol Regime: {tag_color(vol['vol_regime'].upper(), vol_pass)}  |  Term Structure: {tag_color(term['term_tag'].upper() + ' (' + term_sprd_str + ')', term_pass)}")
    print(f"└───────────────────────────────────────────────────────────────────────────┘")

    # Panel 2: GEX & Gamma Flip
    flip_lvl = gex['gamma_flip_level']
    flip_str = f"${flip_lvl:,.2f}" if flip_lvl else "N/A"
    dist_str = f"{gex['distance_pct']:+.2f}%" if gex['distance_pct'] is not None else "N/A"
    gex_pass = gex['gex_regime'] == 'positive'
    call_res = f"${gex['otm_call_resistance']:,.2f}" if gex['otm_call_resistance'] else "N/A"
    put_supp = f"${gex['otm_put_support']:,.2f}" if gex['otm_put_support'] else "N/A"

    print(f"\n{c.BOLD}┌─ 2. GAMMA EXPOSURE (GEX) & GAMMA FLIP PANEL ─────────────────────────────┐{c.ENDC}")
    print(f"│ Net Gamma: {gex['net_gamma_value'] or 0:,.0f} ({gex['net_gamma_regime']})")
    print(f"│ Gamma Flip Level: {flip_str:<14} | Spot vs Flip: {dist_str}")
    print(f"│ GEX Resistance (Call): {call_res:<9} | GEX Support (Put): {put_supp}")
    print(f"│ GEX Regime: {tag_color(gex['gex_regime'].upper(), gex_pass)}")
    print(f"└───────────────────────────────────────────────────────────────────────────┘")

    # Panel 3: Skew & Moneyness
    pc_str = f"{skew['put_call_ratio']:.2f}" if skew['put_call_ratio'] is not None else "N/A"
    max_oi_call = skew.get('max_oi_call_strike')
    max_oi_put = skew.get('max_oi_put_strike')
    rr_str = f"{skew['avg_25d_rr']:.2f}" if skew['avg_25d_rr'] is not None else "N/A"
    print(f"\n{c.BOLD}┌─ 3. SKEW, MONEYNESS & TAIL RISK PANEL ───────────────────────────────────┐{c.ENDC}")
    print(f"│ Put/Call Ratio: {pc_str:<8} | 25-Delta Risk Reversal: {rr_str}")
    print(f"│ Max-OI Pin Strike: ${max_oi_call or 0:,.2f} (Call OI) / ${max_oi_put or 0:,.2f} (Put OI)")
    print(f"│ Skew Bias: {c.OKCYAN}{skew['skew_bias'].upper()}{c.ENDC}")
    print(f"└───────────────────────────────────────────────────────────────────────────┘")

    # Panel 4: Strategy Decision
    rec = sig['recommended_strategy']
    is_short_vol = sig['short_vol_iron_condor_allowed']
    rec_color = c.OKGREEN if is_short_vol else (c.WARNING if rec == 'HOLD_NEUTRAL' else c.OKBLUE)
    
    print(f"\n{c.BOLD}┌─ 4. SIGNAL & STRATEGY DECISION MATRIX ───────────────────────────────────┐{c.ENDC}")
    print(f"│ RECOMMENDED STRATEGY: {rec_color}{c.BOLD}{rec}{c.ENDC} (Confidence: {sig['confidence_score']}%)")
    print(f"│ Short-Vol Iron Condor Allowed: {tag_color(str(is_short_vol).upper(), is_short_vol)}")
    print(f"│")
    print(f"│ Decision Rule Checklist:")
    for reason in sig['reasons']:
        if "[PASS]" in reason:
            print(f"│   {c.OKGREEN}✔{c.ENDC} {reason.replace('[PASS]', '').strip()}")
        elif "[WARN]" in reason:
            print(f"│   {c.WARNING}⚠{c.ENDC} {reason.replace('[WARN]', '').strip()}")
        else:
            print(f"│   {c.FAIL}✖{c.ENDC} {reason.replace('[FAIL]', '').strip()}")
    
    st = sig.get('suggested_strikes', {})
    if st.get('short_put') and st.get('short_call'):
        print(f"│")
        print(f"│ Recommended Tradeable 4-Leg Iron Condor Structure:")
        print(f"│   • Long Put Wing  : ${st['long_put_wing']:,.2f} (Tail protection)")
        print(f"│   • Short Put Leg  : ${st['short_put']:,.2f} (Below Gamma Flip ${flip_lvl:,.2f})")
        print(f"│   • Short Call Leg : ${st['short_call']:,.2f} (At Positive GEX Resistance)")
        print(f"│   • Long Call Wing : ${st['long_call_wing']:,.2f} (Tail protection)")
        print(f"│   • Wing Structure : {st['wing_note']}")
    print(f"└───────────────────────────────────────────────────────────────────────────┘\n")


def get_derivatives_monkey_signals(asset: str = "ETH") -> dict:
    """Helper function to be called programmatically by trading agents or routines."""
    extractor = DerivativesMonkeyExtractor()
    return extractor.get_live_signals(asset=asset)


def main():
    parser = argparse.ArgumentParser(description="Derivatives Monkey Live Dynamic Data Extractor")
    parser.add_argument("-a", "--asset", type=str, default="ETH", help="Asset symbol (default: ETH, options: BTC, ETH, SOL, etc.)")
    parser.add_argument("-j", "--json", action="store_true", help="Output raw JSON instead of dashboard formatting")
    parser.add_argument("-o", "--output", type=str, default=None, help="Save extracted JSON to specified file path")
    parser.add_argument("-w", "--watch", action="store_true", help="Watch mode: continuously extract and display data")
    parser.add_argument("-i", "--interval", type=int, default=15, help="Polling interval in seconds for watch mode (default: 15)")

    args = parser.parse_args()
    extractor = DerivativesMonkeyExtractor()

    while True:
        try:
            signals = extractor.get_live_signals(asset=args.asset)

            if args.output:
                with open(args.output, 'w') as f:
                    json.dump(signals, f, indent=2)

            if args.json:
                print(json.dumps(signals, indent=2))
            else:
                if args.watch:
                    os.system('cls' if os.name == 'nt' else 'clear')
                render_terminal_dashboard(signals)

        except Exception as e:
            print(f"{Colors.FAIL}Error extracting live data: {e}{Colors.ENDC}", file=sys.stderr)

        if not args.watch:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
