#!/usr/bin/env python3
"""
Derivatives Monkey Market Scanner & Multi-Market Ranking CLI
Evaluates and ranks crypto markets (BTC, ETH, HYPE, XRP, XAUT, SOL, ZEC, ADA, CC, etc.)
for deploying Short-Volatility Iron Condors with Delta-Hedged Perp Positions.
"""

import sys
import os
import json
import time
import argparse
import concurrent.futures
from datetime import datetime, timezone

# Ensure local module import
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from derivatives_monkey_extractor import DerivativesMonkeyExtractor, Colors

DEFAULT_MARKETS = ["ETH", "BTC", "HYPE", "XAUT", "SOL", "XRP", "ZEC", "ADA", "CC"]

class MarketRanker:
    def __init__(self, markets=None, timeout=5, workers=8):
        self.markets = [m.strip().upper() for m in (markets or DEFAULT_MARKETS)]
        self.extractor = DerivativesMonkeyExtractor(timeout=timeout)
        self.workers = min(len(self.markets), workers) if self.markets else 1

    def _fetch_single(self, symbol: str) -> dict:
        try:
            sig = self.extractor.get_live_signals(symbol)
            return {"symbol": symbol, "data": sig, "error": None}
        except Exception as e:
            return {"symbol": symbol, "data": None, "error": str(e)}

    def scan_and_rank(self) -> list:
        # Parallel fetch for low latency
        raw_results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(self._fetch_single, m) for m in self.markets]
            for f in concurrent.futures.as_completed(futures):
                raw_results.append(f.result())

        market_data = []
        for res in raw_results:
            sym = res["symbol"]
            sig = res["data"]
            
            if not sig or sig.get("spot_price", {}).get("value", 0) == 0 or res["error"]:
                market_data.append({
                    "symbol": sym,
                    "status": "UNAVAILABLE / NOT LISTED",
                    "score": 0,
                    "spot": 0.0,
                    "iv30": 0.0,
                    "rv": 0.0,
                    "vrp": 0.0,
                    "iv_rank": 0.0,
                    "vol_reg": "N/A",
                    "net_gamma_reg": "N/A",
                    "gex_reg": "N/A",
                    "flip": 0.0,
                    "dist_pct": 0.0,
                    "term_tag": "N/A",
                    "term_spread": 0.0,
                    "skew_bias": "N/A",
                    "rr25": 0.0,
                    "allowed": False,
                    "rec": "NOT_LISTED",
                    "confidence": 0,
                    "reasons": ["Market not currently listed on options exchange dashboard."],
                    "suggested_strikes": {}
                })
                continue

            spot = sig["spot_price"]["value"]
            vol = sig["volatility_edge"]
            gex = sig["gamma_exposure"]
            skew = sig["skew_and_distribution"]
            term = sig["term_structure"]
            signal = sig["signal"]

            vrp = vol["vrp"] if vol["vrp"] is not None else 0.0
            iv30 = vol["iv_30d"] if vol["iv_30d"] is not None else 0.0
            rv = vol["rv"] if vol["rv"] is not None else 0.0
            iv_rank = vol["iv_rank"] if vol["iv_rank"] is not None else 0.0
            vol_reg = vol["vol_regime"]

            net_gamma_reg = gex["net_gamma_regime"]
            gex_reg = gex["gex_regime"]
            flip = gex["gamma_flip_level"] if gex["gamma_flip_level"] is not None else 0.0
            dist_pct = gex["distance_pct"] if gex["distance_pct"] is not None else 0.0
            spot_above_flip = gex["spot_above_flip"]

            term_tag = term["term_tag"]
            term_spread = term["term_spread_30d_7d"] if term["term_spread_30d_7d"] is not None else 0.0

            skew_bias = skew["skew_bias"]
            rr25 = skew["avg_25d_rr"] if skew["avg_25d_rr"] is not None else 0.0

            allowed = signal["short_vol_iron_condor_allowed"]
            rec = signal["recommended_strategy"]
            confidence = signal["confidence_score"]
            reasons = signal["reasons"]
            suggested_strikes = signal["suggested_strikes"]

            # --- Quantitative Multi-Factor Ranking (0 - 100) ---
            # 1. Vol Edge / VRP Score (Max 30 pts)
            score_vrp = 0
            if vrp >= 4.0: score_vrp = 30
            elif vrp >= 2.0: score_vrp = 22
            elif vrp >= 0.0: score_vrp = 15
            elif vrp >= -2.0: score_vrp = 8
            else: score_vrp = 0

            # 2. GEX / Dealer Gamma Regime (Max 35 pts) - Prevents hedging churn
            score_gex = 0
            if net_gamma_reg in ["long_gamma", "positive"] and spot_above_flip:
                score_gex = 30
                if dist_pct > 0.5: score_gex += 5  # Bonus clearance buffer
            elif net_gamma_reg in ["long_gamma", "positive"] and not spot_above_flip:
                score_gex = 15  # Long gamma but hovering near flip
            elif net_gamma_reg == "balanced":
                score_gex = 8
            else:
                score_gex = 0   # Short gamma = high whipsaw risk for delta hedging

            # 3. Term Structure (Max 20 pts) - Theta capture slope
            score_term = 0
            if term_tag == "contango" and term_spread > 4.0: score_term = 20
            elif term_tag == "contango" and term_spread >= 0.0: score_term = 15
            elif term_tag == "mild_backwardation": score_term = 5
            else: score_term = 0

            # 4. Skew & Tail Risk (Max 15 pts) - Wing safety
            score_skew = 0
            if skew_bias == "balanced": score_skew = 15
            elif skew_bias == "heavy_call_skew": score_skew = 12
            elif skew_bias == "heavy_put_skew": score_skew = 8
            else: score_skew = 0

            total_score = min(score_vrp + score_gex + score_term + score_skew, 100)

            market_data.append({
                "symbol": sym,
                "status": "ACTIVE",
                "score": total_score,
                "score_breakdown": {
                    "vrp_score": score_vrp,
                    "gex_score": score_gex,
                    "term_score": score_term,
                    "skew_score": score_skew
                },
                "spot": spot,
                "iv30": iv30,
                "rv": rv,
                "vrp": vrp,
                "iv_rank": iv_rank,
                "vol_reg": vol_reg,
                "net_gamma_reg": net_gamma_reg,
                "gex_reg": gex_reg,
                "flip": flip,
                "dist_pct": dist_pct,
                "term_tag": term_tag,
                "term_spread": term_spread,
                "skew_bias": skew_bias,
                "rr25": rr25,
                "allowed": allowed,
                "rec": rec,
                "confidence": confidence,
                "reasons": reasons,
                "suggested_strikes": suggested_strikes
            })

        # Sort descending by score
        market_data.sort(key=lambda x: x["score"], reverse=True)
        return market_data


