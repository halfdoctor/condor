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

    def get_live_signals(self, asset: str = "ETH") -> dict:
        asset_upper = asset.upper()
        
        # 1. Fetch data from endpoints
        ms_data = self._fetch_json("/api/market/market-summary", {"asset": asset_upper})
        ivr_data = self._fetch_json("/api/market/ivr-gamma-flip", {"asset": asset_upper})
        gf_data = self._fetch_json("/api/market/gamma-flip", {"asset": asset_upper})
        atm_rv_data = self._fetch_json("/api/market/atm-vs-rv", {"asset": asset_upper})
        skew_data = self._fetch_json("/api/market/skew-by-maturity", {"asset": asset_upper})
        exp_data = self._fetch_json("/api/market/expiry-exposure", {"asset": asset_upper})

        timestamp_iso = datetime.now(timezone.utc).isoformat()

        # Parse Spot Price
        spot_val = ms_data.get("spot", {}).get("value") or ivr_data.get("current_spot") or gf_data.get("spot")
        if spot_val is None:
            spot_val = 0.0

        # --- 1) Volatility Edge / VRP Panel ---
        atm_iv_7d = ms_data.get("atm_iv_7d", {}).get("value")
        atm_iv_30d = ms_data.get("atm_iv_30d", {}).get("value") or ivr_data.get("iv_current")
        rv_val = ms_data.get("rv", {}).get("value")
        vrp_val = ms_data.get("rv", {}).get("vrp")
        
        if vrp_val is None and atm_iv_30d is not None and rv_val is not None:
            vrp_val = atm_iv_30d - rv_val

        iv_rank = ivr_data.get("iv_rank")
        iv_percentile = ms_data.get("iv_percentile", {}).get("percentile")

        # Determine vol regime (rich, elevated, fair, cheap)
        if vrp_val is not None and vrp_val >= 4.0 or (iv_rank is not None and iv_rank >= 70):
            vol_regime = "rich"
        elif vrp_val is not None and vrp_val >= 2.0 or (iv_rank is not None and iv_rank >= 50):
            vol_regime = "elevated"
        elif vrp_val is not None and vrp_val >= -2.0:
            vol_regime = "fair"
        else:
            vol_regime = "cheap"

        # --- 2) Gamma Exposure (GEX) & Gamma Flip ---
        net_gamma_val = ms_data.get("net_gamma", {}).get("value")
        net_gamma_regime = ms_data.get("net_gamma", {}).get("regime") or gf_data.get("regime")
        
        gamma_flip_strike = ms_data.get("gamma_flip", {}).get("strike") or ivr_data.get("gamma_flip_level") or gf_data.get("flip_strike")
        
        distance_pct = ms_data.get("gamma_flip", {}).get("distance_pct") or gf_data.get("distance_pct")
        if distance_pct is None and spot_val > 0 and gamma_flip_strike is not None:
            distance_pct = ((spot_val - gamma_flip_strike) / spot_val) * 100.0

        distance_usd = gf_data.get("distance_usd")
        if distance_usd is None and spot_val > 0 and gamma_flip_strike is not None:
            distance_usd = spot_val - gamma_flip_strike

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
        pc_tag = ms_data.get("pc", {}).get("tag", "balanced")
        call_wall = ms_data.get("call_wall", {})
        put_wall = ms_data.get("put_wall", {})

        skew_items = skew_data.get("items", [])
        near_rr_25d = None
        if skew_items:
            valid_rr = [item.get("rr_25d") for item in skew_items if item.get("rr_25d") is not None]
            if valid_rr:
                near_rr_25d = sum(valid_rr[:5]) / len(valid_rr[:5])

        if near_rr_25d is not None:
            if near_rr_25d < -6.0:
                skew_bias = "panic_put_skew"
            elif near_rr_25d < -3.0 or (pc_ratio is not None and pc_ratio > 0.8):
                skew_bias = "heavy_put_skew"
            elif near_rr_25d > 3.0:
                skew_bias = "heavy_call_skew"
            else:
                skew_bias = "balanced"
        else:
            skew_bias = pc_tag

        # --- 4) Term Structure / Expiry View (Contango vs Backwardation) ---
        term_spread = ms_data.get("term", {}).get("spread_vp")
        if term_spread is None and atm_iv_30d is not None and atm_iv_7d is not None:
            term_spread = atm_iv_30d - atm_iv_7d

        term_tag = ms_data.get("term", {}).get("tag")
        if not term_tag:
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
        vrp_display = f"{vrp_val:.2f}" if vrp_val is not None else "N/A"
        if vol_regime in ["rich", "elevated"] or (vrp_val is not None and vrp_val >= 2.5):
            rule_vol_pass = True
            reasons.append(f"[PASS] Volatility edge is {vol_regime.upper()} (VRP: {vrp_display} vol pts, IV Rank: {iv_rank if iv_rank is not None else 'N/A'}).")
        else:
            reasons.append(f"[FAIL] Volatility edge is {vol_regime.upper()} (VRP: {vrp_display} vol pts). IV is cheap or not rich enough for selling vol.")

        # Rule 2: GEX Regime
        if gex_regime == "positive":
            rule_gex_pass = True
            reasons.append(f"[PASS] Net Gamma regime is POSITIVE ({net_gamma_regime}). Dealers dampening volatility.")
        elif gex_regime == "fragile":
            reasons.append(f"[WARN] Net Gamma is positive but Spot is below Gamma Flip level. Structurally fragile.")
        else:
            reasons.append(f"[FAIL] Net Gamma regime is NEGATIVE / BALANCED ({net_gamma_regime}). High trend risk.")

        # Rule 3: Spot vs Gamma Flip
        dist_display = f"+{distance_pct:.2f}%" if distance_pct is not None else "N/A"
        flip_display = f"{gamma_flip_strike:,.2f}" if gamma_flip_strike is not None else "N/A"
        if spot_above_flip and distance_pct is not None and distance_pct > 0.1:
            rule_spot_pass = True
            reasons.append(f"[PASS] Spot price ({spot_val:,.2f}) is above Gamma Flip ({flip_display}) by {dist_display}.")
        else:
            reasons.append(f"[FAIL] Spot price ({spot_val:,.2f}) is AT or BELOW Gamma Flip ({flip_display}).")

        # Rule 4: Term Structure (VIX / IV Contango vs Backwardation)
        spread_display = f"{term_spread:+.2f}" if term_spread is not None else "N/A"
        if term_tag == "contango" or (term_spread is not None and term_spread >= 0):
            rule_term_pass = True
            reasons.append(f"[PASS] Term Structure is in CONTANGO (30D-7D spread: {spread_display} vol pts). Normal risk premium environment friendly to short vol.")
        elif term_tag == "mild_backwardation" or (term_spread is not None and term_spread >= -3.0):
            rule_term_pass = True  # Allowed with caution
            reasons.append(f"[WARN] Term Structure in MILD BACKWARDATION (30D-7D spread: {spread_display} vol pts). VRP inversion risk; monitor or reduce size.")
        else:
            rule_term_pass = False
            reasons.append(f"[FAIL] Term Structure in DEEP BACKWARDATION (30D-7D spread: {spread_display} vol pts). Volatility stress / VRP inversion risk. Avoid short vol.")

        # Rule 5: Skew / Tail Risk Assessment
        rr_display = f"{near_rr_25d:.2f}" if near_rr_25d is not None else "N/A"
        if skew_bias == "balanced":
            rule_skew_pass = True
            reasons.append(f"[PASS] Skew is BALANCED (25D Risk Reversal: {rr_display}). Standard symmetric wings allowed.")
        elif skew_bias == "heavy_put_skew":
            rule_skew_pass = True  # Pass with strike adjustment
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
            if vol_regime == "rich":
                base_conf += 10
            if term_tag == "contango":
                base_conf += 10
            if distance_pct and distance_pct > 0.5:
                base_conf += 5
            confidence = min(base_conf, 95)
        elif long_vol_allowed:
            rec_strategy = "LONG_VOL_STRADDLE"
            confidence = 65
        else:
            rec_strategy = "HOLD_NEUTRAL"
            confidence = 50

        # Suggested Strikes & Wing Sizing
        rec_short_put = None
        rec_short_call = None
        asymmetric_put_wing = False
        wing_note = "Standard symmetric wings"

        if spot_val > 0 and gamma_flip_strike:
            if skew_bias == "heavy_put_skew":
                asymmetric_put_wing = True
                # Place short put wider (10% below spot or 96% of gamma flip)
                rec_short_put = min(gamma_flip_strike * 0.96, spot_val * 0.90)
                wing_note = "Asymmetric Put Wing: Short put moved 1.5x wider OTM due to heavy put skew"
            else:
                # Standard short put placed safely below gamma flip or 7% below spot
                rec_short_put = min(gamma_flip_strike * 0.98, spot_val * 0.93)

            # Short call placed near call wall or 7% above spot
            call_w_strike = call_wall.get("strike")
            if call_w_strike and call_w_strike > spot_val:
                rec_short_call = call_w_strike
            else:
                rec_short_call = spot_val * 1.07

        return {
            "metadata": {
                "asset": asset_upper,
                "timestamp": timestamp_iso,
                "source_url": f"{self.base_url}/{asset.lower()}/dashboard"
            },
            "spot_price": {
                "value": spot_val,
            },
            "volatility_edge": {
                "iv_7d": atm_iv_7d,
                "iv_30d": atm_iv_30d,
                "rv": rv_val,
                "vrp": vrp_val,
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
                "gex_regime": gex_regime
            },
            "skew_and_distribution": {
                "put_call_ratio": pc_ratio,
                "pc_tag": pc_tag,
                "call_wall": call_wall,
                "put_wall": put_wall,
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
                    "short_put": round(rec_short_put, 2) if rec_short_put else None,
                    "short_call": round(rec_short_call, 2) if rec_short_call else None,
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
    
    print(f"\n{c.BOLD} SPOT PRICE:{c.ENDC} ${spot:,.2f}")

    # Panel 1: Vol Edge & Term Structure
    vrp_str = f"{vol['vrp']:.2f} vol pts" if vol['vrp'] is not None else "N/A"
    iv_rank_str = f"{vol['iv_rank']:.1f}" if vol['iv_rank'] is not None else "N/A"
    vol_pass = vol['vol_regime'] in ['rich', 'elevated']
    term_sprd = term['term_spread_30d_7d']
    term_sprd_str = f"{term_sprd:+.2f} vol pts" if term_sprd is not None else "N/A"
    term_pass = term['term_tag'] == 'contango'

    print(f"\n{c.BOLD}┌─ 1. VOLATILITY EDGE & TERM STRUCTURE PANEL ───────────────────────────────┐{c.ENDC}")
    print(f"│ 30D IV: {vol['iv_30d'] or 0:.2f}%  |  7D IV: {vol['iv_7d'] or 0:.2f}%  |  RV: {vol['rv'] or 0:.2f}%")
    print(f"│ Vol Risk Premium (VRP): {vrp_str:<12} | IV Rank: {iv_rank_str:<6}")
    print(f"│ Vol Regime: {tag_color(vol['vol_regime'].upper(), vol_pass)}  |  Term Structure: {tag_color(term['term_tag'].upper() + ' (' + term_sprd_str + ')', term_pass)}")
    print(f"└───────────────────────────────────────────────────────────────────────────┘")

    # Panel 2: GEX & Gamma Flip
    flip_lvl = gex['gamma_flip_level']
    flip_str = f"${flip_lvl:,.2f}" if flip_lvl else "N/A"
    dist_str = f"{gex['distance_pct']:+.2f}%" if gex['distance_pct'] is not None else "N/A"
    gex_pass = gex['gex_regime'] == 'positive'
    print(f"\n{c.BOLD}┌─ 2. GAMMA EXPOSURE (GEX) & GAMMA FLIP PANEL ─────────────────────────────┐{c.ENDC}")
    print(f"│ Net Gamma: {gex['net_gamma_value'] or 0:,.0f} ({gex['net_gamma_regime']})")
    print(f"│ Gamma Flip Level: {flip_str:<14} | Spot vs Flip: {dist_str}")
    print(f"│ GEX Regime: {tag_color(gex['gex_regime'].upper(), gex_pass)}")
    print(f"└───────────────────────────────────────────────────────────────────────────┘")

    # Panel 3: Skew & Moneyness
    pc_str = f"{skew['put_call_ratio']:.2f}" if skew['put_call_ratio'] is not None else "N/A"
    call_w = skew['call_wall'].get('strike')
    put_w = skew['put_wall'].get('strike')
    rr_str = f"{skew['avg_25d_rr']:.2f}" if skew['avg_25d_rr'] is not None else "N/A"
    print(f"\n{c.BOLD}┌─ 3. SKEW, MONEYNESS & TAIL RISK PANEL ───────────────────────────────────┐{c.ENDC}")
    print(f"│ Put/Call Ratio: {pc_str:<8} | 25-Delta Risk Reversal: {rr_str}")
    print(f"│ Call Wall Strike: ${call_w or 0:,.2f}  |  Put Wall Strike: ${put_w or 0:,.2f}")
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
    
    if sig['suggested_strikes']['short_put']:
        print(f"│")
        print(f"│ Recommended Strike & Wing Structure:")
        print(f"│   Short Put Strike  : ${sig['suggested_strikes']['short_put']:,.2f} (Below Gamma Flip ${flip_lvl:,.2f})")
        print(f"│   Short Call Strike : ${sig['suggested_strikes']['short_call']:,.2f} (Near Call Wall / Spot Upper)")
        print(f"│   Wing Note         : {sig['suggested_strikes']['wing_note']}")
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
