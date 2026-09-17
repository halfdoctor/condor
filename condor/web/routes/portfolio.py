from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from condor.fetchers.portfolio import (
    PORTFOLIO_HISTORY_RANGES,
    UNIFIED_ACCOUNT_NOTE,
    balance_value,
    dedupe_unified_accounts,
)
from condor.server_data_service import ServerDataType, get_server_data_service
from condor.web.auth import require_server_access
from condor.web.models import (
    BalanceItem,
    ConnectorBalance,
    PortfolioHistoryPoint,
    PortfolioHistoryResponse,
    PortfolioResponse,
    WebUser,
)
from condor.web.routes._errors import upstream_error
from config_manager import get_config_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["portfolio"])


@router.get("/servers/{name}/portfolio", response_model=PortfolioResponse)
async def get_portfolio(
    name: str,
    refresh: bool = Query(False),
    user: WebUser = Depends(require_server_access),
):
    cm = get_config_manager()

    try:
        if refresh:
            # Bypass SDS cache — force exchange re-fetch via Hummingbot
            from condor.fetchers.portfolio import fetch_portfolio_refreshed

            client = await cm.get_client(name)
            state = await fetch_portfolio_refreshed(client)
            # Update SDS cache so subsequent non-refresh reads get fresh data
            get_server_data_service().put(name, ServerDataType.PORTFOLIO, state)
        else:
            state = await get_server_data_service().get_or_fetch(
                name, ServerDataType.PORTFOLIO
            )
    except Exception as e:
        logger.exception("Portfolio fetch failed for '%s'", name)
        raise upstream_error("Failed to get portfolio", e)

    if state is None:
        # Check if fetch is registered
        sds = get_server_data_service()
        registered = ServerDataType.PORTFOLIO in sds._fetch_registry
        logger.warning(
            "Portfolio returned None for %s (fetch registered: %s, health: %s)",
            name,
            registered,
            (
                sds.get_server_health(name).status.value
                if name in sds._health
                else "unknown"
            ),
        )
        raise HTTPException(
            status_code=502,
            detail=f"Failed to get portfolio: server '{name}' returned no data"
            + ("" if registered else " (fetch not registered — try restarting)"),
        )

    # Parse portfolio state into structured response
    # API returns: {account_name: {connector_name: [balance_dicts]}}
    state, unified = dedupe_unified_accounts(state)
    connectors: list[ConnectorBalance] = []
    total_usd = 0.0

    if isinstance(state, dict):
        for account_name, account_data in state.items():
            if not isinstance(account_data, dict):
                continue

            for connector_name, connector_balances in account_data.items():
                if not isinstance(connector_balances, list):
                    continue

                balances: list[BalanceItem] = []
                connector_total = 0.0

                for item in connector_balances:
                    if not isinstance(item, dict):
                        continue
                    token = item.get("token", item.get("asset", ""))
                    total_bal = float(item.get("units", item.get("total_balance", 0)))
                    available = float(
                        item.get(
                            "available_units", item.get("available_balance", total_bal)
                        )
                    )
                    usd_val = balance_value(item)

                    if not token:
                        continue

                    balances.append(
                        BalanceItem(
                            token=token,
                            total=total_bal,
                            available=available,
                            usd_value=usd_val,
                        )
                    )
                    connector_total += usd_val

                # Filter out zero-value tokens and sort by absolute value descending
                balances = [
                    b for b in balances if abs(b.usd_value) >= 0.01 or abs(b.total) > 0
                ]
                balances.sort(key=lambda b: abs(b.usd_value), reverse=True)

                connectors.append(
                    ConnectorBalance(
                        connector=connector_name,
                        balances=balances,
                        total_usd=connector_total,
                        note=(
                            UNIFIED_ACCOUNT_NOTE
                            if (account_name, connector_name) in unified
                            else None
                        ),
                    )
                )

    _dedupe_derive_unified(connectors)
    total_usd = sum(c.total_usd for c in connectors)
    return PortfolioResponse(server=name, connectors=connectors, total_usd=total_usd)


