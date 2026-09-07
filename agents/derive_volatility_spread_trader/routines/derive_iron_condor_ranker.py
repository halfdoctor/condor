"""
Derive Iron Condor Market Ranker & Strategy Monitor
Evaluates multi-factor scoring, strategy conviction / confidence, publishes
interactive ReportBuilder dashboard telemetry, and triggers actionable entry alerts
when Score >= 80 and Confidence >= 80.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

# Ensure agent directory and project root are on sys.path for local module imports
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = (
    Path(__file__).resolve().parents[2]
    if "agents" in CURRENT_DIR.parts
    else CURRENT_DIR.parent
)
AGENT_DIR = (
    CURRENT_DIR.parent
    if CURRENT_DIR.name == "routines"
    and CURRENT_DIR.parent.name == "derive_volatility_spread_trader"
    else PROJECT_ROOT / "agents" / "derive_volatility_spread_trader"
)

for path_dir in (str(AGENT_DIR), str(PROJECT_ROOT)):
    if path_dir not in sys.path:
        sys.path.insert(0, path_dir)

try:
    from rank_markets_iron_condor import DEFAULT_MARKETS, MarketRanker
except ImportError:
    from agents.derive_volatility_spread_trader.rank_markets_iron_condor import (
        DEFAULT_MARKETS,
        MarketRanker,
    )

from condor.reports import ReportBuilder
from routines.base import RoutineResult

logger = logging.getLogger(__name__)

CATEGORY = "Autonomous Trading"


class Config(BaseModel):
    """Multi-market Iron Condor scanner & monitor: evaluates quantitative multi-factor scoring and strategy conviction, publishes interactive ReportBuilder dashboard telemetry, and alerts/suggests entry when Score >= 80 & Confidence >= 80."""

    markets: str = Field(
        default="ETH,BTC,HYPE,XAUT,SOL,XRP,ZEC,ADA,CC",
        description="Comma-separated crypto assets to evaluate (Derive / Deribit options markets)",
    )
    min_score_alert: float = Field(
        default=80.0,
        description="Minimum quantitative multi-factor score threshold (0-100) to trigger entry alert/recommendation",
    )
    min_confidence_alert: float = Field(
        default=80.0,
        description="Minimum strategy conviction / confidence score (0-100) to trigger entry alert/recommendation",
    )
    sort_by: str = Field(
        default="readiness",
        description="Sort ranking by: readiness (deployment tier + score), score (raw multi-factor), or confidence",
    )
    iterations: int = Field(
        default=1,
        description="Number of monitor loop iterations (1 for single-shot scan, >1 for continuous monitor loop)",
    )
    poll_interval_seconds: int = Field(
        default=30,
        description="Interval in seconds between scan iterations in monitor loop",
    )
    telegram_chat_id: str | None = Field(
        default=None,
        description="Telegram Chat ID for entry alerts (auto-resolved from session/env if omitted)",
    )
    notify_on_alert_only: bool = Field(
        default=True,
        description="Only send Telegram alert when a market meets both min_score_alert and min_confidence_alert",
    )
    auto_refresh_seconds: int = Field(
        default=30,
        description="Interactive dashboard auto-refresh interval (seconds) in browser",
    )
    timeout_seconds: int = Field(
        default=5,
        description="HTTP request timeout per symbol in seconds",
    )
    workers: int = Field(
        default=8,
        description="Maximum concurrent worker threads for parallel options telemetry fetching",
    )


def sanitize_telegram_chat_id(raw_chat_id: Any) -> str | None:
    """Validate and sanitize Telegram chat ID."""
    if not raw_chat_id:
        return None
    val = str(raw_chat_id).strip().strip("\"'")
    if ":" in val:
        return None
    try:
        int(val)
        return val
    except ValueError:
        if val.startswith("@") and len(val) > 1:
            return val
        return None


def resolve_target_telegram_chat_id(
    chat_id: str | None, context: Any = None
) -> str | None:
    """Resolve destination Telegram chat ID with multi-level fallback."""
    clean = sanitize_telegram_chat_id(chat_id)
    if clean:
        return clean
    if context is not None:
        for attr in ("_chat_id", "chat_id"):
            val = getattr(context, attr, None)
            clean = sanitize_telegram_chat_id(val)
            if clean:
                return clean
    for env_key in ("ADMIN_USER_ID", "TELEGRAM_CHAT_ID", "CONDOR_CHAT_ID"):
        clean = sanitize_telegram_chat_id(os.environ.get(env_key))
        if clean:
            return clean
    return None


async def send_telegram_entry_alert(
    context: ContextTypes.DEFAULT_TYPE | None,
    chat_id: str | None,
    message: str,
) -> bool:
    """Dispatch real-time Telegram alert with Markdown formatting and multi-channel fallback."""
    target_chat = resolve_target_telegram_chat_id(chat_id, context)
    if not target_chat:
        logger.info("Telegram alert skipped: No valid chat_id resolved.")
        return False

    # 1. Try context.bot
    if context is not None and hasattr(context, "bot") and context.bot:
        try:
            await context.bot.send_message(
                chat_id=target_chat, text=message, parse_mode="Markdown"
            )
            return True
        except Exception as e:
            logger.debug(
                f"Failed Markdown alert via context.bot ({e}), retrying plain text..."
            )
            try:
                await context.bot.send_message(chat_id=target_chat, text=message)
                return True
            except Exception as e2:
                logger.warning(f"Failed plain text alert via context.bot: {e2}")

    # 2. Direct HTTP fallback via Telegram Bot API
    token = None
    for env_key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_TOKEN",
        "CONDOR_TELEGRAM_BOT_TOKEN",
    ):
        cand = os.environ.get(env_key)
        if cand and ":" in str(cand).strip():
            token = str(cand).strip()
            break

    if token:
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(
                    url,
                    json={
                        "chat_id": target_chat,
                        "text": message,
                        "parse_mode": "Markdown",
                    },
                )
                if r.status_code == 200 and r.json().get("ok"):
                    return True
                # Retry plain text if Markdown parsing failed
                r2 = await client.post(
                    url, json={"chat_id": target_chat, "text": message}
                )
                if r2.status_code == 200 and r2.json().get("ok"):
                    return True
        except Exception as e:
            logger.warning(f"Failed direct Telegram HTTP alert: {e}")

    return False


def format_entry_alert_message(candidate: dict) -> str:
    """Construct actionable Markdown trade recommendation for qualifying setup."""
    sym = candidate["symbol"]
    score = candidate["score"]
    conf = candidate.get("confidence", 0)
    spot = candidate["spot"]
    iv30 = candidate["iv30"]
    rv = candidate["rv"]
    vrp = candidate["vrp"]
    vol_reg = candidate.get("vol_reg", "N/A").upper()
    gex_reg = candidate.get("gex_reg", "N/A").upper()
    net_gamma = candidate.get("net_gamma_reg", "N/A")
    flip = candidate.get("flip", 0.0)
    dist_pct = candidate.get("dist_pct", 0.0)
    term_tag = candidate.get("term_tag", "N/A").upper()
    term_spread = candidate.get("term_spread", 0.0)

    strikes = candidate.get("suggested_strikes", {})
    sp_put = (
        f"${strikes.get('short_put', 0):,.2f}" if strikes.get("short_put") else "N/A"
    )
    sp_call = (
        f"${strikes.get('short_call', 0):,.2f}" if strikes.get("short_call") else "N/A"
    )
    lp_put = (
        f"${strikes.get('long_put_wing', 0):,.2f}"
        if strikes.get("long_put_wing")
        else "N/A"
    )
    lp_call = (
        f"${strikes.get('long_call_wing', 0):,.2f}"
        if strikes.get("long_call_wing")
        else "N/A"
    )
    wing_note = strikes.get("wing_note", "Standard symmetric wings (10–15 delta)")

    opt_dte = candidate.get("optimal_dte", {})
    exp_date = strikes.get("target_expiry") or opt_dte.get("target_expiry_date", "N/A")
    target_dte = strikes.get("target_dte") or opt_dte.get("target_dte", 14)

    return (
        f"🎯 *DERIVE IRON CONDOR ENTRY ALERT*\n\n"
        f"★ *Market:* `{sym}` | *Spot:* `${spot:,.2f}`\n"
        f"• *Score:* `{score}/100` | *Conviction:* `{conf}%`\n"
        f"• *Volatility Edge:* 30D IV `{iv30:.1f}%` vs RV `{rv:.1f}%` | VRP: `{vrp:+.2f}` vol pts ({vol_reg})\n"
        f"• *Dealer GEX:* `{gex_reg}` ({net_gamma}) | Spot vs Flip: `{dist_pct:+.2f}%` (Flip: `${flip:,.2f}`)\n"
        f"• *Term Structure:* `{term_tag}` (`{term_spread:+.2f}` vol pts theta slope)\n"
        f"• *Optimal Tenor:* `{target_dte} DTE` (Expiry: `{exp_date}`)\n\n"
        f"📋 *Recommended 4-Leg Execution Blueprint:*\n"
        f"  1. Buy Put Wing: `{lp_put}` (~10Δ outer protection)\n"
        f"  2. Sell Short Put: `{sp_put}` (safely below Gamma Flip `${flip:,.2f}`)\n"
        f"  3. Sell Short Call: `{sp_call}` (~30Δ inner body)\n"
        f"  4. Buy Call Wing: `{lp_call}` (~10Δ outer protection)\n"
        f"  • *Wing Architecture:* {wing_note}\n"
        f"  • *Delta Hedging:* Pair with `{sym}-PERP` (maintain delta band ±0.15)\n"
    )


def build_plotly_score_breakdown(active_markets: list[dict]):
    """Construct Plotly stacked bar figure showing 4 quantitative scoring factors."""
    import plotly.graph_objects as go

    symbols = [m["symbol"] for m in active_markets]
    vrp_scores = [
        m.get("score_breakdown", {}).get("vrp_score", 0) for m in active_markets
    ]
    gex_scores = [
        m.get("score_breakdown", {}).get("gex_score", 0) for m in active_markets
    ]
    term_scores = [
        m.get("score_breakdown", {}).get("term_score", 0) for m in active_markets
    ]
    skew_scores = [
        m.get("score_breakdown", {}).get("skew_score", 0) for m in active_markets
    ]

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            name="VRP / Vol Edge (Max 30)",
            x=symbols,
            y=vrp_scores,
            marker_color="#3b82f6",
        )
    )
    fig.add_trace(
        go.Bar(
            name="Dealer GEX / Regime (Max 35)",
            x=symbols,
            y=gex_scores,
            marker_color="#10b981",
        )
    )
    fig.add_trace(
        go.Bar(
            name="Term Structure Slope (Max 20)",
            x=symbols,
            y=term_scores,
            marker_color="#8b5cf6",
        )
    )
    fig.add_trace(
        go.Bar(
            name="Skew & Tail Risk (Max 15)",
            x=symbols,
            y=skew_scores,
            marker_color="#f59e0b",
        )
    )

    fig.add_hline(
        y=80,
        line_dash="dash",
        line_color="#22c55e",
        annotation_text="Target Entry Threshold (80 pts)",
        annotation_position="top left",
    )

    fig.update_layout(
        barmode="stack",
        title="Multi-Factor Score Composition by Asset (Target: >= 80 pts)",
        xaxis_title="Market",
        yaxis_title="Points (0 - 100)",
        yaxis=dict(range=[0, 105]),
        margin=dict(l=40, r=40, t=50, b=80),
        legend=dict(orientation="h", yanchor="top", y=-0.22, xanchor="center", x=0.5),
    )
    return fig


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> RoutineResult:
    """Execute MarketRanker scanning, multi-factor analysis, interactive report generation, and entry alerts."""
    symbols = [s.strip().upper() for s in config.markets.split(",") if s.strip()]
    if not symbols:
        symbols = DEFAULT_MARKETS

    ranker = MarketRanker(
        markets=symbols,
        timeout=config.timeout_seconds,
        workers=config.workers,
    )

    # State tracking for alerts
    alerted_symbols: set[str] = set()
    report_id: str | None = None
    iteration = 0
    max_iters = max(1, config.iterations)
    last_ranked: list[dict] = []
    qualifying_candidates: list[dict] = []

    try:
        while True:
            iteration += 1
            ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            logger.info(
                f"[derive_iron_condor_ranker] Iteration {iteration}/{max_iters} starting scan across {len(symbols)} markets..."
            )

            # Parallel scan in thread pool to preserve async responsiveness
            ranked = await asyncio.to_thread(ranker.scan_and_rank, config.sort_by)
            last_ranked = ranked

            active_markets = [m for m in ranked if m["status"] == "ACTIVE"]
            unavailable = [m for m in ranked if m["status"] != "ACTIVE"]

            # Filter candidates meeting Score >= 80 and Confidence >= 80
            qualifying_candidates = [
                m
                for m in active_markets
                if m["score"] >= config.min_score_alert
                and m.get("confidence", 0) >= config.min_confidence_alert
            ]

            # ── 1. Dispatch Alerts if Criteria Met ───────────────────────────
            for qual in qualifying_candidates:
                sym = qual["symbol"]
                if sym not in alerted_symbols:
                    alert_msg = format_entry_alert_message(qual)
                    delivered = await send_telegram_entry_alert(
                        context, config.telegram_chat_id, alert_msg
                    )
                    if delivered:
                        alerted_symbols.add(sym)
                        logger.info(
                            f"[derive_iron_condor_ranker] Dispatched Telegram entry alert for {sym} (Score {qual['score']}, Conf {qual['confidence']}%)"
                        )
                else:
                    logger.debug(
                        f"[derive_iron_condor_ranker] {sym} already alerted in this session; skipping redundant ping."
                    )

            # If notify_on_alert_only is False and iteration == 1, send summary message
            if (
                not config.notify_on_alert_only
                and iteration == 1
                and not qualifying_candidates
            ):
                top_sym = (
                    f"{active_markets[0]['symbol']} ({active_markets[0]['score']}/100, Conf {active_markets[0]['confidence']}%)"
                    if active_markets
                    else "None"
                )
                summary_ping = (
                    f"ℹ️ *Derive Iron Condor Scanner Completed*\n"
                    f"Evaluated {len(active_markets)} active markets. Top candidate: `{top_sym}`. "
                    f"No markets currently meet simultaneous Score >= {config.min_score_alert:.0f} & Confidence >= {config.min_confidence_alert:.0f}."
                )
                await send_telegram_entry_alert(
                    context, config.telegram_chat_id, summary_ping
                )

            # ── 2. Construct Interactive ReportBuilder Dashboard ─────────────
            builder = ReportBuilder(
                "Derive Iron Condor Market Scanner & Entry Telemetry"
            ).manual_order()
            builder.source("routine", "derive_iron_condor_ranker").tags(
                ["options", "iron-condor", "volatility", "derive", "signals"]
            )

            is_continuous_loop = max_iters > 1
            if is_continuous_loop:
                builder.auto_refresh(config.auto_refresh_seconds)

            # Top candidate details
            top_market = active_markets[0] if active_markets else None
            top_label = (
                f"{top_market['symbol']} ({top_market['score']}/100, Conf {top_market['confidence']}%)"
                if top_market
                else "None"
            )
            top_delta = (
                f"VRP: {top_market['vrp']:+.2f} vol pts" if top_market else "No Data"
            )

            avg_score = (
                sum(m["score"] for m in active_markets) / len(active_markets)
                if active_markets
                else 0.0
            )
            avg_conf = (
                sum(m.get("confidence", 0) for m in active_markets)
                / len(active_markets)
                if active_markets
                else 0.0
            )
            allowed_count = sum(1 for m in active_markets if m.get("allowed"))
            qualified_count = len(qualifying_candidates)

            # Section 1: Executive KPI Header
            builder.section(
                "01 / EXECUTIVE CONVICTION & DEPLOYMENT TELEMETRY",
                f"Multi-market scan executed at {ts_str} across {len(symbols)} assets. Entry gating: Score >= {config.min_score_alert:.0f} & Confidence >= {config.min_confidence_alert:.0f}.",
            )
            builder.kpi("Top Candidate", top_label, delta=top_delta, trend="up")
            builder.kpi(
                "Entry Ready (>=80/80)",
                str(qualified_count),
                delta=(
                    "Actionable Setups Available"
                    if qualified_count > 0
                    else "Waiting for Clearance"
                ),
                trend="up" if qualified_count > 0 else "neutral",
            )
            builder.kpi(
                "Allowed Deployments",
                f"{allowed_count}/{len(active_markets)}",
                delta=f"Avg Score: {avg_score:.1f}",
                trend="up" if allowed_count > 0 else "neutral",
            )
            builder.kpi(
                "Avg Market Conviction",
                f"{avg_conf:.1f}%",
                delta=f"Active Markets: {len(active_markets)}",
                trend="neutral",
            )

            # Prepare dataset rows for interactive components
            dataset_rows = []
            for m in active_markets:
                sb = m.get("score_breakdown", {})
                st = m.get("suggested_strikes", {})
                failed_str = (
                    ", ".join(m.get("failed_gates", []))
                    if m.get("failed_gates")
                    else "None (All Gates Passed)"
                )
                is_qualified = (
                    m["score"] >= config.min_score_alert
                    and m.get("confidence", 0) >= config.min_confidence_alert
                )

                deploy_status = (
                    "READY FOR ENTRY"
                    if is_qualified
                    else (
                        "ALLOWED (WATCH)" if m.get("allowed") else "FILTERED / BLOCKED"
                    )
                )

                dataset_rows.append(
                    {
                        "symbol": m["symbol"],
                        "score": m["score"],
                        "confidence": m.get("confidence", 0),
                        "vrp_score": sb.get("vrp_score", 0),
                        "gex_score": sb.get("gex_score", 0),
                        "term_score": sb.get("term_score", 0),
                        "skew_score": sb.get("skew_score", 0),
                        "spot_price": round(m["spot"], 2),
                        "iv_30d": round(m["iv30"], 1),
                        "realized_vol": round(m["rv"], 1),
                        "vrp": round(m["vrp"], 2),
                        "iv_rank": round(m.get("iv_rank", 0), 1),
                        "gamma_flip": round(m["flip"], 2),
                        "distance_to_flip_pct": round(m["dist_pct"], 2),
                        "term_spread": round(m["term_spread"], 2),
                        "term_structure": m.get("term_tag", "").upper(),
                        "gex_regime": m.get("gex_reg", "").upper(),
                        "net_gamma_regime": m.get("net_gamma_reg", ""),
                        "skew_bias": m.get("skew_bias", "").upper(),
                        "recommendation": m.get("rec", ""),
                        "deployment_status": deploy_status,
                        "entry_qualified": "YES" if is_qualified else "NO",
                        "target_dte": st.get("target_dte") or 14,
                        "target_expiry": st.get("target_expiry") or "N/A",
                        "short_put": st.get("short_put", 0),
                        "short_call": st.get("short_call", 0),
                        "long_put_wing": st.get("long_put_wing", 0),
                        "long_call_wing": st.get("long_call_wing", 0),
                        "failed_gates": failed_str,
                    }
                )

            # Section 2: Interactive Filtering & Matrix Explorer
            builder.section(
                "02 / INTERACTIVE CONVICTION MATRIX & FILTERING",
                "Explore multi-factor scores, gamma clearance, and conviction across all listed Derive options assets.",
            )

            if dataset_rows:
                builder.dataset("markets", dataset_rows)

                # Filter controls
                builder.select_filter(
                    "symbol-filter", "markets", "symbol", label="Asset"
                )
                builder.select_filter(
                    "status-filter",
                    "markets",
                    "deployment_status",
                    label="Deployment Status",
                )
                builder.select_filter(
                    "gex-filter",
                    "markets",
                    "gex_regime",
                    label="Dealer Gamma Regime",
                )
                builder.range_filter(
                    "score-filter",
                    "markets",
                    "score",
                    label="Multi-Factor Score",
                    value_type="number",
                )
                builder.range_filter(
                    "conf-filter",
                    "markets",
                    "confidence",
                    label="Confidence Conviction",
                    value_type="number",
                )

                # Dynamic Filter-Aware Metrics
                builder.metric("Filtered Candidates", "markets", aggregate="count")
                builder.metric(
                    "Avg Filtered Score",
                    "markets",
                    "score",
                    aggregate="mean",
                    format=".1f",
                )
                builder.metric(
                    "Avg Filtered Conf",
                    "markets",
                    "confidence",
                    aggregate="mean",
                    format=".1f",
                    suffix="%",
                )
                builder.metric(
                    "Max Score",
                    "markets",
                    "score",
                    aggregate="max",
                    format=".0f",
                )

                # Interactive Visualizations
                builder.chart(
                    "bar",
                    "Multi-Factor Score (0-100) by Symbol",
                    "markets",
                    x="symbol",
                    y="score",
                    color="deployment_status",
                    x_label="Asset",
                    y_label="Multi-Factor Score",
                    height=360,
                )

                builder.chart(
                    "scatter",
                    "Score vs Conviction Conviction Matrix (Target: Top-Right >= 80/80)",
                    "markets",
                    x="score",
                    y="confidence",
                    color="deployment_status",
                    x_label="Quantitative Score (0-100)",
                    y_label="Strategy Conviction (%)",
                    height=380,
                )

                # Plotly stacked bar breakdown
                breakdown_fig = build_plotly_score_breakdown(active_markets)
                builder.plotly(breakdown_fig)

                # Filter-Aware Data Table
                builder.data_table(
                    "markets",
                    title="Derive Multi-Market Options Telemetry & Strike Selection Table",
                    columns=[
                        "symbol",
                        "deployment_status",
                        "score",
                        "confidence",
                        "spot_price",
                        "vrp",
                        "iv_30d",
                        "gex_regime",
                        "distance_to_flip_pct",
                        "term_structure",
                        "recommendation",
                        "failed_gates",
                    ],
                )

            # Section 3: Actionable Entry & Execution Recommendations
            builder.section(
                "03 / ACTIONABLE TRADING SIGNALS & STRIKE ARCHITECTURE",
                "Concrete defined-risk Iron Condor structures and delta-hedged execution rules.",
            )

            rec_md = []
            if qualifying_candidates:
                rec_md.append(
                    "### 🚀 ACTIONABLE HIGH-CONVICTION DEPLOYMENTS (Score >= 80 & Confidence >= 80)\n"
                )
                for q in qualifying_candidates:
                    st = q.get("suggested_strikes", {})
                    opt = q.get("optimal_dte", {})
                    exp = st.get("target_expiry") or opt.get(
                        "target_expiry_date", "N/A"
                    )
                    dte_val = st.get("target_dte") or opt.get("target_dte", 14)

                    rec_md.append(f"#### **{q['symbol']}** — DEPLOYMENT READY")
                    rec_md.append(
                        f"- **Multi-Factor Score:** `{q['score']}/100` | **Strategy Conviction:** `{q['confidence']}%`"
                    )
                    rec_md.append(
                        f"- **Spot:** `${q['spot']:,.2f}` | **Gamma Flip:** `${q['flip']:,.2f}` (`{q['dist_pct']:+.2f}%` buffer)"
                    )
                    rec_md.append(
                        f"- **Volatility Edge:** 30D IV `{q['iv30']:.1f}%` vs RV `{q['rv']:.1f}%` (VRP `{q['vrp']:+.2f}` vol pts)"
                    )
                    rec_md.append(
                        f"- **Term Structure:** `{q['term_tag'].upper()}` ({q['term_spread']:+.2f} pts theta slope)"
                    )
                    rec_md.append(
                        f"- **Optimal Tenor:** `{dte_val} DTE` (Target Expiry: `{exp}`)"
                    )
                    rec_md.append(f"- **Suggested 4-Leg Strike Architecture:**")
                    rec_md.append(
                        f"  - Long Put Wing: `${st.get('long_put_wing', 0):,.2f}`"
                    )
                    rec_md.append(
                        f"  - Short Put: `${st.get('short_put', 0):,.2f}` (Below Dealer Gamma Flip)"
                    )
                    rec_md.append(f"  - Short Call: `${st.get('short_call', 0):,.2f}`")
                    rec_md.append(
                        f"  - Long Call Wing: `${st.get('long_call_wing', 0):,.2f}`"
                    )
                    rec_md.append(
                        f"  - *Wing Structure:* {st.get('wing_note', 'Standard symmetric wings')}"
                    )
                    rec_md.append(
                        f"- **Perp Delta Hedge Rule:** Pair with `{q['symbol']}-PERP` on Derive. Rebalance when |Net Delta| > 0.15.\n"
                    )
            else:
                rec_md.append(
                    "> **[STATUS: WATCHLIST / PENDING CLEARANCE]**\n"
                    f"> No markets currently satisfy simultaneous **Score >= {config.min_score_alert:.0f}** and **Confidence >= {config.min_confidence_alert:.0f}**.\n"
                )

                if top_market:
                    rec_md.append(
                        f"### Current Lead Candidate: **{top_market['symbol']}** (Score: {top_market['score']}/100, Confidence: {top_market['confidence']}%)\n"
                    )
                    rec_md.append(
                        f"- **Current VRP Edge:** `{top_market['vrp']:+.2f}` vol pts (IV30: `{top_market['iv30']:.1f}%` vs RV: `{top_market['rv']:.1f}%`)"
                    )
                    rec_md.append(
                        f"- **Dealer Gamma Regime:** `{top_market['gex_reg'].upper()}` (Spot `${top_market['spot']:,.2f}` vs Flip `${top_market['flip']:,.2f}`)"
                    )
                    rec_md.append(
                        f"- **Term Structure:** `{top_market['term_tag'].upper()}` ({top_market['term_spread']:+.2f} pts)"
                    )

                rec_md.append("\n### ⚡ Watchlist Required Triggers for Entry:")
                for m in active_markets[:5]:
                    failed = m.get("failed_gates", [])
                    triggers = []
                    if "Flip" in failed or m["dist_pct"] <= 0:
                        triggers.append(
                            f"Spot (${m['spot']:,.2f}) must clear Gamma Flip (${m['flip']:,.2f})"
                        )
                    if "GEX" in failed or m["net_gamma_reg"] not in [
                        "long_gamma",
                        "positive",
                    ]:
                        triggers.append(
                            f"Dealer GEX must turn positive (currently {m['net_gamma_reg']})"
                        )
                    if "Term" in failed or m["term_tag"] != "contango":
                        triggers.append(
                            f"Term structure must steepen into contango (spread: {m['term_spread']:+.1f} pts)"
                        )
                    if "Vol" in failed or m["vrp"] < 2.0:
                        triggers.append(
                            f"VRP vol edge must expand to positive territory (currently {m['vrp']:+.2f} pts)"
                        )
                    if "Skew" in failed:
                        triggers.append(
                            f"Skew must normalize from put panic (25D RR: {m['rr25']:.1f})"
                        )
                    if not triggers:
                        triggers.append("Score & confidence must cross 80 threshold")

                    rec_md.append(
                        f"- **{m['symbol']}** (Score {m['score']}/100 | Conf {m['confidence']}%): {' & '.join(triggers)}"
                    )

            if unavailable:
                unavail_syms = ", ".join(m["symbol"] for m in unavailable)
                rec_md.append(
                    f"\n_Note: {len(unavailable)} listed candidate(s) currently unlisted or lack options liquidity: {unavail_syms}._"
                )

            builder.markdown("\n".join(rec_md))

            # Save / update report
            report_id = await builder.save(report_id=report_id)
            logger.info(
                f"[derive_iron_condor_ranker] Report saved/updated (ID: {report_id})"
            )

            # Check loop termination
            if iteration >= max_iters:
                break

            await asyncio.sleep(config.poll_interval_seconds)

    except asyncio.CancelledError:
        logger.info(
            f"[derive_iron_condor_ranker] Monitor cancelled after {iteration} iteration(s)."
        )
        if report_id:
            # Re-save with auto_refresh disabled to leave a static offline snapshot
            try:
                builder = ReportBuilder(
                    "Derive Iron Condor Market Scanner (Stopped)"
                ).manual_order()
                builder.source("routine", "derive_iron_condor_ranker").tags(
                    ["options", "iron-condor", "derive"]
                )
                builder.auto_refresh(None)
                builder.section(
                    "MONITOR STOPPED",
                    f"Routine execution ended at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}.",
                )
                builder.kpi("Status", "Stopped", delta=f"Ran {iteration} iterations")
                await builder.save(report_id=report_id)
            except Exception as e_stop:
                logger.debug(f"Failed to write stopped report snapshot: {e_stop}")
        return RoutineResult(
            text=f"Derive Iron Condor monitor stopped after {iteration} iterations (Report: {report_id})."
        )

    # ── 3. Build Result Summary & Table ──────────────────────────────────────
    summary_lines = []
    summary_lines.append(f"🎯 **Derive Iron Condor Multi-Market Ranking & Telemetry**")
    summary_lines.append(
        f"Scan Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} | Evaluated: {len(active_markets)} active markets"
    )
    if report_id:
        summary_lines.append(f"📊 Dashboard Report: `{report_id}`")

    if qualifying_candidates:
        ready_syms = ", ".join(
            f"{q['symbol']} ({q['score']}/100, {q['confidence']}%)"
            for q in qualifying_candidates
        )
        summary_lines.append(f"\n🚀 **Actionable Setups Ready:** {ready_syms}")
    else:
        top_str = (
            f"{last_ranked[0]['symbol']} (Score {last_ranked[0]['score']}/100, Conf {last_ranked[0].get('confidence', 0)}%)"
            if last_ranked
            else "None"
        )
        summary_lines.append(
            f"\n⚡ **Status:** No markets currently meet simultaneous Score >= {config.min_score_alert:.0f} & Confidence >= {config.min_confidence_alert:.0f}."
        )
        summary_lines.append(f"Top Candidate on Watchlist: {top_str}")

    # Build structured table data for UI
    table_cols = [
        "Rank",
        "Market",
        "Score",
        "Confidence",
        "Spot",
        "VRP",
        "GEX Regime",
        "Dist Flip",
        "Term Structure",
        "Signal",
        "Entry Alert",
    ]
    table_rows = []
    for idx, m in enumerate(active_markets, 1):
        is_qual = (
            m["score"] >= config.min_score_alert
            and m.get("confidence", 0) >= config.min_confidence_alert
        )
        table_rows.append(
            {
                "Rank": idx,
                "Market": m["symbol"],
                "Score": f"{m['score']}/100",
                "Confidence": f"{m.get('confidence', 0)}%",
                "Spot": f"${m['spot']:,.2f}",
                "VRP": f"{m['vrp']:+.2f}",
                "GEX Regime": m.get("gex_reg", "").upper(),
                "Dist Flip": f"{m.get('dist_pct', 0.0):+.2f}%",
                "Term Structure": f"{m.get('term_tag', '').upper()} ({m.get('term_spread', 0.0):+.1f})",
                "Signal": m.get("rec", ""),
                "Entry Alert": "🚀 READY" if is_qual else "WATCH",
            }
        )

    sections = [
        {
            "type": "kpi",
            "label": "Top Market",
            "value": (
                f"{active_markets[0]['symbol']} ({active_markets[0]['score']})"
                if active_markets
                else "None"
            ),
        },
        {
            "type": "kpi",
            "label": "Ready Setups",
            "value": str(len(qualifying_candidates)),
        },
        {
            "type": "kpi",
            "label": "Avg Score",
            "value": f"{avg_score:.1f}/100",
        },
        {
            "type": "kpi",
            "label": "Report ID",
            "value": report_id or "N/A",
        },
    ]

    return RoutineResult(
        text="\n".join(summary_lines),
        table_data=table_rows,
        table_columns=table_cols,
        sections=sections,
    )