def render_ranking_cli(ranked_markets: list):
    c = Colors
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    print("\n" + "="*110)
    print(f"{c.BOLD}{c.HEADER}  DERIVATIVES MONKEY MULTI-MARKET SCANNER — SHORT-VOL IRON CONDOR RANKING{c.ENDC}")
    print(f"{c.DIM}  Scan Time: {ts} | Scoring: Vol Edge (30%) + GEX/Dealer Hedge (35%) + Term Structure (20%) + Skew (15%){c.ENDC}")
    print("="*110 + "\n")

    # Table Header
    header = f"{'Rank':<5} {'Market':<7} {'Score':<8} {'Spot Price':<12} {'30D IV':<9} {'RV':<8} {'VRP (pts)':<11} {'GEX Regime':<15} {'Dist Flip':<11} {'Term Structure':<16} {'Skew':<13} {'Signal':<12}"
    print(f"{c.BOLD}{header}{c.ENDC}")
    print("-" * 135)

    for idx, m in enumerate(ranked_markets, 1):
        if m["status"] != "ACTIVE":
            print(f"{idx:<5} {m['symbol']:<7} {'0/100':<8} {'—':<12} {'—':<9} {'—':<8} {'—':<11} {c.DIM}{'NOT LISTED':<15}{c.ENDC} {'—':<11} {'—':<16} {'—':<13} {c.DIM}UNAVAILABLE{c.ENDC}")
            continue

        score_str = f"{m['score']}/100"
        score_colored = f"{c.OKGREEN}{score_str:<8}{c.ENDC}" if m['score'] >= 80 else (f"{c.WARNING}{score_str:<8}{c.ENDC}" if m['score'] >= 60 else f"{c.FAIL}{score_str:<8}{c.ENDC}")
        
        spot_str = f"${m['spot']:,.2f}"
        iv_str = f"{m['iv30']:.1f}%"
        rv_str = f"{m['rv']:.1f}%"
        vrp_str = f"{m['vrp']:+.2f}"
        
        # GEX
        gex_raw = f"{m['gex_reg'].upper()}"
        gex_colored = f"{c.OKGREEN}{gex_raw:<15}{c.ENDC}" if m['gex_reg'] == 'positive' else (f"{c.WARNING}{gex_raw:<15}{c.ENDC}" if m['gex_reg'] == 'fragile' else f"{c.FAIL}{gex_raw:<15}{c.ENDC}")
        
        dist_str = f"{m['dist_pct']:+.2f}%"
        dist_colored = f"{c.OKGREEN}{dist_str:<11}{c.ENDC}" if m['dist_pct'] > 0 else f"{c.FAIL}{dist_str:<11}{c.ENDC}"

        # Term Structure
        term_str = f"{m['term_tag'].upper()[:8]} ({m['term_spread']:+.1f})"
        term_colored = f"{c.OKGREEN}{term_str:<16}{c.ENDC}" if m['term_tag'] == 'contango' else f"{c.FAIL}{term_str:<16}{c.ENDC}"

        # Skew
        skew_str = m['skew_bias'].upper()[:12]

        # Allowed / Rec
        if m['allowed']:
            rec_str = f"{c.BOLD}{c.OKGREEN}ALLOWED{c.ENDC}"
        elif m['rec'] == 'HOLD_NEUTRAL':
            rec_str = f"{c.WARNING}WATCHLIST{c.ENDC}"
        else:
            rec_str = f"{c.FAIL}AVOID{c.ENDC}"

        row = f"{idx:<5} {c.BOLD}{m['symbol']:<7}{c.ENDC} {score_colored} {spot_str:<12} {iv_str:<9} {rv_str:<8} {vrp_str:<11} {gex_colored} {dist_colored} {term_colored} {skew_str:<13} {rec_str:<12}"
        print(row)

    print("-" * 135)

    # --- Actionable Execution Summary ---
    active_allowed = [m for m in ranked_markets if m['allowed'] and m['status'] == 'ACTIVE']
    top_picks = [m for m in ranked_markets if m['score'] >= 75 and m['status'] == 'ACTIVE']
    watchlist = [m for m in ranked_markets if not m['allowed'] and 50 <= m['score'] < 80 and m['status'] == 'ACTIVE']

    print(f"\n{c.BOLD}┌─ 🎯 ACTIONABLE STRATEGY RECOMMENDATIONS ─────────────────────────────────────────────────────────┐{c.ENDC}")
    if active_allowed:
        for best in active_allowed:
            strikes = best.get("suggested_strikes", {})
            sp_put = f"${strikes.get('short_put', 0):,.2f}" if strikes.get('short_put') else "N/A"
            sp_call = f"${strikes.get('short_call', 0):,.2f}" if strikes.get('short_call') else "N/A"
            wing_note = strikes.get('wing_note', 'Standard symmetric wings')

            print(f"│ {c.BOLD}{c.OKGREEN}★ ACTIVE DEPLOYMENT READY: {best['symbol']} (Score: {best['score']}/100 | Confidence: {best['confidence']}%){c.ENDC}")
            print(f"│   • Volatility Edge : 30D IV {best['iv30']:.1f}% vs RV {best['rv']:.1f}% | VRP: {best['vrp']:+.2f} vol pts ({best['vol_reg'].upper()})")
            print(f"│   • GEX & Hedging   : Positive / Dealer Long Gamma | Spot vs Flip: {best['dist_pct']:+.2f}%")
            print(f"│   • Term Structure  : {best['term_tag'].upper()} (+{best['term_spread']:.2f} vol pts theta slope)")
            print(f"│   • Execution Setup : Short Put: {sp_put} (Below Flip ${best['flip']:,.2f}) | Short Call: {sp_call}")
            print(f"│   • Delta Hedge Rule: Pair with {best['symbol']}-PERP (Maintain delta band ±0.15 with perpetual futures)")
            print(f"│   • Wing Structure  : {wing_note}")
    elif top_picks:
        best = top_picks[0]
        strikes = best.get("suggested_strikes", {})
        sp_put = f"${strikes.get('short_put', 0):,.2f}" if strikes.get('short_put') else "N/A"
        sp_call = f"${strikes.get('short_call', 0):,.2f}" if strikes.get('short_call') else "N/A"
        wing_note = strikes.get('wing_note', 'Standard symmetric wings')

        print(f"│ {c.BOLD}{c.OKGREEN}★ TOP CANDIDATE (PENDING CLEARANCE): {best['symbol']} (Score: {best['score']}/100){c.ENDC}")
        print(f"│   • Volatility Edge : Rich IV ({best['iv30']:.1f}% vs RV {best['rv']:.1f}%) | VRP: {best['vrp']:+.2f} vol pts")
        print(f"│   • GEX Regime      : Positive / Dealer Long Gamma | Spot vs Flip: {best['dist_pct']:+.2f}%")
        print(f"│   • Term Structure  : {best['term_tag'].upper()} (+{best['term_spread']:.2f} vol pts theta slope)")
        print(f"│   • Execution Setup : Short Put: {sp_put} (Below Flip ${best['flip']:,.2f}) | Short Call: {sp_call}")
        print(f"│   • Delta Hedge Rule: Pair with {best['symbol']}-PERP (Maintain delta band ±0.15 with perpetual futures)")
    else:
        print(f"│ {c.WARNING}No markets currently meet full active deployment threshold.{c.ENDC}")

    if watchlist:
        print(f"│")
        print(f"│ {c.BOLD}⚡ WATCHLIST / CONDITIONAL TRIGGERS:{c.ENDC}")
        for w in watchlist:
            if w['dist_pct'] <= 0:
                trigger_note = f"Spot (${w['spot']:,.2f}) needs to cross above Gamma Flip (${w['flip']:,.2f})"
            elif w['net_gamma_reg'] not in ['long_gamma', 'positive']:
                trigger_note = f"Dealer GEX needs to flip positive (currently {w['net_gamma_reg']})"
            else:
                trigger_note = f"Volatility edge needs to expand (currently VRP {w['vrp']:+.2f} vol pts)"
            print(f"│   • {c.BOLD}{w['symbol']:<5}{c.ENDC} (Score: {w['score']:>2}/100) — {trigger_note}")

    print(f"└──────────────────────────────────────────────────────────────────────────────────────────────────┘\n")