def _dedupe_derive_unified(connectors: list[ConnectorBalance]) -> None:
    """Avoid double-counting Derive balances for unified subaccounts.

    Derive uses a unified cross-collateral subaccount where the same collateral (USDC, ETH, HYPE,
    etc.) backs both options and perpetuals. When both ``derive`` and ``derive_perpetual`` are
    connected, the collateral assets are reported by both connectors. Deduplicate by dropping
    matching collateral balances from ``derive_perpetual`` so they are reflected under ``derive`` only.
    Mutates ``connectors`` in place.
    """
    spot = next((c for c in connectors if c.connector == "derive"), None)
    perp = next((c for c in connectors if c.connector == "derive_perpetual"), None)
    if spot is None or perp is None:
        return

    spot_by_token = {b.token: b for b in spot.balances}
    deduped_balances: list[BalanceItem] = []
    dropped_any = False

    for b in perp.balances:
        spot_b = spot_by_token.get(b.token)
        if spot_b is not None:
            # Check if units or USD values match within 1% (or float precision delta)
            units_match = abs(b.total - spot_b.total) <= max(
                1e-4, 0.01 * abs(spot_b.total)
            )
            usd_match = abs(b.usd_value - spot_b.usd_value) <= max(
                0.1, 0.01 * max(abs(spot_b.usd_value), abs(b.usd_value))
            )
            if units_match or usd_match:
                dropped_any = True
                continue
        deduped_balances.append(b)

    if dropped_any:
        perp.balances = deduped_balances
        perp.total_usd = sum(b.usd_value for b in perp.balances)
        perp.note = "Unified account — balance is reflected in the derive balance."


@router.get(
    "/servers/{name}/portfolio/history", response_model=PortfolioHistoryResponse
)
async def get_portfolio_history(
    name: str,
    range: str = Query("1D", pattern="^(1D|1W|1M|3M)$"),
    breakdown: bool = Query(False),
    user: WebUser = Depends(require_server_access),
):

    # The raw snapshots are the same regardless of *breakdown* (parsing happens
    # below), so the cache key is the range alone. SDS coalesces concurrent
    # reads of the same expired key into one backend fetch.
    try:
        history = await get_server_data_service().get_or_fetch(
            name, ServerDataType.PORTFOLIO_HISTORY, range_key=range
        )
    except Exception as e:
        logger.warning("Failed to get portfolio history: %s", e)
        history = None

    _, interval = PORTFOLIO_HISTORY_RANGES[range]

    logger.debug("Portfolio history response shape: %s", type(history))
    if isinstance(history, dict):
        logger.debug("Portfolio history keys: %s", list(history.keys()))

    entries, keyed = _extract_snapshot_entries(history)

    # Forward-fill: track per-connector totals so missing exchanges
    # carry forward their last known value instead of dropping to 0.
    points: list[PortfolioHistoryPoint] = []
    prev_connector_totals: dict[str, float] = {}
    for ts, snapshot in entries:
        total = (
            0 if keyed else snapshot.get("total_value", snapshot.get("total_usd", 0))
        )
        if total == 0:
            # Sum token values from nested structure
            # API returns {timestamp, state: {account: {connector: [balances]}}}
            cur_totals = _extract_connector_totals(_snapshot_state(snapshot, keyed))
            if cur_totals and prev_connector_totals:
                # Forward-fill: for connectors seen before but missing now, use previous value
                for key, prev_val in prev_connector_totals.items():
                    if key not in cur_totals:
                        cur_totals[key] = prev_val
            if cur_totals:
                prev_connector_totals = cur_totals
            total = sum(cur_totals.values())
        if keyed and total <= 0:
            # Dict-keyed payloads historically drop zero-total points
            continue
        points.append(PortfolioHistoryPoint(timestamp=ts, total_usd=float(total)))

    points.sort(key=lambda p: p.timestamp)

    top_tokens: list[str] = []
    if breakdown and points:
        top_tokens = _build_token_breakdown(entries, keyed, points)

    return PortfolioHistoryResponse(
        server=name, points=points, interval=interval, top_tokens=top_tokens
    )


