#!/usr/bin/env python3
"""
Derivatives Monkey Market Scanner & Multi-Market Ranking CLI
Evaluates and ranks crypto markets (BTC, ETH, HYPE, XRP, XAUT, SOL, ZEC, ADA, CC, etc.)
for deploying Short-Volatility Iron Condors with Delta-Hedged Perp Positions.
"""

import argparse
import concurrent.futures
import json
import os
import sys
import time
from datetime import datetime, timezone

# Ensure local module import
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from derivatives_monkey_extractor import Colors, DerivativesMonkeyExtractor

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

    def scan_and_rank(self, sort_by: str = "readiness") -> list:
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

            has_options = (
                sig and sig.get("volatility_edge", {}).get("iv_30d") is not None
            )
            if (
                not sig
                or not has_options
                or sig.get("spot_price", {}).get("value", 0) == 0
                or res["error"]
            ):
                market_data.append(
                    {
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
                        "tier_order": 0,
                        "failed_gates": ["Not Listed"],
                        "reasons": [
                            "Market not currently listed on options exchange dashboard."
                        ],
                        "suggested_strikes": {},
                    }
                )
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
            flip = (
                gex["gamma_flip_level"] if gex["gamma_flip_level"] is not None else 0.0
            )
            dist_pct = gex["distance_pct"] if gex["distance_pct"] is not None else 0.0
            spot_above_flip = gex["spot_above_flip"]

            term_tag = term["term_tag"]
            term_spread = (
                term["term_spread_30d_7d"]
                if term["term_spread_30d_7d"] is not None
                else 0.0
            )

            skew_bias = skew["skew_bias"]
            rr25 = skew["avg_25d_rr"] if skew["avg_25d_rr"] is not None else 0.0

            allowed = signal["short_vol_iron_condor_allowed"]
            rec = signal["recommended_strategy"]
            confidence = signal["confidence_score"]
            reasons = signal["reasons"]
            suggested_strikes = signal["suggested_strikes"]
            optimal_dte = signal.get("optimal_dte", {})

            # Identify failed gates from rule_evaluation
            rule_eval = signal.get("rule_evaluation", {})
            failed_gates = []
            if not rule_eval.get("volatility_rule_passed", True):
                failed_gates.append("Vol")
            if not rule_eval.get("gamma_regime_rule_passed", True):
                failed_gates.append("GEX")
            if not rule_eval.get("spot_vs_flip_rule_passed", True):
                failed_gates.append("Flip")
            if not rule_eval.get("term_structure_rule_passed", True):
                failed_gates.append("Term")
            if not rule_eval.get("skew_rule_passed", True):
                failed_gates.append("Skew")

            tier_order = 3 if allowed else (2 if rec == "HOLD_NEUTRAL" else 1)

            # --- Quantitative Multi-Factor Ranking (0 - 100) ---
            # 1. Vol Edge / VRP Score (Max 30 pts)
            score_vrp = 0
            if vrp >= 4.0:
                score_vrp = 30
            elif vrp >= 2.0:
                score_vrp = 22
            elif vrp >= 0.0:
                score_vrp = 15
            elif vrp >= -2.0:
                score_vrp = 8
            else:
                score_vrp = 0

            # 2. GEX / Dealer Gamma Regime (Max 35 pts) - Prevents hedging churn
            score_gex = 0
            if net_gamma_reg in ["long_gamma", "positive"] and spot_above_flip:
                score_gex = 30
                if dist_pct > 0.5:
                    score_gex += 5  # Bonus clearance buffer
            elif net_gamma_reg in ["long_gamma", "positive"] and not spot_above_flip:
                score_gex = 15  # Long gamma but hovering near flip
            elif net_gamma_reg == "balanced":
                score_gex = 8
            else:
                score_gex = 0  # Short gamma = high whipsaw risk for delta hedging

            # 3. Term Structure (Max 20 pts) - Theta capture slope
            score_term = 0
            if term_tag == "contango" and term_spread > 4.0:
                score_term = 20
            elif term_tag == "contango" and term_spread >= 0.0:
                score_term = 15
            elif term_tag == "mild_backwardation":
                score_term = 5
            else:
                score_term = 0

            # 4. Skew & Tail Risk (Max 15 pts) - Wing safety
            score_skew = 0
            if skew_bias == "balanced":
                score_skew = 15
            elif skew_bias == "heavy_call_skew":
                score_skew = 12
            elif skew_bias == "heavy_put_skew":
                score_skew = 8
            else:
                score_skew = 0

            total_score = min(score_vrp + score_gex + score_term + score_skew, 100)

            market_data.append(
                {
                    "symbol": sym,
                    "status": "ACTIVE",
                    "score": total_score,
                    "tier_order": tier_order,
                    "failed_gates": failed_gates,
                    "score_breakdown": {
                        "vrp_score": score_vrp,
                        "gex_score": score_gex,
                        "term_score": score_term,
                        "skew_score": score_skew,
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
                    "suggested_strikes": suggested_strikes,
                    "optimal_dte": optimal_dte,
                }
            )

        # Sort based on strategy preference
        if sort_by == "readiness":
            market_data.sort(
                key=lambda x: (
                    x.get("tier_order", 0),
                    x["score"],
                    x.get("confidence", 0),
                ),
                reverse=True,
            )
        elif sort_by == "confidence":
            market_data.sort(
                key=lambda x: (x.get("confidence", 0), x["score"]),
                reverse=True,
            )
        else:  # "score"
            market_data.sort(key=lambda x: x["score"], reverse=True)

        return market_data


def render_ranking_cli(ranked_markets: list, sort_by: str = "readiness"):
    c = Colors
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    sort_labels = {
        "readiness": "READINESS (Actionable First)",
        "score": "SCORE (Raw Multi-Factor)",
        "confidence": "CONFIDENCE (Strategy Conviction)",
    }
    sort_disp = sort_labels.get(sort_by, sort_by.upper())

    print("\n" + "=" * 142)
    print(
        f"{c.BOLD}{c.HEADER}  DERIVATIVES MONKEY MULTI-MARKET SCANNER — SHORT-VOL IRON CONDOR RANKING{c.ENDC}"
    )
    print(
        f"{c.DIM}  Scan Time: {ts} | Sort: {sort_disp} | Scoring: Vol Edge (30%) + GEX/Dealer Hedge (35%) + Term Structure (20%) + Skew (15%){c.ENDC}"
    )
    print("=" * 142 + "\n")

    # Table Header
    header = f"{'Rank':<5} {'Market':<7} {'Score':<8} {'Conf':<6} {'Spot Price':<12} {'30D IV':<9} {'RV':<8} {'VRP (pts)':<11} {'GEX Regime':<15} {'Dist Flip':<11} {'Term Structure':<16} {'Skew':<13} {'Signal':<16}"
    print(f"{c.BOLD}{header}{c.ENDC}")
    print("-" * 142)

    for idx, m in enumerate(ranked_markets, 1):
        if m["status"] != "ACTIVE":
            print(
                f"{idx:<5} {m['symbol']:<7} {'0/100':<8} {'—':<6} {'—':<12} {'—':<9} {'—':<8} {'—':<11} {c.DIM}{'NOT LISTED':<15}{c.ENDC} {'—':<11} {'—':<16} {'—':<13} {c.DIM}UNAVAILABLE{c.ENDC}"
            )
            continue

        score_str = f"{m['score']}/100"
        score_colored = (
            f"{c.OKGREEN}{score_str:<8}{c.ENDC}"
            if m["score"] >= 80
            else (
                f"{c.WARNING}{score_str:<8}{c.ENDC}"
                if m["score"] >= 60
                else f"{c.FAIL}{score_str:<8}{c.ENDC}"
            )
        )

        conf_val = m.get("confidence", 0)
        conf_str = f"{conf_val}%"
        if conf_val >= 80:
            conf_colored = f"{c.BOLD}{c.OKGREEN}{conf_str:<6}{c.ENDC}"
        elif conf_val >= 65:
            conf_colored = f"{c.OKGREEN}{conf_str:<6}{c.ENDC}"
        elif conf_val >= 50:
            conf_colored = f"{c.WARNING}{conf_str:<6}{c.ENDC}"
        else:
            conf_colored = f"{c.FAIL}{conf_str:<6}{c.ENDC}"

        spot_str = f"${m['spot']:,.2f}"
        iv_str = f"{m['iv30']:.1f}%"
        rv_str = f"{m['rv']:.1f}%"
        vrp_str = f"{m['vrp']:+.2f}"

        # GEX
        gex_raw = f"{m['gex_reg'].upper()}"
        gex_colored = (
            f"{c.OKGREEN}{gex_raw:<15}{c.ENDC}"
            if m["gex_reg"] == "positive"
            else (
                f"{c.WARNING}{gex_raw:<15}{c.ENDC}"
                if m["gex_reg"] == "fragile"
                else f"{c.FAIL}{gex_raw:<15}{c.ENDC}"
            )
        )

        dist_str = f"{m['dist_pct']:+.2f}%"
        dist_colored = (
            f"{c.OKGREEN}{dist_str:<11}{c.ENDC}"
            if m["dist_pct"] > 0
            else f"{c.FAIL}{dist_str:<11}{c.ENDC}"
        )

        # Term Structure
        term_str = f"{m['term_tag'].upper()[:8]} ({m['term_spread']:+.1f})"
        term_colored = (
            f"{c.OKGREEN}{term_str:<16}{c.ENDC}"
            if m["term_tag"] == "contango"
            else f"{c.FAIL}{term_str:<16}{c.ENDC}"
        )

        # Skew
        skew_str = m["skew_bias"].upper()[:12]

        # Allowed / Rec with failed gates annotation
        failed = m.get("failed_gates", [])
        fail_desc = f" ({','.join(failed)})" if failed else ""
        if m["allowed"]:
            rec_raw = "ALLOWED"
            rec_colored = f"{c.BOLD}{c.OKGREEN}{rec_raw:<16}{c.ENDC}"
        elif m["rec"] == "HOLD_NEUTRAL":
            rec_raw = f"WATCH{fail_desc}"
            rec_colored = f"{c.WARNING}{rec_raw:<16}{c.ENDC}"
        else:
            rec_raw = f"AVOID{fail_desc}"
            rec_colored = f"{c.FAIL}{rec_raw:<16}{c.ENDC}"

        row = f"{idx:<5} {c.BOLD}{m['symbol']:<7}{c.ENDC} {score_colored} {conf_colored} {spot_str:<12} {iv_str:<9} {rv_str:<8} {vrp_str:<11} {gex_colored} {dist_colored} {term_colored} {skew_str:<13} {rec_colored}"
        print(row)

    print("-" * 142)

    # --- Actionable Execution Summary ---
    active_allowed = [
        m for m in ranked_markets if m["allowed"] and m["status"] == "ACTIVE"
    ]
    top_picks = [
        m for m in ranked_markets if m["score"] >= 75 and m["status"] == "ACTIVE"
    ]
    watchlist = [
        m
        for m in ranked_markets
        if not m["allowed"] and 50 <= m["score"] < 80 and m["status"] == "ACTIVE"
    ]

    print(
        f"\n{c.BOLD}┌─ 🎯 ACTIONABLE STRATEGY RECOMMENDATIONS ─────────────────────────────────────────────────────────┐{c.ENDC}"
    )
    if active_allowed:
        for best in active_allowed:
            strikes = best.get("suggested_strikes", {})
            sp_put = (
                f"${strikes.get('short_put', 0):,.2f}"
                if strikes.get("short_put")
                else "N/A"
            )
            sp_call = (
                f"${strikes.get('short_call', 0):,.2f}"
                if strikes.get("short_call")
                else "N/A"
            )
            wing_note = strikes.get("wing_note", "Standard symmetric wings")
            opt_dte = best.get("optimal_dte", {})
            exp_date = strikes.get("target_expiry") or opt_dte.get(
                "target_expiry_date", "N/A"
            )
            target_dte_val = strikes.get("target_dte") or opt_dte.get("target_dte", 14)
            target_dte_disp = (
                f"{target_dte_val:.1f} DTE"
                if isinstance(target_dte_val, float)
                else f"{target_dte_val} DTE"
            )

            print(
                f"│ {c.BOLD}{c.OKGREEN}★ ACTIVE DEPLOYMENT READY: {best['symbol']} (Score: {best['score']}/100 | Confidence: {best['confidence']}%){c.ENDC}"
            )
            print(
                f"│   • Volatility Edge : 30D IV {best['iv30']:.1f}% vs RV {best['rv']:.1f}% | VRP: {best['vrp']:+.2f} vol pts ({best['vol_reg'].upper()})"
            )
            print(
                f"│   • GEX & Hedging   : Positive / Dealer Long Gamma | Spot vs Flip: {best['dist_pct']:+.2f}%"
            )
            print(
                f"│   • Term Structure  : {best['term_tag'].upper()} (+{best['term_spread']:.2f} vol pts theta slope)"
            )
            print(
                f"│   • Optimal Tenor   : {c.OKCYAN}{c.BOLD}{target_dte_disp} (Target Expiry: {exp_date}){c.ENDC} — Single-tenor 4-leg match"
            )
            print(
                f"│   • Execution Setup : Short Put: {sp_put} (Below Flip ${best['flip']:,.2f}) | Short Call: {sp_call}"
            )
            print(
                f"│   • Delta Hedge Rule: Pair with {best['symbol']}-PERP (Maintain delta band ±0.15 with perpetual futures)"
            )
            print(f"│   • Wing Structure  : {wing_note}")
    elif top_picks:
        best = top_picks[0]
        strikes = best.get("suggested_strikes", {})
        sp_put = (
            f"${strikes.get('short_put', 0):,.2f}"
            if strikes.get("short_put")
            else "N/A"
        )
        sp_call = (
            f"${strikes.get('short_call', 0):,.2f}"
            if strikes.get("short_call")
            else "N/A"
        )
        opt_dte = best.get("optimal_dte", {})
        exp_date = strikes.get("target_expiry") or opt_dte.get(
            "target_expiry_date", "N/A"
        )
        target_dte_val = strikes.get("target_dte") or opt_dte.get("target_dte", 14)
        target_dte_disp = (
            f"{target_dte_val:.1f} DTE"
            if isinstance(target_dte_val, float)
            else f"{target_dte_val} DTE"
        )

        print(
            f"│ {c.BOLD}{c.OKGREEN}★ TOP CANDIDATE (PENDING CLEARANCE): {best['symbol']} (Score: {best['score']}/100){c.ENDC}"
        )
        print(
            f"│   • Volatility Edge : Rich IV ({best['iv30']:.1f}% vs RV {best['rv']:.1f}%) | VRP: {best['vrp']:+.2f} vol pts"
        )
        print(
            f"│   • GEX Regime      : Positive / Dealer Long Gamma | Spot vs Flip: {best['dist_pct']:+.2f}%"
        )
        print(
            f"│   • Term Structure  : {best['term_tag'].upper()} (+{best['term_spread']:.2f} vol pts theta slope)"
        )
        print(
            f"│   • Optimal Tenor   : {c.OKCYAN}{c.BOLD}{target_dte_disp} (Target Expiry: {exp_date}){c.ENDC}"
        )
        print(
            f"│   • Execution Setup : Short Put: {sp_put} (Below Flip ${best['flip']:,.2f}) | Short Call: {sp_call}"
        )
        print(
            f"│   • Delta Hedge Rule: Pair with {best['symbol']}-PERP (Maintain delta band ±0.15 with perpetual futures)"
        )
    else:
        print(
            f"│ {c.WARNING}No markets currently meet full active deployment threshold.{c.ENDC}"
        )

    if watchlist:
        print(f"│")
        print(f"│ {c.BOLD}⚡ WATCHLIST / CONDITIONAL TRIGGERS:{c.ENDC}")
        for w in watchlist:
            failed = w.get("failed_gates", [])
            triggers = []
            if "Flip" in failed or w["dist_pct"] <= 0:
                triggers.append(
                    f"Spot (${w['spot']:,.2f}) must clear Gamma Flip (${w['flip']:,.2f})"
                )
            if "GEX" in failed or w["net_gamma_reg"] not in ["long_gamma", "positive"]:
                triggers.append(
                    f"Dealer GEX must flip positive (currently {w['net_gamma_reg']})"
                )
            if "Term" in failed or w["term_tag"] != "contango":
                triggers.append(
                    f"Term structure must normalize to contango (currently {w['term_tag'].upper()} {w['term_spread']:+.1f} pts)"
                )
            if "Vol" in failed:
                triggers.append(
                    f"Volatility edge must expand (currently VRP {w['vrp']:+.2f} vol pts)"
                )
            if "Skew" in failed:
                triggers.append(
                    f"Skew must recover from panic put bias (25D RR: {w['rr25']:.1f})"
                )

            if not triggers:
                triggers.append(
                    f"Volatility edge needs to expand (currently VRP {w['vrp']:+.2f} vol pts)"
                )

            trigger_note = " & ".join(triggers)
            print(
                f"│   • {c.BOLD}{w['symbol']:<5}{c.ENDC} (Score: {w['score']:>2}/100 | Conf: {w['confidence']}%) — {trigger_note}"
            )

    print(
        f"└──────────────────────────────────────────────────────────────────────────────────────────────────┘\n"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Multi-Market Ranking for Short-Vol Iron Condors with Delta Hedging"
    )
    parser.add_argument(
        "-m",
        "--markets",
        type=str,
        default=",".join(DEFAULT_MARKETS),
        help="Comma-separated list of symbols (default: ETH,BTC,HYPE,XAUT,SOL,XRP,ZEC,ADA,CC)",
    )
    parser.add_argument(
        "-s",
        "--sort",
        type=str,
        choices=["readiness", "score", "confidence"],
        default="readiness",
        help="Sort ranking by: readiness (deployment tier + score), score (raw multi-factor), or confidence (default: readiness)",
    )
    parser.add_argument(
        "-j", "--json", action="store_true", help="Output raw JSON ranking"
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default=None,
        help="Save ranking JSON to specified file path",
    )
    parser.add_argument(
        "-w", "--watch", action="store_true", help="Continuous monitoring watch mode"
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=int,
        default=15,
        help="Refresh interval in seconds for watch mode",
    )
    parser.add_argument(
        "-t", "--timeout", type=int, default=5, help="HTTP request timeout in seconds"
    )

    args = parser.parse_args()
    market_list = [m.strip() for m in args.markets.split(",") if m.strip()]
    ranker = MarketRanker(markets=market_list, timeout=args.timeout)

    while True:
        try:
            ranked = ranker.scan_and_rank(sort_by=args.sort)

            if args.output:
                with open(args.output, "w") as f:
                    json.dump(ranked, f, indent=2)

            if args.json:
                print(json.dumps(ranked, indent=2))
            else:
                if args.watch:
                    os.system("cls" if os.name == "nt" else "clear")
                render_ranking_cli(ranked, sort_by=args.sort)

        except Exception as e:
            print(
                f"{Colors.FAIL}Error running ranking: {e}{Colors.ENDC}", file=sys.stderr
            )

        if not args.watch:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