def main():
    parser = argparse.ArgumentParser(description="Multi-Market Ranking for Short-Vol Iron Condors with Delta Hedging")
    parser.add_argument("-m", "--markets", type=str, default=",".join(DEFAULT_MARKETS), help="Comma-separated list of symbols (default: ETH,BTC,HYPE,XAUT,SOL,XRP,ZEC,ADA,CC)")
    parser.add_argument("-j", "--json", action="store_true", help="Output raw JSON ranking")
    parser.add_argument("-o", "--output", type=str, default=None, help="Save ranking JSON to specified file path")
    parser.add_argument("-w", "--watch", action="store_true", help="Continuous monitoring watch mode")
    parser.add_argument("-i", "--interval", type=int, default=15, help="Refresh interval in seconds for watch mode")
    parser.add_argument("-t", "--timeout", type=int, default=5, help="HTTP request timeout in seconds")

    args = parser.parse_args()
    market_list = [m.strip() for m in args.markets.split(",") if m.strip()]
    ranker = MarketRanker(markets=market_list, timeout=args.timeout)

    while True:
        try:
            ranked = ranker.scan_and_rank()

            if args.output:
                with open(args.output, "w") as f:
                    json.dump(ranked, f, indent=2)

            if args.json:
                print(json.dumps(ranked, indent=2))
            else:
                if args.watch:
                    os.system("cls" if os.name == "nt" else "clear")
                render_ranking_cli(ranked)

        except Exception as e:
            print(f"{Colors.FAIL}Error running ranking: {e}{Colors.ENDC}", file=sys.stderr)

        if not args.watch:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