_SNAPSHOT_LIST_KEYS = ("data", "snapshots", "history", "points", "results")


def _snapshot_state(snapshot: Any, keyed: bool) -> Any:
    """Return the portfolio state within a snapshot entry."""
    return snapshot if keyed else snapshot.get("state", snapshot)


def _extract_snapshot_entries(history: Any) -> tuple[list[tuple[float, Any]], bool]:
    """Normalize a history response into sorted (timestamp, snapshot) entries.

    Handles three shapes: a bare list of snapshots, a dict with the snapshot
    list under a common key, or a dict mapping timestamps to portfolio states.
    Returns (entries, keyed); keyed=True means the dict-keyed-timestamp shape,
    where each entry holds a raw portfolio state instead of a snapshot dict.
    """
    snapshots = None
    if isinstance(history, list):
        snapshots = history
    elif isinstance(history, dict):
        # Try common keys
        for key in _SNAPSHOT_LIST_KEYS:
            if key in history and isinstance(history[key], list):
                snapshots = history[key]
                break
        if snapshots is None:
            # The dict itself maps timestamps → portfolio states
            keyed_entries: list[tuple[float, Any]] = [
                (_parse_timestamp(ts_key), snapshot_data)
                for ts_key, snapshot_data in history.items()
            ]
            keyed_entries.sort(key=lambda x: x[0])
            return keyed_entries, True

    entries: list[tuple[float, Any]] = []
    for snapshot in snapshots or []:
        if not isinstance(snapshot, dict):
            continue
        ts = snapshot.get("timestamp", snapshot.get("time", snapshot.get("t", 0)))
        if not ts:
            continue
        entries.append((_parse_timestamp(ts), snapshot))
    entries.sort(key=lambda x: x[0])
    return entries, False


def _build_token_breakdown(
    entries: list[tuple[float, Any]],
    keyed: bool,
    points: list[PortfolioHistoryPoint],
) -> list[str]:
    """Populate each point's per-token values, collapsing beyond the top 8 into "Other".

    Mutates ``point.tokens`` in place and returns the top token names.
    """
    # Build token values per timestamp (with forward-fill for missing exchanges)
    ts_token_map: dict[float, dict[str, float]] = {}
    prev_token_vals: dict[str, float] = {}
    for ts, snapshot in entries:
        token_vals = _extract_token_values(_snapshot_state(snapshot, keyed))
        if not token_vals:
            continue
        # Forward-fill: tokens present before but missing now keep previous value
        if prev_token_vals:
            for tk, tv in prev_token_vals.items():
                if tk not in token_vals:
                    token_vals[tk] = tv
        prev_token_vals = token_vals
        ts_token_map[ts] = token_vals

    if not ts_token_map:
        return []

    # Determine top 8 tokens by aggregate value
    agg: dict[str, float] = {}
    for tv in ts_token_map.values():
        for token, val in tv.items():
            agg[token] = agg.get(token, 0) + val
    top_tokens = sorted(agg, key=lambda t: agg[t], reverse=True)[:8]
    top_set = set(top_tokens)

    # Populate token breakdown on each point, collapsing rest into "Other"
    for point in points:
        tv = ts_token_map.get(point.timestamp, {})
        if not tv:
            continue
        tokens_out: dict[str, float] = {}
        other = 0.0
        for token, val in tv.items():
            if token in top_set:
                tokens_out[token] = val
            else:
                other += val
        if other > 0:
            tokens_out["Other"] = other
        point.tokens = tokens_out

    if "Other" in {t for p in points for t in p.tokens} and "Other" not in top_tokens:
        top_tokens.append("Other")
    return top_tokens


def _parse_timestamp(val: object) -> float:
    """Parse a timestamp that may be numeric or ISO 8601 string."""
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val)
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(val)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, TypeError):
            pass
    return 0.0


def _dedupe_snapshot_connectors(account_data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of account connectors dict with duplicated unified balances removed."""
    if not isinstance(account_data, dict):
        return account_data

    connectors = dict(account_data)

    # Derive deduplication
    if "derive" in connectors and "derive_perpetual" in connectors:
        spot_list = connectors["derive"]
        perp_list = connectors["derive_perpetual"]
        if isinstance(spot_list, list) and isinstance(perp_list, list):
            spot_by_token = {
                item.get("token", item.get("asset", "")): item
                for item in spot_list
                if isinstance(item, dict)
            }
            cleaned_perp = []
            for item in perp_list:
                if isinstance(item, dict):
                    token = item.get("token", item.get("asset", ""))
                    spot_item = spot_by_token.get(token)
                    if spot_item is not None:
                        u1 = float(item.get("units", item.get("total_balance", 0)))
                        u2 = float(
                            spot_item.get("units", spot_item.get("total_balance", 0))
                        )
                        v1 = float(item.get("value", item.get("usd_value", 0)))
                        v2 = float(
                            spot_item.get("value", spot_item.get("usd_value", 0))
                        )
                        if abs(u1 - u2) <= max(1e-4, 0.01 * abs(u2)) or abs(
                            v1 - v2
                        ) <= max(0.1, 0.01 * max(abs(v1), abs(v2))):
                            continue
                cleaned_perp.append(item)
            connectors["derive_perpetual"] = cleaned_perp

    # Hyperliquid deduplication
    if "hyperliquid" in connectors and "hyperliquid_perpetual" in connectors:
        spot_list = connectors["hyperliquid"]
        perp_list = connectors["hyperliquid_perpetual"]
        if isinstance(spot_list, list) and isinstance(perp_list, list):
            spot_stables = sum(
                float(item.get("value", item.get("usd_value", 0)))
                for item in spot_list
                if isinstance(item, dict)
                and item.get("token", item.get("asset", "")) in _HL_STABLES
            )
            perp_stables = sum(
                float(item.get("value", item.get("usd_value", 0)))
                for item in perp_list
                if isinstance(item, dict)
                and item.get("token", item.get("asset", "")) in _HL_STABLES
            )
            if (
                spot_stables > 0
                and perp_stables > 0
                and abs(perp_stables - spot_stables) <= max(0.01, 0.01 * spot_stables)
            ):
                connectors["hyperliquid_perpetual"] = [
                    item
                    for item in perp_list
                    if isinstance(item, dict)
                    and item.get("token", item.get("asset", "")) not in _HL_STABLES
                ]

    return connectors


def _extract_token_values(data: object) -> dict[str, float]:
    """Extract per-token USD values from a portfolio snapshot."""
    tokens: dict[str, float] = {}
    if not isinstance(data, dict):
        return tokens
    for val in data.values():
        if isinstance(val, dict):
            clean_val = _dedupe_snapshot_connectors(val)
            for inner in clean_val.values():
                if isinstance(inner, list):
                    for item in inner:
                        if isinstance(item, dict):
                            token = item.get("token", item.get("asset", ""))
                            usd = balance_value(item)
                            if token and (
                                usd != 0
                                or float(
                                    item.get("units", item.get("total_balance", 0))
                                )
                                != 0
                            ):
                                tokens[token] = tokens.get(token, 0) + usd
    return tokens


def _extract_connector_totals(data: object) -> dict[str, float]:
    """Extract per-connector USD totals from a portfolio snapshot."""
    totals: dict[str, float] = {}
    if not isinstance(data, dict):
        return totals
    for account, val in data.items():
        if isinstance(val, dict):
            clean_val = _dedupe_snapshot_connectors(val)
            for connector, inner in clean_val.items():
                key = f"{account}:{connector}"
                if isinstance(inner, list):
                    s = 0.0
                    for item in inner:
                        if isinstance(item, dict):
                            s += balance_value(item)
                    totals[key] = s
                elif isinstance(inner, (int, float)):
                    totals[key] = float(inner)
        elif isinstance(val, (int, float)):
            totals[account] = float(val)
    return totals
