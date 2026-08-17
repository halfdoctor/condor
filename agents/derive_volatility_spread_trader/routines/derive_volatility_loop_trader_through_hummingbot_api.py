import math
import json
import logging
import datetime
import os
import sys
import time
import asyncio
import httpx
from typing import Any
from pathlib import Path
from decimal import Decimal
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from telegram.ext import ContextTypes
from condor.reports import LiveReport, ReportBuilder
from routines.base import RoutineResult

load_dotenv()

logger = logging.getLogger(__name__)

# Mark as continuous routine for Condor server-side background lifecycle
CONTINUOUS = True
CATEGORY = "Autonomous Trading"

# Dynamically resolve paths relative to current file and environment (Docker / Multi-host portable)
ROUTINES_DIR = Path(__file__).resolve().parent
STATE_FILE = Path(os.getenv("POSITION_STATE_FILE", ROUTINES_DIR / ".position_state.json"))
HEARTBEAT_FILE = Path(os.getenv("HEARTBEAT_FILE", ROUTINES_DIR / ".heartbeat.json"))

# Global in-memory cache for instrument mark prices across polling ticks
LAST_KNOWN_MARK_PRICES: dict[str, float] = {}

class Config(BaseModel):
    """Autonomous Decoupled Dual-Cadence Trader: Derivatives Monkey Consensus Cross-Check, Pure Target Delta (30Δ/10Δ), 30s Fast Rehedge, 50 Bps Slippage Protection"""
    trading_pair: str = Field(default="ETH-USDT", description="Underlying asset trading pair (ETH-USDT, BTC-USDT, HYPE-USDT)")
    poll_interval_seconds: int = Field(default=300, description="Macro loop interval for options market volatility analysis, surface GEX, and RFQ scale-in/take-profit (default 300s / 5m)")
    perp_hedge_interval_seconds: int = Field(default=30, description="Fast sub-loop interval for perpetual delta rebalancing and spot price tracking to prevent gamma drift (default 30s)")
    paper_mode: bool = Field(default=False, description="Dry-run paper mode (True = Simulated execution & alerts; False = Live exchange orders)")
    enable_options_entry: bool = Field(default=True, description="Enable automatic RFQ scale-in and entry for new options packages. When False, runs in Hedge-Only mode (maintains, delta-hedges, and exits existing positions without opening new ones)")
    short_delta_target: float = Field(default=0.30, description="Target Delta for Short Call & Put legs (e.g. 0.30 = ~30 Delta / inner body of Iron Condor)")
    wing_delta_target: float = Field(default=0.10, description="Target Delta for Long Call & Put wings (e.g. 0.10 = ~10 Delta / outer wing protection)")
    dte: int = Field(default=14, description="Target Days to Expiration for newly initiated options structures")
    min_edge: float = Field(default=5.0, description="Minimum net volatility edge required for entry/scale-in (vol points)")
    hedge_band: float = Field(default=0.05, description="Rebalance delta threshold (|Net Delta| > band) triggering perp hedge")
    emergency_band: float = Field(default=0.20, description="Emergency delta threshold triggering market hedge rebalance")
    max_margin_utilization_pct: float = Field(default=75.0, description="Maximum subaccount margin utilization cap (%) beyond which no new options are entered")
    max_perp_delta_hedge_ratio: float = Field(default=1.0, description="Cap multiplier ensuring perpetual delta never exceeds options delta hedge requirement")
    max_perp_slippage_bps: int = Field(default=50, description="Max slippage tolerance in basis points (50 bps = 0.50%) for perpetual hedge execution limit price protection")
    target_profit_pct: float = Field(default=60.0, description="Take-profit percentage of net premium collected (e.g. 60.0%)")
    stop_loss_pct: float = Field(default=100.0, description="Stop-loss percentage of net premium collected (e.g. 100.0% = 1x net credit loss)")
    capital_allocation: float = Field(default=10000.0, description="Capital allocated for strategy collateral ($)")
    contract_size: float = Field(default=1.0, description="Option leg contract sizing in base currency (e.g. 1.0 ETH, 0.1 BTC)")
    telegram_chat_id: str | None = Field(default=None, description="Telegram Chat ID for real-time vol edge alerts (falls back to ADMIN_USER_ID or TELEGRAM_CHAT_ID in .env)")
    notify_on_change_only: bool = Field(default=True, description="Only dispatch Telegram notification when a position changes or order executes")
    options_route: str = Field(default="RFQ_COMBO_PACKAGE", description="Primary options execution route (RFQ_COMBO_PACKAGE or ORDERBOOK_SEQUENTIAL)")
    smart_contract_wallet: str | None = Field(default=None, description="Derive Smart Contract Wallet address (falls back to DERIVE_SMART_CONTRACT_WALLET in .env)")
    session_key_priv: str | None = Field(default=None, description="Derive Session Key private key (falls back to DERIVE_SESSION_KEY_PRIV in .env)")
    subaccount_id: int | None = Field(default=None, description="Derive Subaccount ID (falls back to DERIVE_SUBACCOUNT_ID in .env)")

def bs_d1(S, K, T, r, sigma):
    if T <= 1e-5 or sigma <= 1e-5:
        return 0.0
    return (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))

def bs_norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def norm_inv(p: float) -> float:
    """Abramowitz & Stegun rational approximation for inverse standard normal CDF."""
    if p <= 0.0 or p >= 1.0:
        p = max(1e-6, min(0.999999, p))
    if p < 0.5:
        t = math.sqrt(-2.0 * math.log(p))
        c0, c1, c2 = 2.515517, 0.802853, 0.010328
        d1, d2, d3 = 1.432788, 0.189269, 0.001308
        return -(t - ((c2 * t + c1) * t + c0) / (((d3 * t + d2) * t + d1) * t + 1.0))
    else:
        t = math.sqrt(-2.0 * math.log(1.0 - p))
        c0, c1, c2 = 2.515517, 0.802853, 0.010328
        d1, d2, d3 = 1.432788, 0.189269, 0.001308
        return t - ((c2 * t + c1) * t + c0) / (((d3 * t + d2) * t + d1) * t + 1.0)

def strike_from_delta(S: float, T: float, r: float, sigma: float, target_delta: float, is_call: bool = True) -> float:
    """Calculate continuous theoretical strike price corresponding to an exact target Delta."""
    if is_call:
        d1 = norm_inv(target_delta)
    else:
        d1 = norm_inv(1.0 - target_delta)
    return S * math.exp((r + 0.5 * sigma ** 2) * T - d1 * sigma * math.sqrt(T))

def bs_call_price(S, K, T, r, sigma):
    if T <= 1e-5:
        return max(0.0, S - K)
    d1 = bs_d1(S, K, T, r, sigma)
    d2 = d1 - sigma * math.sqrt(T)
    return S * bs_norm_cdf(d1) - K * math.exp(-r * T) * bs_norm_cdf(d2)

def bs_put_price(S, K, T, r, sigma):
    if T <= 1e-5:
        return max(0.0, K - S)
    d1 = bs_d1(S, K, T, r, sigma)
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * bs_norm_cdf(-d2) - S * bs_norm_cdf(-d1)

def bs_call_delta(S, K, T, r, sigma):
    if T <= 1e-5:
        return 1.0 if S > K else 0.0
    return bs_norm_cdf(bs_d1(S, K, T, r, sigma))

def bs_put_delta(S, K, T, r, sigma):
    return bs_call_delta(S, K, T, r, sigma) - 1.0

def bs_vega(S, K, T, r, sigma):
    if T <= 1e-5 or sigma <= 1e-5:
        return 0.0
    d1 = bs_d1(S, K, T, r, sigma)
    return S * math.sqrt(T) * (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-0.5 * d1 ** 2)

def find_closest_delta_option(opts: list, target_delta: float, is_call: bool, spot_price: float, T: float, r: float, sigma: float) -> dict:
    """Scan live Derive option contracts for the chosen expiry and select the contract with the minimum absolute Delta error to the target Delta."""
    if not opts:
        raise ValueError("No option instruments available for expiry")
        
    def get_opt_delta(opt_item: dict) -> float:
        pricing = opt_item.get("option_pricing") or {}
        if "d" in pricing and pricing["d"] is not None:
            try:
                return float(pricing["d"])
            except (ValueError, TypeError):
                pass
                
        try:
            strike = float(opt_item.get("option_details", {}).get("strike", 0.0))
            if is_call:
                return bs_call_delta(spot_price, strike, T, r, sigma)
            else:
                return bs_put_delta(spot_price, strike, T, r, sigma)
        except Exception:
            return 0.0

    target_signed_delta = target_delta if is_call else -target_delta
    return min(opts, key=lambda opt: abs(get_opt_delta(opt) - target_signed_delta))

def get_asset_precision_and_min_size(symbol: str, inst_info: dict | None = None) -> tuple[int, float]:
    """Dynamically determine the size decimal precision and minimum order size based on asset type and exchange specs.
    
    Prevents rounding errors on high-value assets like BTC (4 decimals) vs ETH (3 decimals) vs HYPE (1 decimal).
    """
    sym = symbol.upper().split("-")[0]
    min_amount = None
    if inst_info:
        try:
            raw_min = inst_info.get("minimum_amount")
            if raw_min is not None:
                min_amount = float(raw_min)
        except (ValueError, TypeError):
            pass
            
    if sym == "BTC":
        min_size = min_amount or 0.01
        decimals = 4
    elif sym == "ETH":
        min_size = min_amount or 0.10
        decimals = 3
    elif sym == "SOL":
        min_size = min_amount or 0.10
        decimals = 2
    elif sym == "HYPE":
        min_size = min_amount or 1.00
        decimals = 1
    else:
        min_size = min_amount or 0.10
        decimals = 3
        
    return decimals, min_size

def has_position_state_materially_changed(current: dict, previous: dict, tolerance: float = 1e-4) -> bool:
    """Compare two position dictionaries with floating-point tolerance and zero-filtering.
    
    Prevents Telegram alert spam caused by minute float precision jitter (e.g. 0.30000000000000004 vs 0.3).
    """
    all_keys = set(current.keys()).union(set(previous.keys()))
    for k in all_keys:
        curr_amt = float(current.get(k, 0.0) or 0.0)
        prev_amt = float(previous.get(k, 0.0) or 0.0)
        if abs(curr_amt - prev_amt) > tolerance:
            return True
    return False

def parse_option_contract_details(instrument_name: str) -> tuple[float | None, str | None, float | None, float | None]:
    """Parse strike (K), option type ('C'/'P'), exact DTE (days), and exact T (years) directly from instrument name format (e.g. 'ETH-20260828-1950-C')."""
    try:
        parts = instrument_name.split("-")
        if len(parts) >= 4:
            exp_date_str = parts[1]
            strike = float(parts[2])
            opt_type = parts[3].upper()
            
            exp_dt = datetime.datetime.strptime(exp_date_str, "%Y%m%d").replace(hour=8, minute=0, second=0, tzinfo=datetime.timezone.utc)
            now_dt = datetime.datetime.now(datetime.timezone.utc)
            t_seconds = max(60.0, (exp_dt - now_dt).total_seconds())
            exact_dte = t_seconds / 86400.0
            T = t_seconds / (365.0 * 86400.0)
            return strike, opt_type, exact_dte, T
    except Exception as e:
        logger.warning(f"Error parsing option contract details for {instrument_name}: {e}")
    return None, None, None, None

def round_derive_strike(strike: float, spot: float, pair: str = "ETH") -> float:
    symbol = pair.upper().split("-")[0]
    if symbol == "BTC" or spot > 10000:
        increment = 1000.0
    elif symbol == "HYPE" or spot < 100:
        increment = 1.0
    else:
        increment = 50.0
    return round(strike / increment) * increment

def save_json_atomic(target_path: Path, data: dict):
    """Atomically write a JSON file using a temporary file and atomic replace to prevent corrupt reads by external watchdog or monitor processes."""
    temp_file = None
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_file = target_path.with_suffix(f".tmp.{os.getpid()}.{int(time.time()*1000)}")
        with open(temp_file, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, target_path)
    except Exception as e:
        logger.warning(f"Failed atomic write to {target_path}: {e}")
        if temp_file and temp_file.exists():
            try:
                temp_file.unlink()
            except Exception:
                pass

def load_previous_position_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
                if isinstance(state, dict):
                    for k, v in state.get("leg_marks", {}).items():
                        try:
                            val = float(v)
                            if val > 0.0:
                                LAST_KNOWN_MARK_PRICES.setdefault(k, val)
                        except (ValueError, TypeError):
                            pass
                    return state
        except Exception:
            pass
    return {}

def save_position_state(state: dict):
    save_json_atomic(STATE_FILE, state)

def save_heartbeat(data: dict):
    payload = {
        "status": "healthy",
        **data,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "timestamp_epoch": datetime.datetime.now(datetime.timezone.utc).timestamp(),
    }
    save_json_atomic(HEARTBEAT_FILE, payload)

def resolve_derive_credentials(config: Config) -> tuple[str, str, int | None]:
    """Resolve Derive credentials with PRIMARY source from hummingbot-api (encrypted connector files & GATEWAY_PASSPHRASE/CONFIG_PASSWORD) and SECONDARY fallback to root .env / environment variables.
    
    Priority Mapping:
    1. Explicit config overrides (if provided via UI or command args)
    2. hummingbot-api encrypted connector files (derive_perpetual.yml / derive.yml):
       - derive_perpetual_api_key / derive_api_key -> DERIVE_SMART_CONTRACT_WALLET
       - derive_perpetual_api_secret / derive_api_secret -> DERIVE_SESSION_KEY_PRIV
       - sub_id / subaccount_id -> DERIVE_SUBACCOUNT_ID
       - Decryption password: GATEWAY_PASSPHRASE -> CONFIG_PASSWORD from hummingbot-api/.env
    3. Root .env / OS environment variables fallback:
       - DERIVE_SMART_CONTRACT_WALLET / derive_perpetual_api_key / derive_api_key
       - DERIVE_SESSION_KEY_PRIV / derive_perpetual_api_secret / derive_api_secret
       - DERIVE_SUBACCOUNT_ID / sub_id / subaccount_id
       - GATEWAY_PASSPHRASE / CONFIG_PASSWORD
    """
    wallet = (config.smart_contract_wallet or "").strip()
    priv = (config.session_key_priv or "").strip()
    sub_id = config.subaccount_id

    # 1. Primary Source: Hummingbot-API connector files & hummingbot-api/.env
    if not (wallet and priv and sub_id is not None):
        import binascii
        import yaml
        from eth_account import Account

        hummingbot_base = Path(os.getenv("HUMMINGBOT_API_DIR", Path.home() / "hummingbot-api"))
        hb_env_file = hummingbot_base / ".env"
        
        # Resolve decryption password: Check hummingbot-api/.env first (GATEWAY_PASSPHRASE, then CONFIG_PASSWORD)
        pwd = None
        if hb_env_file.exists():
            try:
                from dotenv import dotenv_values
                hb_env = dotenv_values(hb_env_file)
                pwd = hb_env.get("GATEWAY_PASSPHRASE") or hb_env.get("CONFIG_PASSWORD")
            except Exception as e:
                logger.debug(f"Could not load hummingbot-api .env: {e}")
                
        # Fallback password from process environment or root .env
        if not pwd:
            pwd = os.getenv("GATEWAY_PASSPHRASE") or os.getenv("CONFIG_PASSWORD")

        connector_candidates = [
            hummingbot_base / "bots" / "credentials" / "master_account" / "connectors" / "derive_perpetual.yml",
            hummingbot_base / "bots" / "credentials" / "master_account" / "connectors" / "derive.yml",
            Path("/app/hummingbot-api/bots/credentials/master_account/connectors/derive_perpetual.yml"),
            Path("/app/hummingbot-api/bots/credentials/master_account/connectors/derive.yml"),
            Path("/workspace/hummingbot-api/bots/credentials/master_account/connectors/derive_perpetual.yml"),
            Path("/workspace/hummingbot-api/bots/credentials/master_account/connectors/derive.yml"),
        ]

        if pwd:
            for p in connector_candidates:
                if p.exists():
                    try:
                        with open(p, "r") as f:
                            raw = yaml.safe_load(f)
                        if isinstance(raw, dict):
                            dec = {}
                            for k, v in raw.items():
                                if k == "connector":
                                    continue
                                try:
                                    raw_json = binascii.unhexlify(v).decode("utf-8")
                                    dec[k] = Account.decrypt(raw_json, pwd).decode("utf-8")
                                except Exception:
                                    dec[k] = v
                                    
                            if not wallet:
                                wallet = (dec.get("derive_perpetual_api_key") or dec.get("derive_api_key") or "").strip()
                            if not priv:
                                priv = (dec.get("derive_perpetual_api_secret") or dec.get("derive_api_secret") or "").strip()
                            if sub_id is None and ("sub_id" in dec or "subaccount_id" in dec):
                                try:
                                    sub_id = int(dec.get("sub_id") or dec.get("subaccount_id"))
                                except (ValueError, TypeError):
                                    pass
                    except Exception as e:
                        logger.debug(f"Could not load credentials from {p}: {e}")
        else:
            logger.debug("GATEWAY_PASSPHRASE / CONFIG_PASSWORD not set in hummingbot-api/.env or environment; skipping connector decryption.")

    # 2. Secondary Fallback: Root .env / OS environment variables
    if not wallet:
        wallet = (os.getenv("DERIVE_SMART_CONTRACT_WALLET") or os.getenv("derive_perpetual_api_key") or os.getenv("derive_api_key") or "").strip()
    if not priv:
        priv = (os.getenv("DERIVE_SESSION_KEY_PRIV") or os.getenv("derive_perpetual_api_secret") or os.getenv("derive_api_secret") or "").strip()
    if sub_id is None:
        raw_env_sub = os.getenv("DERIVE_SUBACCOUNT_ID") or os.getenv("sub_id") or os.getenv("subaccount_id")
        if raw_env_sub:
            try:
                sub_id = int(str(raw_env_sub).strip())
            except ValueError:
                pass

    return wallet, priv, sub_id

def sanitize_telegram_chat_id(raw_chat_id: Any) -> str | None:
    """Validate and sanitize Telegram chat ID.
    
    Rejects strings containing ':' (e.g. bot tokens mistakenly passed as chat ID),
    and validates numeric chat IDs or @usernames.
    """
    if not raw_chat_id:
        return None
    val = str(raw_chat_id).strip().strip("\"'")
    if ":" in val:
        logger.warning(f"Rejecting invalid chat_id format '{val[:10]}...' (detected bot token instead of numeric chat ID)")
        return None
    try:
        int(val)
        return val
    except ValueError:
        if val.startswith("@") and len(val) > 1:
            return val
        return None

def resolve_target_telegram_chat_id(chat_id: str | None, context: Any = None) -> str | None:
    """Resolve destination Telegram chat ID with multi-level fallback:
    1. Explicit chat_id parameter (sanitized)
    2. Context runtime _chat_id / chat_id (from Condor user session)
    3. Environment variables: ADMIN_USER_ID -> TELEGRAM_CHAT_ID -> CONDOR_CHAT_ID
    """
    clean = sanitize_telegram_chat_id(chat_id)
    if clean:
        return clean
    
    if context is not None:
        for attr in ("_chat_id", "chat_id"):
            if hasattr(context, attr):
                clean = sanitize_telegram_chat_id(getattr(context, attr))
                if clean:
                    return clean
                    
    for env_key in ("ADMIN_USER_ID", "TELEGRAM_CHAT_ID", "CONDOR_CHAT_ID"):
        clean = sanitize_telegram_chat_id(os.environ.get(env_key))
        if clean:
            return clean
            
    return None

def resolve_target_telegram_bot_token(raw_candidate: str | None = None) -> str | None:
    """Resolve Telegram bot token with fallback from mistaken chat_id if applicable."""
    for env_key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", "CONDOR_TELEGRAM_BOT_TOKEN"):
        token = os.environ.get(env_key)
        if token and ":" in str(token).strip():
            return str(token).strip()
    if raw_candidate and ":" in str(raw_candidate).strip():
        return str(raw_candidate).strip()
    return None

async def send_telegram_alert(
    context: ContextTypes.DEFAULT_TYPE | None,
    chat_id: str | None,
    message: str,
    client: httpx.AsyncClient | None = None
) -> bool:
    """Dispatch real-time Telegram alert to the user with multi-channel redundancy and verification."""
    target_chat = resolve_target_telegram_chat_id(chat_id, context)
    if not target_chat:
        logger.info("Telegram alert skipped: No valid chat_id resolved (ADMIN_USER_ID / TELEGRAM_CHAT_ID not set or invalid)")
        return False

    bot_token = resolve_target_telegram_bot_token(chat_id)
    
    # 1. Try context.bot if available
    if context is not None and hasattr(context, "bot") and context.bot:
        try:
            res = await context.bot.send_message(chat_id=target_chat, text=message, parse_mode="Markdown")
            if isinstance(res, dict):
                if res.get("ok"):
                    logger.info(f"Telegram notification delivered to chat {target_chat} via context.bot")
                    return True
                else:
                    logger.warning(f"context.bot returned error ({res.get('description')}), retrying plain text...")
            else:
                logger.info(f"Telegram notification sent to chat {target_chat} via context.bot")
                return True
        except Exception as e:
            logger.warning(f"Failed to send Telegram alert via context.bot (Markdown): {e}. Retrying plain text...")
            
        try:
            res = await context.bot.send_message(chat_id=target_chat, text=message)
            if isinstance(res, dict):
                if res.get("ok"):
                    logger.info(f"Telegram notification delivered to chat {target_chat} as plain text fallback")
                    return True
            else:
                logger.info(f"Telegram notification delivered to chat {target_chat} as plain text fallback")
                return True
        except Exception as e2:
            logger.warning(f"Failed to send plain text alert via context.bot: {e2}")

    # 2. Direct HTTP fallback via Telegram Bot API
    if bot_token:
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload_md = {"chat_id": target_chat, "text": message, "parse_mode": "Markdown"}
            payload_plain = {"chat_id": target_chat, "text": message}

            async def _post_msg(c: httpx.AsyncClient) -> bool:
                r = await c.post(url, json=payload_md, timeout=5.0)
                if r.status_code == 200 and r.json().get("ok"):
                    return True
                r_plain = await c.post(url, json=payload_plain, timeout=5.0)
                if r_plain.status_code == 200 and r_plain.json().get("ok"):
                    return True
                logger.warning(f"Telegram API direct delivery failed: {r_plain.text}")
                return False

            if client:
                try:
                    ok = await _post_msg(client)
                    if ok:
                        logger.info(f"Telegram notification delivered to chat {target_chat} via async client")
                        return True
                except Exception as e_c:
                    logger.warning(f"Telegram alert via async client failed ({e_c}), retrying with temp client...")

            async with httpx.AsyncClient(timeout=5.0) as temp_client:
                ok = await _post_msg(temp_client)
                if ok:
                    logger.info(f"Telegram notification delivered to chat {target_chat} via direct HTTP")
                    return True
        except Exception as e:
            logger.warning(f"Failed to send Telegram alert via async HTTP fallback: {e}")
    else:
        logger.info("Telegram direct alert skipped: No TELEGRAM_TOKEN or TELEGRAM_BOT_TOKEN set in .env")

    return False

_ERROR_ALERT_HISTORY: dict[str, float] = {}

async def send_telegram_error_alert(
    context: ContextTypes.DEFAULT_TYPE | None,
    chat_id: str | None,
    error_title: str,
    error: Exception | str,
    cycle_number: int | str,
    cycle_type: str,
    pair: str,
    client: httpx.AsyncClient | None = None,
    extra_info: str | None = None,
    throttle_seconds: float = 60.0,
):
    """Send immediate automated Telegram alert for unhandled errors/exceptions or crashes with error summary, cycle number, and timestamp.
    
    Includes intelligent throttling for repeating errors to prevent flood while ensuring the first occurrence and distinct errors alert immediately.
    """
    now = time.time()
    err_cls = type(error).__name__ if isinstance(error, Exception) else "Error"
    err_str = str(error) if error else "Unknown error"
    error_key = f"{cycle_type}:{err_cls}:{err_str[:80]}"
    
    if throttle_seconds > 0:
        last_alert_time = _ERROR_ALERT_HISTORY.get(error_key, 0.0)
        if (now - last_alert_time) < throttle_seconds:
            logger.info(f"Telegram error alert throttled ({now - last_alert_time:.1f}s < {throttle_seconds:.1f}s): {error_key}")
            return

    _ERROR_ALERT_HISTORY[error_key] = now
    timestamp_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    
    if len(err_str) > 500:
        err_str = err_str[:497] + "..."
    clean_err_str = err_str.replace("`", "'")

    error_alert_msg = (
        f"🚨 *[{error_title}]* 🚨\n"
        f"*Asset*: `{pair}` | *Time*: `{timestamp_str}`\n"
        f"*Cycle*: `{cycle_type} #{cycle_number}`\n"
        f"*Error Type*: `{err_cls}`\n"
        f"*Error Details*: `{clean_err_str}`\n"
    )
    if extra_info:
        error_alert_msg += f"• *Status*: {extra_info}\n"
    else:
        error_alert_msg += "• *Status*: Exception caught; routine remains resilient and continues.\n"

    try:
        await send_telegram_alert(context, chat_id, error_alert_msg, client)
    except Exception as ex:
        logger.error(f"send_telegram_error_alert failed to dispatch: {ex}")

async def fetch_derivatives_monkey_data(symbol: str = "ETH") -> dict:
    """Fetch live market intelligence and strategy signal from derivatives_monkey_extractor.py non-blockingly."""
    extractor_path = ROUTINES_DIR.parent / "derivatives_monkey_extractor.py"
    if not extractor_path.exists():
        logger.warning(f"Derivatives Monkey extractor not found at {extractor_path}")
        return {}
        
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(extractor_path), "--asset", symbol, "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=8.0)
        if proc.returncode == 0 and stdout:
            return json.loads(stdout.decode("utf-8"))
        else:
            logger.warning(f"Derivatives Monkey extractor non-zero exit ({proc.returncode}): {stderr.decode('utf-8')}")
    except Exception as e:
        logger.warning(f"Failed to fetch Derivatives Monkey signals: {e}")
    return {}

async def fetch_robust_spot_price(client: httpx.AsyncClient, symbol: str = "ETH") -> float:
    """Fetch live spot price non-blockingly across 4 independent venues. If all fail, raises RuntimeError to prevent static fallback trading."""
    # 1. Derive Perpetual Index/Mark Price
    try:
        r = (await client.post("https://api.lyra.finance/public/get_ticker", json={"instrument_name": f"{symbol}-PERP"}, timeout=3.5)).json().get("result", {})
        price = float(r.get("index_price") or r.get("mark_price") or 0.0)
        if price > 0:
            return round(price, 2)
    except Exception as e:
        logger.warning(f"Derive live spot fetch failed: {e}")

    # 2. Binance Public Ticker
    try:
        r = (await client.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}USDT", timeout=3.5)).json()
        price = float(r.get("price") or 0.0)
        if price > 0:
            return round(price, 2)
    except Exception as e:
        logger.warning(f"Binance live spot fetch failed: {e}")

    # 3. Hyperliquid AllMids
    try:
        r = (await client.post("https://api.hyperliquid.xyz/info", json={"type": "allMids"}, timeout=3.5)).json()
        if symbol in r:
            price = float(r[symbol])
            if price > 0:
                return round(price, 2)
    except Exception as e:
        logger.warning(f"Hyperliquid live spot fetch failed: {e}")

    # 4. Coinbase Spot
    try:
        r = (await client.get(f"https://api.coinbase.com/v2/prices/{symbol}-USD/spot", timeout=3.5)).json().get("data", {})
        price = float(r.get("amount") or 0.0)
        if price > 0:
            return round(price, 2)
    except Exception as e:
        logger.warning(f"Coinbase live spot fetch failed: {e}")

    raise RuntimeError(f"CRITICAL: Failed to fetch live spot price for {symbol} across all 4 data venues (Derive, Binance, Hyperliquid, Coinbase). Skipping cycle to prevent execution on invalid pricing.")

async def fetch_dynamic_market_volatility(client: httpx.AsyncClient, pair: str = "ETH-USDT", target_dte: int = 14) -> tuple[float, float, float, float, float, float, str, str]:
    """Dynamically fetch live multi-venue spot, 7D RV, ATM IV, aggregate Dealer GEX ($M), DEX ($M), and 25D IV Skew non-blockingly."""
    symbol = pair.upper().split("-")[0]
    
    # 1. Multi-venue Non-Blocking Robust Spot Price
    spot_price = await fetch_robust_spot_price(client, symbol)
    
    # 2. Dynamic 7D Realized Volatility from Live Hourly Candles
    rv_7d = 25.0
    try:
        binance_sym = f"{symbol}USDT"
        k_resp = (await client.get(f"https://api.binance.com/api/v3/klines?symbol={binance_sym}&interval=1h&limit=168", timeout=4.0)).json()
        closes = [float(k[4]) for k in k_resp if len(k) > 4]
        if len(closes) >= 24:
            returns = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
            mean_r = sum(returns) / len(returns)
            var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
            rv_7d = round(math.sqrt(var_r * 24.0 * 365.0) * 100.0, 2)
    except Exception as e:
        logger.warning(f"Binance candle fetch error: {e}. Falling back to Hyperliquid...")
        try:
            hl_resp = (await client.post("https://api.hyperliquid.xyz/info", json={"type": "candleSnapshot", "req": {"coin": symbol, "interval": "1h", "startTime": int((datetime.datetime.now(datetime.timezone.utc).timestamp() - 7*86400)*1000)}}, timeout=4.0)).json()
            closes = [float(c["c"]) for c in hl_resp if "c" in c]
            if len(closes) >= 24:
                returns = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
                mean_r = sum(returns) / len(returns)
                var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
                rv_7d = round(math.sqrt(var_r * 24.0 * 365.0) * 100.0, 2)
        except Exception as e2:
            logger.warning(f"Hyperliquid candle fallback error: {e2}")

    # 3. Live Option Surface Aggregation: ATM IV, Real Dealer GEX ($M), Real DEX ($M), and Real 25D IV Skew
    iv_14d = rv_7d + 10.0
    dealer_gex_m = 0.0
    dealer_dex_m = 0.0
    iv_skew_pts = 0.0
    block_rfq_bias = "BALANCED_TWO_WAY_FLOW"
    
    try:
        inst_resp = (await client.post("https://api.lyra.finance/public/get_instruments", json={"currency": symbol, "instrument_type": "option", "expired": False}, timeout=4.5)).json().get("result", [])
        now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
        
        valid_expiries = sorted(list(set(
            i.get("option_details", {}).get("expiry", 0)
            for i in inst_resp
            if (i.get("option_details", {}).get("expiry", 0) - now_ts) >= 3 * 86400
        )))
        
        if valid_expiries:
            target_expiry_ts = now_ts + (target_dte * 86400)
            chosen_expiry = min(valid_expiries, key=lambda exp: abs(exp - target_expiry_ts))
            exp_date_str = datetime.datetime.fromtimestamp(chosen_expiry, datetime.timezone.utc).strftime("%Y%m%d")
            
            # Fetch full option tickers and Greeks for the target expiry non-blockingly
            t_resp = (await client.post("https://api.lyra.finance/public/get_tickers", json={"currency": symbol, "instrument_type": "option", "expired": False, "expiry_date": exp_date_str}, timeout=5.0)).json()
            tickers = t_resp.get("result", {}).get("tickers", {})
            
            total_call_gex = 0.0
            total_put_gex = 0.0
            total_dex = 0.0
            calls_25d = []
            puts_25d = []
            atm_call_iv = None
            closest_atm_diff = 999999.0
            
            for name, t in tickers.items():
                parts = name.split("-")
                if len(parts) < 4:
                    continue
                opt_type = parts[3]
                k = float(parts[2])
                op = t.get("option_pricing") or {}
                stats = t.get("stats") or {}
                delta = float(op.get("d") or 0.0)
                gamma = float(op.get("g") or 0.0)
                iv = float(op.get("i") or 0.0)
                oi = float(stats.get("oi") or 0.0)
                
                dollar_gex = oi * gamma * (spot_price ** 2) * 0.01 / 1e6
                dollar_dex = oi * delta * spot_price / 1e6
                total_dex += dollar_dex
                
                if opt_type == "C":
                    total_call_gex += dollar_gex
                    if 0.15 <= delta <= 0.35:
                        calls_25d.append(iv)
                    diff = abs(k - spot_price)
                    if diff < closest_atm_diff and iv > 0.05:
                        closest_atm_diff = diff
                        atm_call_iv = iv * 100.0
                else:
                    total_put_gex += dollar_gex
                    if -0.35 <= delta <= -0.15:
                        puts_25d.append(iv)
                        
            dealer_gex_m = round(total_call_gex - total_put_gex, 4)
            dealer_dex_m = round(total_dex, 4)
            
            avg_put_iv = (sum(puts_25d) / len(puts_25d) * 100.0) if puts_25d else (rv_7d + 12.0)
            avg_call_iv = (sum(calls_25d) / len(calls_25d) * 100.0) if calls_25d else (rv_7d + 10.0)
            iv_skew_pts = round(avg_put_iv - avg_call_iv, 2)
            
            if atm_call_iv and atm_call_iv > 10.0:
                iv_14d = round(atm_call_iv, 2)
            else:
                iv_14d = round((avg_put_iv + avg_call_iv) / 2.0, 2)
                
            if iv_skew_pts > 3.0:
                block_rfq_bias = "INSTITUTIONAL_DOWNSIDE_PUT_DEMAND"
            elif iv_skew_pts < -3.0:
                block_rfq_bias = "INSTITUTIONAL_UPSIDE_CALL_DEMAND"
            else:
                block_rfq_bias = "INSTITUTIONAL_VOL_SELLING"
    except Exception as e:
        logger.warning(f"Derive surface metrics calculation error: {e}")

    gex_conviction = "POSITIVE GEX (VOLATILITY DAMPENING REGIME)" if dealer_gex_m >= 0.0 else "NEGATIVE GEX (VOLATILITY AMPLIFYING REGIME)"
    
    return spot_price, iv_14d, rv_7d, dealer_gex_m, dealer_dex_m, iv_skew_pts, block_rfq_bias, gex_conviction

def map_rfq_quote_prices(rfq_legs: list, quote_legs: list) -> bool:
    """Map quote execution prices explicitly by instrument_name or asset_address+sub_id.
    
    Prevents assigning wrong prices if maker quote leg array order diverges from request order.
    """
    price_by_name = {}
    price_by_addr_sub = {}
    
    for q in quote_legs:
        iname = q.get("instrument_name")
        p = q.get("price")
        if iname and p is not None:
            price_by_name[iname] = Decimal(str(p))
            
        addr = (q.get("asset_address") or "").lower()
        sub = int(q.get("sub_id", 0))
        if addr and p is not None:
            price_by_addr_sub[(addr, sub)] = Decimal(str(p))
            
    all_matched = True
    for leg in rfq_legs:
        if leg.instrument_name in price_by_name:
            leg.price = price_by_name[leg.instrument_name]
        else:
            addr_key = (getattr(leg, "asset_address", "").lower(), int(getattr(leg, "sub_id", 0)))
            if addr_key in price_by_addr_sub:
                leg.price = price_by_addr_sub[addr_key]
            else:
                logger.error(f"Failed to match quote price for leg: {leg.instrument_name}")
                all_matched = False
                
    return all_matched

async def poll_best_rfq_quote(
    client: httpx.AsyncClient, 
    subaccount_id: int, 
    auth_headers: dict, 
    rfq_id: str, 
    rfq_legs: list,
    mode: str = "entry", 
    max_wait_seconds: int = 5,
    max_acceptable_cost: float | None = None
) -> dict | None:
    """Poll Derive RFQ quotes across multiple ticks over a multi-second window and select the best profit quote:
    - Entry (Selling Spread): Highest Net Credit Received
    - Exit (Buying Spread to Close): Lowest Cost to Close (capped at max_acceptable_cost)
    """
    user_dir_map = {}
    for l in rfq_legs:
        iname = getattr(l, "instrument_name", None) or (l.get("instrument_name") if isinstance(l, dict) else None)
        d = getattr(l, "direction", None) or (l.get("direction") if isinstance(l, dict) else None)
        if iname and d:
            user_dir_map[iname] = d
            
    def eval_quote_net(quote: dict) -> float:
        net_val = 0.0
        for leg in quote.get("legs", []):
            iname = leg.get("instrument_name")
            p = float(leg.get("price", 0.0))
            amt = float(leg.get("amount", 1.0))
            u_dir = user_dir_map.get(iname, "buy")
            if u_dir == "sell":
                net_val += p * amt  # Taker receives credit
            else:
                net_val -= p * amt  # Taker pays debit
        return net_val

    best_quote = None
    best_score = -999999.0 if mode == "entry" else 999999.0

    for tick in range(max_wait_seconds):
        await asyncio.sleep(1.0)
        try:
            poll_resp = await client.post(
                "https://api.lyra.finance/private/poll_quotes",
                json={"subaccount_id": subaccount_id, "status": "open"},
                headers=auth_headers,
                timeout=4.0
            )
            quotes = poll_resp.json().get("result", {}).get("quotes", [])
            matching = [q for q in quotes if q.get("rfq_id") == rfq_id and q.get("direction") == "sell"]
            
            if matching:
                for q in matching:
                    net_p = eval_quote_net(q)
                    if mode == "entry":
                        # Highest net credit is best
                        if net_p > best_score:
                            best_score = net_p
                            best_quote = q
                    else:
                        # Lowest cost to close is best (cost = -net_p)
                        cost = -net_p
                        if max_acceptable_cost is not None and cost > max_acceptable_cost:
                            logger.info(f"Skipping RFQ exit quote exceeding max cost cap: ${cost:.2f} > ${max_acceptable_cost:.2f}")
                            continue
                        if cost < best_score:
                            best_score = cost
                            best_quote = q
                            
                # Once we have gathered competing quotes across 2+ ticks, return winner
                if tick >= 2 and best_quote:
                    break
        except Exception as e:
            logger.warning(f"Error during RFQ poll tick {tick+1}/{max_wait_seconds}: {e}")

    return best_quote

async def resolve_actual_entry_credit(
    client: httpx.AsyncClient, 
    subaccount_id: int, 
    auth_headers: dict, 
    active_legs: list, 
    fallback_spread_unit_credit: float
) -> float:
    """Resolve the true net entry credit received when opening the active Iron Condor position.
    
    1. Primary Source: Exact weighted average_price recorded by Derive clearinghouse for every active leg.
    2. Secondary Source: Live RFQ package trade history (page_size 100).
    3. Fallback: Theoretical spread credit multiplied by active short contract size.
    """
    if not active_legs:
        return 0.0

    # 1. Primary Source: Derive Clearinghouse average_price_excl_fees from get_subaccount
    canonical_credit = 0.0
    has_avg_prices = False
    for p in active_legs:
        amt = float(p.get("amount", 0.0))
        avg_px = float(p.get("average_price_excl_fees", 0.0) or p.get("average_price", 0.0) or 0.0)
        if avg_px > 0.0:
            has_avg_prices = True
            if amt < 0:
                canonical_credit += abs(amt) * avg_px  # Short leg credit received
            elif amt > 0:
                canonical_credit -= abs(amt) * avg_px  # Long leg debit paid
                
    if has_avg_prices and canonical_credit > 0.0:
        return round(canonical_credit, 2)

    # 2. Secondary Source: Query live trade history (page_size 100)
    if auth_headers and subaccount_id:
        try:
            trades_res = (await client.post(
                "https://api.lyra.finance/private/get_trade_history",
                json={"subaccount_id": subaccount_id, "page_size": 100},
                headers=auth_headers,
                timeout=5.0
            )).json().get("result", {}).get("trades", [])
            
            active_inames = {p.get("instrument_name") for p in active_legs}
            
            by_rfq = {}
            for t in trades_res:
                rfq = t.get("rfq_id")
                if rfq and "IC-PACKAGE" in t.get("label", ""):
                    by_rfq.setdefault(rfq, []).append(t)
                    
            total_open_rfq_credit = 0.0
            for rfq, tlist in by_rfq.items():
                package_inames = {t.get("instrument_name") for t in tlist}
                if package_inames.issubset(active_inames) or active_inames.issubset(package_inames):
                    net_rfq_credit = 0.0
                    for t in tlist:
                        p = float(t.get("trade_price") or 0.0)
                        amt = float(t.get("trade_amount") or 0.0)
                        d = t.get("direction")
                        if d == "sell":
                            net_rfq_credit += p * amt
                        elif d == "buy":
                            net_rfq_credit -= p * amt
                    if net_rfq_credit > 0.0:
                        total_open_rfq_credit += net_rfq_credit
                        
            if total_open_rfq_credit > 0.0:
                return round(total_open_rfq_credit, 2)
        except Exception as e:
            logger.warning(f"Error querying live trade history for entry credit: {e}")

    # 3. Fallback: Theoretical spread credit * short contract size
    short_contracts = max(abs(float(p.get("amount", 0.0))) for p in active_legs if float(p.get("amount", 0.0)) < 0) if any(float(p.get("amount", 0.0)) < 0 for p in active_legs) else 1.0
    return round(fallback_spread_unit_credit * short_contracts, 2)

async def execute_perp_rebalance(
    config: Config,
    http_client: httpx.AsyncClient,
    symbol: str,
    spot_price: float,
    live_options_delta: float,
    live_perp_delta: float,
    auth_headers: dict,
    session_key_wallet,
    smart_contract_wallet: str,
    subaccount_id: int,
    inst_perp_info: dict,
    size_decimals: int,
    min_order_size: float,
    context: ContextTypes.DEFAULT_TYPE | None = None
) -> tuple[float, str, str, bool]:
    """Execute fast perpetual delta rebalance using passive Maker limit orders (0.01% offset with Post-Only and 30s cancellation of unfilled orders)."""
    from derive_action_signing import SignedAction, TradeModuleData, utils
    TRADE_MODULE_ADDRESS = "0xB8D20c2B7a1Ad2EE33Bc50eF10876eD3035b5e7b"
    DOMAIN_SEPARATOR = "0xd96e5f90797da7ec8dc4e276260c7f3f87fedf68775fbe1ef116e996fc60441b"
    ACTION_TYPEHASH = "0x4d7a9f27c403ff9c0f19bce61d76d82f9aa29f8d6d4b0c5474607d9770d1af17"
    
    perp_symbol = f"{symbol}-PERP"
    execution_mode_str = "[PAPER MODE - DRY RUN]" if config.paper_mode else "[LIVE ORDER EXECUTION]"
    target_perp_delta = - live_options_delta
    max_allowed_perp_delta = max(min_order_size, abs(live_options_delta) * config.max_perp_delta_hedge_ratio)
    
    delta_imbalance = target_perp_delta - live_perp_delta
    hedge_action_status = f"DELTA NEUTRAL (|Imbalance| {abs(delta_imbalance):.4f} <= {config.hedge_band:.2f})"
    perp_order_summary = "None (Aligned with Options Delta Hedge Cap)"
    rebalanced = False

    if spot_price <= 0.0:
        logger.warning(f"execute_perp_rebalance: Invalid spot price (${spot_price}); skipping rebalance.")
        return live_perp_delta, hedge_action_status, perp_order_summary, False
    
    # 0. Clean Cancellation: Cancel any existing open resting orders on perp_symbol before evaluating / placing new maker orders
    if not config.paper_mode and auth_headers and subaccount_id:
        try:
            await http_client.post(
                "https://api.lyra.finance/private/cancel_all",
                json={"subaccount_id": subaccount_id, "instrument_name": perp_symbol},
                headers=auth_headers,
                timeout=4.0
            )
        except Exception as e_can:
            logger.debug(f"Cancel existing perp open orders failed/skipped: {e_can}")
    
    is_overhedged = live_perp_delta > (max_allowed_perp_delta + 0.05) or live_perp_delta < (-max_allowed_perp_delta - 0.05)
    maker_offset = 0.0001  # 0.01% (1 basis point offset from mark price for maker posting)
    
    if is_overhedged or abs(delta_imbalance) > config.hedge_band:
        rebalance_size = round(abs(delta_imbalance), size_decimals)
        if rebalance_size >= min_order_size:
            is_sell = delta_imbalance < 0
            side_str = "SELL" if is_sell else "BUY"
            # Maker price: 0.01% above mark for SELL, 0.01% below mark for BUY
            maker_price = round(spot_price * (1.0 + maker_offset) if is_sell else spot_price * (1.0 - maker_offset), 2)
            
            hedge_action_status = f"PERPETUAL MAKER REBALANCE ({side_str} {rebalance_size:.{size_decimals}f} {perp_symbol} @ ${maker_price:,.2f})"
            perp_order_summary = f"{execution_mode_str} Submit MAKER {side_str} {rebalance_size:.{size_decimals}f} {perp_symbol} @ ${maker_price:,.2f} (Target: {target_perp_delta:+.4f} {symbol})"
            
            if not config.paper_mode:
                if not (session_key_wallet and smart_contract_wallet and subaccount_id):
                    perp_order_summary += " | Skipped (Credentials missing in .env)"
                else:
                    try:
                        trade_data = TradeModuleData(
                            asset_address=inst_perp_info.get("base_asset_address", "0x0000000000000000000000000000000000000000"),
                            sub_id=int(inst_perp_info.get("base_asset_sub_id", 0)),
                            limit_price=Decimal(str(maker_price)),
                            amount=Decimal(str(rebalance_size)),
                            max_fee=Decimal("100"),
                            recipient_id=subaccount_id,
                            is_bid=not is_sell
                        )
                        action = SignedAction(
                            subaccount_id=subaccount_id,
                            owner=smart_contract_wallet,
                            signer=session_key_wallet.address,
                            signature_expiry_sec=utils.MAX_INT_32,
                            nonce=utils.get_action_nonce(),
                            module_address=TRADE_MODULE_ADDRESS,
                            module_data=trade_data,
                            DOMAIN_SEPARATOR=DOMAIN_SEPARATOR,
                            ACTION_TYPEHASH=ACTION_TYPEHASH,
                        )
                        action.sign(session_key_wallet.key)
                        
                        order_resp = await http_client.post(
                            "https://api.lyra.finance/private/order",
                            json={
                                **action.to_json(),
                                "instrument_name": perp_symbol,
                                "direction": "sell" if is_sell else "buy",
                                "order_type": "limit",
                                "post_only": True,
                                "reduce_only": is_overhedged,
                                "time_in_force": "gtc",
                                "label": "fast-rebalance-perp-maker"
                            },
                            headers=auth_headers,
                            timeout=5.0
                        )
                        order_json = order_resp.json()
                        res_order = order_json.get("result", {})
                        order_obj = res_order.get("order", {})
                        ord_status = order_obj.get("order_status")
                        
                        if ord_status == "filled":
                            avg_px = float(order_obj.get("average_price") or maker_price)
                            perp_order_summary += f" | MAKER FILLED @ ${avg_px:,.2f}"
                            live_perp_delta += (-rebalance_size if is_sell else rebalance_size)
                            rebalanced = True
                        elif ord_status == "open":
                            perp_order_summary += f" | MAKER POSTED @ ${maker_price:,.2f} (Resting GTC | Post-Only)"
                            rebalanced = True
                        elif "error" in order_json:
                            err_info = order_json.get("error", {})
                            err_msg = err_info.get("message", str(err_info)) if isinstance(err_info, dict) else str(err_info)
                            perp_order_summary += f" | Rejected: {err_msg}"
                            logger.warning(f"Perp maker order rejected by Derive: {err_msg}")
                        else:
                            perp_order_summary += f" | Order Status: {ord_status or 'Submitted'}"
                            rebalanced = True
                    except Exception as e:
                        logger.error(f"Perp maker rebalance execution error: {e}", exc_info=True)
                        perp_order_summary += f" | Error: {e}"
                        if not config.paper_mode:
                            await send_telegram_error_alert(
                                context=context,
                                chat_id=config.telegram_chat_id,
                                error_title="PERPETUAL MAKER REBALANCE ERROR",
                                error=e,
                                cycle_number="Maker Rebalance Execution",
                                cycle_type="Perp Order",
                                pair=config.trading_pair,
                                client=http_client,
                                extra_info=f"Perpetual maker rebalance order failed for {perp_symbol} ({side_str} {rebalance_size} {symbol} @ ${maker_price})",
                                throttle_seconds=30.0
                            )

    return live_perp_delta, hedge_action_status, perp_order_summary, rebalanced

async def execute_cycle(config: Config, context: ContextTypes.DEFAULT_TYPE, http_client: httpx.AsyncClient) -> tuple[dict, list, list, str, list]:
    """Execute a single complete macro monitoring, Derivatives Monkey consensus cross-check, pure target delta strike selection, real GEX surface analysis, gradual scale-in, exact DTE Greeks, adaptive precision rounding, take-profit evaluation, and execution cycle non-blockingly."""
    timestamp_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    symbol = config.trading_pair.upper().split("-")[0]
    perp_symbol = f"{symbol}-PERP"
    execution_mode_str = "[PAPER MODE - DRY RUN]" if config.paper_mode else "[LIVE ORDER EXECUTION]"
    strategy_operating_mode = "AUTONOMOUS (ENTRY + HEDGE)" if config.enable_options_entry else "HEDGE-ONLY (NEW ENTRY DISABLED)"
    
    # -------------------------------------------------------------
    # 1. Multi-Venue Spot, Dynamic RV, ATM IV & Real Surface GEX/DEX/Skew
    # -------------------------------------------------------------
    spot_price, iv_14d, rv_7d, dealer_gex_m, dealer_dex_m, iv_skew_pts, block_rfq_bias, gex_conviction = await fetch_dynamic_market_volatility(http_client, config.trading_pair, config.dte)
    
    # -------------------------------------------------------------
    # 1B. Derivatives Monkey Live Intelligence & Signal Extraction
    # -------------------------------------------------------------
    dm_data = await fetch_derivatives_monkey_data(symbol)
    dm_signal = dm_data.get("signal", {})
    dm_short_vol_allowed = bool(dm_signal.get("short_vol_iron_condor_allowed", False))
    dm_strategy = dm_signal.get("recommended_strategy", "HOLD_NEUTRAL")
    dm_confidence = int(dm_signal.get("confidence_score", 0))
    dm_reasons = dm_signal.get("reasons", [])
    
    dm_vrp = dm_data.get("volatility_edge", {}).get("vrp")
    dm_iv_rank = dm_data.get("volatility_edge", {}).get("iv_rank")
    dm_vol_regime = str(dm_data.get("volatility_edge", {}).get("vol_regime", "N/A"))
    dm_gex_regime = str(dm_data.get("gamma_exposure", {}).get("gex_regime", "N/A"))
    dm_flip_strike = dm_data.get("gamma_exposure", {}).get("gamma_flip_level")
    dm_spot_above_flip = dm_data.get("gamma_exposure", {}).get("spot_above_flip", False)
    dm_term_tag = str(dm_data.get("term_structure", {}).get("term_tag", "N/A"))
    
    r = 0.03
    raw_vol_premium = iv_14d - rv_7d
    friction_cost = 2.50
    net_edge = raw_vol_premium - friction_cost
    local_edge_open = net_edge >= config.min_edge

    # Cross-Check Signal Consensus (Derivatives Monkey takes precedence)
    signal_conflict = False
    conflict_reason = ""
    
    if local_edge_open and not dm_short_vol_allowed:
        signal_conflict = True
        conflict_reason = f"Derivatives Monkey blocks entry ({dm_strategy} | Conf: {dm_confidence}% | GEX: {dm_gex_regime} | Term: {dm_term_tag})"
    elif not local_edge_open and dm_short_vol_allowed:
        signal_conflict = True
        conflict_reason = f"Local Net Edge ({net_edge:.2f} pts) below threshold ({config.min_edge:.1f} pts)"
        
    edge_open = local_edge_open and dm_short_vol_allowed and (dm_confidence >= 65)
    
    if signal_conflict:
        signal_status = f"BLOCKED BY CONFLICT: {conflict_reason}"
        regime_mode = f"Standby ({conflict_reason})"
        consensus_str = f"CONFLICT (ENTRY BLOCKED: {conflict_reason})"
    elif edge_open:
        signal_status = f"ACTIVE (CONSENSUS REACHED: Both Engines Confirm Short Vol | DM Conf: {dm_confidence}%)"
        regime_mode = f"Short Volatility ({dm_strategy})"
        consensus_str = f"ALIGNED (DUAL CONFIRMATION | Conf: {dm_confidence}%)"
    else:
        signal_status = "INACTIVE (EDGE / CONVICTION BELOW THRESHOLD)"
        regime_mode = "Neutral / Standby"
        consensus_str = "STANDBY (EDGE INSUFFICIENT)"
    
    sigma = iv_14d / 100.0
    T_entry = config.dte / 365.0
    
    # -------------------------------------------------------------
    # Pure Target Delta Strike Resolution (Option B: 30 Delta Short Body & 10 Delta Long Wings)
    # -------------------------------------------------------------
    raw_k_sc = strike_from_delta(spot_price, T_entry, r, sigma, config.short_delta_target, is_call=True)
    raw_k_lc = strike_from_delta(spot_price, T_entry, r, sigma, config.wing_delta_target, is_call=True)
    raw_k_sp = strike_from_delta(spot_price, T_entry, r, sigma, config.short_delta_target, is_call=False)
    raw_k_lp = strike_from_delta(spot_price, T_entry, r, sigma, config.wing_delta_target, is_call=False)
    
    k_short_put = round_derive_strike(raw_k_sp, spot_price, config.trading_pair)
    k_short_call = round_derive_strike(raw_k_sc, spot_price, config.trading_pair)
    k_long_put = round_derive_strike(raw_k_lp, spot_price, config.trading_pair)
    k_long_call = round_derive_strike(raw_k_lc, spot_price, config.trading_pair)
    
    c_sc = bs_call_price(spot_price, k_short_call, T_entry, r, sigma)
    p_sp = bs_put_price(spot_price, k_short_put, T_entry, r, sigma)
    c_lc = bs_call_price(spot_price, k_long_call, T_entry, r, sigma)
    p_lp = bs_put_price(spot_price, k_long_put, T_entry, r, sigma)
    theoretical_net_unit_credit = round(max(5.0, (c_sc + p_sp - c_lc - p_lp)), 2)
    contracts = float(config.contract_size)
    total_credit = round(theoretical_net_unit_credit * contracts, 2)
    
    from web3 import Web3
    from eth_account.messages import encode_defunct
    from derive_action_signing import SignedAction, RFQQuoteDetails, RFQExecuteModuleData, TradeModuleData, utils

    # -------------------------------------------------------------
    # 2. Derive Credentials & Subaccount Resolution (hummingbot-api primary)
    # -------------------------------------------------------------
    smart_contract_wallet, session_key_priv, subaccount_id = resolve_derive_credentials(config)
    
    DOMAIN_SEPARATOR = "0xd96e5f90797da7ec8dc4e276260c7f3f87fedf68775fbe1ef116e996fc60441b"
    ACTION_TYPEHASH = "0x4d7a9f27c403ff9c0f19bce61d76d82f9aa29f8d6d4b0c5474607d9770d1af17"
    RFQ_MODULE_ADDRESS = "0x9371352CCef6f5b36EfDFE90942fFE622Ab77F1D"
    TRADE_MODULE_ADDRESS = "0xB8D20c2B7a1Ad2EE33Bc50eF10876eD3035b5e7b"
    
    auth_headers = {}
    session_key_wallet = None
    if session_key_priv and smart_contract_wallet:
        try:
            web3_client = Web3()
            session_key_wallet = web3_client.eth.account.from_key(session_key_priv)
            timestamp_str_ms = str(int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000))
            sig_obj = web3_client.eth.account.sign_message(encode_defunct(text=timestamp_str_ms), private_key=session_key_priv)
            sig_hex = "0x" + (sig_obj.signature.hex() if hasattr(sig_obj.signature, "hex") else str(sig_obj.signature))
            
            auth_headers = {
                "accept": "application/json",
                "content-type": "application/json",
                "X-LyraWallet": smart_contract_wallet,
                "X-LyraTimestamp": timestamp_str_ms,
                "X-LyraSignature": sig_hex,
            }
        except Exception as e:
            logger.warning(f"Failed to initialize Derive session key signature: {e}")
    elif not config.paper_mode:
        logger.warning("Derive credentials not fully resolved.")
    
    # -------------------------------------------------------------
    # 3. Dynamic Asset Precision Resolution from Exchange Specs
    # -------------------------------------------------------------
    inst_perp_info = {}
    try:
        inst_perp_resp = (await http_client.post("https://api.lyra.finance/public/get_instrument", json={"instrument_name": perp_symbol}, timeout=4.0)).json()
        inst_perp_info = inst_perp_resp.get("result", {})
    except Exception:
        pass
    size_decimals, min_order_size = get_asset_precision_and_min_size(symbol, inst_perp_info)

    # -------------------------------------------------------------
    # 4. Live Derive Subaccount & Exact Contract-Specific Greeks Discovery
    # -------------------------------------------------------------
    collateral = 0.0
    subaccount_value = 0.0
    positions_value = 0.0
    buying_power = 0.0
    positions_margin_used = 0.0
    margin_utilization_pct = 0.0
    margin_headroom_usd = 0.0
    live_options_delta = 0.0
    live_perp_delta = 0.0
    active_options_count = 0
    current_positions_summary = {}
    live_active_positions_table = []
    active_options_legs_raw = []
    subaccount_queried_successfully = False

    if auth_headers and subaccount_id:
        try:
            sub_resp = await http_client.post(
                "https://api.lyra.finance/private/get_subaccount",
                json={"subaccount_id": subaccount_id},
                headers=auth_headers,
                timeout=6.0
            )
            if sub_resp.status_code == 200:
                sub_res = sub_resp.json().get("result", {})
                subaccount_id = sub_res.get("subaccount_id", subaccount_id)
                collateral = round(float(sub_res.get("collaterals_value", 0.0)), 2)
                subaccount_value = round(float(sub_res.get("subaccount_value", collateral)), 2)
                positions_value = round(float(sub_res.get("positions_value", 0.0)), 2)
                
                collaterals_im = float(sub_res.get("collaterals_initial_margin", 0.0))
                positions_im = abs(float(sub_res.get("positions_initial_margin", 0.0)))
                positions_margin_used = round(positions_im, 2)
                buying_power = round(max(0.0, float(sub_res.get("initial_margin", 0.0))), 2)
                
                effective_capital = collaterals_im if collaterals_im > 0 else (collateral if collateral > 0 else (buying_power + positions_margin_used))
                if effective_capital > 0:
                    margin_utilization_pct = round((positions_im / effective_capital) * 100.0, 2)
                else:
                    margin_utilization_pct = 0.0
                
                max_allowed_margin = (config.max_margin_utilization_pct / 100.0) * effective_capital
                margin_headroom_usd = round(max(0.0, min(buying_power, max_allowed_margin - positions_im)), 2) if effective_capital > 0 else buying_power
                
                calc_opt_delta = 0.0
                calc_perp_delta = 0.0
                opt_cnt = 0
                for pos in sub_res.get("positions", []):
                    raw_amt = float(pos.get("amount", 0.0))
                    amt = round(raw_amt, 4)
                    if abs(amt) < 1e-5:
                        continue
                    iname = pos.get("instrument_name", "")
                    itype = pos.get("instrument_type", "")
                    current_positions_summary[iname] = amt
                    
                    if itype == "option":
                        strike_k, opt_t, contract_dte, contract_T = parse_option_contract_details(iname)
                        unit_d = float(pos.get("delta") or 0.0)
                        
                        if unit_d == 0.0 and strike_k is not None and contract_T is not None:
                            if opt_t == "C":
                                unit_d = bs_call_delta(spot_price, strike_k, contract_T, r, sigma)
                            else:
                                unit_d = bs_put_delta(spot_price, strike_k, contract_T, r, sigma)
                                
                        pos_d = amt * unit_d
                        
                        if symbol in iname:
                            opt_cnt += 1
                            calc_opt_delta += pos_d
                            active_options_legs_raw.append({
                                "instrument_name": iname,
                                "amount": amt,
                                "average_price": float(pos.get("average_price_excl_fees") or pos.get("average_price") or 0.0),
                                "average_price_excl_fees": float(pos.get("average_price_excl_fees") or 0.0),
                                "mark_price": float(pos.get("mark_price") or 0.0),
                                "mark_value": float(pos.get("mark_value") or 0.0),
                                "unrealized_pnl": float(pos.get("unrealized_pnl") or 0.0),
                                "unrealized_pnl_excl_fees": float(pos.get("unrealized_pnl_excl_fees") or 0.0),
                                "total_fees": float(pos.get("total_fees") or 0.0),
                                "strike": strike_k,
                                "opt_type": opt_t,
                                "exact_dte": contract_dte,
                                "exact_T": contract_T,
                                "unit_delta": unit_d,
                                "sigma": sigma
                            })
                            
                        parts = iname.split("-")
                        strike_disp = f"${float(parts[2]):,.0f}" if len(parts) >= 4 and parts[2].replace('.', '', 1).isdigit() else "-"
                        dte_disp = f"{contract_dte:.1f}d" if contract_dte is not None else "-"
                        leg_desc = "Long Option" if amt > 0 else "Short Option"
                        if len(parts) >= 4:
                            opt_type_str = parts[3]
                            if amt < 0:
                                leg_desc = f"Short Call ({dte_disp})" if opt_type_str == "C" else f"Short Put ({dte_disp})"
                            else:
                                leg_desc = f"Long Call ({dte_disp})" if opt_type_str == "C" else f"Long Put ({dte_disp})"
                                
                        live_active_positions_table.append({
                            "Leg": leg_desc,
                            "Contract": iname,
                            "Strike": strike_disp,
                            "Size": f"{amt:+.{size_decimals}f}",
                            "Delta": f"{pos_d:+.4f}"
                        })
                    elif itype == "perp":
                        if symbol in iname:
                            calc_perp_delta += amt
                        live_active_positions_table.append({
                            "Leg": "Perpetual Position",
                            "Contract": iname,
                            "Strike": "-",
                            "Size": f"{amt:+.{size_decimals}f}",
                            "Delta": f"{amt:+.4f}"
                        })
                        
                active_options_count = opt_cnt
                live_options_delta = round(calc_opt_delta, 4)
                live_perp_delta = round(calc_perp_delta, 4)
                subaccount_queried_successfully = True
        except Exception as e:
            logger.warning(f"Error querying live Derive subaccount from private API: {e}")

    # -------------------------------------------------------------
    # 5. Rigorous Mark-to-Market PnL & Take-Profit (60%) / Stop-Loss Engine
    # -------------------------------------------------------------
    prev_state = load_previous_position_state()
    eth_opt_legs = [p for p in active_options_legs_raw if symbol in p.get("instrument_name", "")]
    has_eth_ic_open = bool(eth_opt_legs and len(eth_opt_legs) >= 4)
    
    entry_credit_usd = await resolve_actual_entry_credit(http_client, subaccount_id, auth_headers, eth_opt_legs, theoretical_net_unit_credit)
    current_cost_to_close = 0.0
    valid_leg_marks = {}
    missing_or_unreliable_marks = []
    mark_data_reliable = True
    
    # 1. Authoritative Clearinghouse Realtime Valuation
    has_clearinghouse_pnl = False
    clearinghouse_pnl_excl_fees = 0.0
    clearinghouse_mark_value_sum = 0.0
    
    if has_eth_ic_open:
        for p in eth_opt_legs:
            iname = p.get("instrument_name", "")
            amt = float(p.get("amount", 0.0))
            strike_k = p.get("strike")
            opt_t = p.get("opt_type")
            contract_T = p.get("exact_T")
            leg_sigma = p.get("sigma") or sigma
            
            # Subaccount Clearinghouse Data
            pos_unrealized_pnl_excl = p.get("unrealized_pnl_excl_fees")
            pos_mark_val = p.get("mark_value")
            if pos_unrealized_pnl_excl is not None and pos_mark_val is not None:
                has_clearinghouse_pnl = True
                clearinghouse_pnl_excl_fees += float(pos_unrealized_pnl_excl)
                clearinghouse_mark_value_sum += float(pos_mark_val)

            leg_mark = None
            mark_source = None
            
            # 1. Try subaccount clearinghouse mark_price
            raw_sub_mark = float(p.get("mark_price") or 0.0)
            if raw_sub_mark > 0.0:
                leg_mark = raw_sub_mark
                mark_source = "subaccount"
                LAST_KNOWN_MARK_PRICES[iname] = raw_sub_mark
            else:
                # 2. Try live ticker fetch from Derive public API
                try:
                    t_resp = (await http_client.post("https://api.lyra.finance/public/get_ticker", json={"instrument_name": iname}, timeout=3.5)).json().get("result", {})
                    raw_mark = t_resp.get("mark_price")
                    if raw_mark is not None:
                        val = float(raw_mark)
                        if val > 0.0:
                            leg_mark = val
                            mark_source = "live"
                            LAST_KNOWN_MARK_PRICES[iname] = val
                except Exception as e:
                    logger.warning(f"Error fetching live ticker mark price for {iname}: {e}")
                
            # 3. Fallback to last known valid mark from cache / previous state
            if leg_mark is None or leg_mark <= 0.0:
                mark_data_reliable = False
                missing_or_unreliable_marks.append(iname)
                
                cached_mark = LAST_KNOWN_MARK_PRICES.get(iname) or prev_state.get("leg_marks", {}).get(iname)
                if cached_mark and float(cached_mark) > 0.0:
                    leg_mark = float(cached_mark)
                    mark_source = "cached"
                    logger.warning(f"Live ticker unavailable for {iname}; falling back safely to last known mark price ${leg_mark:.2f}")
                else:
                    # 4. Fallback to Black-Scholes theoretical price for telemetry/display only
                    if strike_k is not None and contract_T is not None and opt_t and spot_price > 0:
                        try:
                            bs_price = bs_call_price(spot_price, strike_k, contract_T, r, leg_sigma) if opt_t == "C" else bs_put_price(spot_price, strike_k, contract_T, r, leg_sigma)
                            if bs_price > 0:
                                leg_mark = round(bs_price, 4)
                                mark_source = "bs_theoretical"
                                logger.warning(f"Live and cached mark unavailable for {iname}; using Black-Scholes estimate ${leg_mark:.2f}")
                        except Exception as e_bs:
                            logger.warning(f"Black-Scholes calculation failed for {iname}: {e_bs}")
                            
                    if leg_mark is None or leg_mark <= 0.0:
                        leg_mark = 0.0
                        mark_source = "zero_fallback"
                        logger.warning(f"Failed to resolve mark price for {iname}; defaulted to $0.00")

            valid_leg_marks[iname] = leg_mark
            current_cost_to_close += (-amt) * leg_mark

        # If clearinghouse mark value is available, use it directly for exact cost to close
        if has_clearinghouse_pnl and abs(clearinghouse_mark_value_sum) > 0.01:
            current_cost_to_close = max(0.0, -clearinghouse_mark_value_sum)

        # Safety Check: If cost to close computed as ~0 while positions are active, mark as unreliable
        if current_cost_to_close <= 0.01:
            mark_data_reliable = False
            logger.warning(f"Total cost to close is ${current_cost_to_close:.2f} while {len(eth_opt_legs)} option legs are active. Marked as unreliable to prevent false take-profit.")

    current_cost_to_close = round(max(0.0, current_cost_to_close), 2)
    
    # Realtime Authoritative Clearinghouse PnL
    if has_eth_ic_open and has_clearinghouse_pnl:
        unrealized_pnl = round(clearinghouse_pnl_excl_fees, 2)
    else:
        unrealized_pnl = round(entry_credit_usd - current_cost_to_close, 2)
        
    profit_captured_pct = round((unrealized_pnl / entry_credit_usd * 100.0), 2) if entry_credit_usd > 0 else 0.0
    pnl_disp = f"+${unrealized_pnl:,.2f}" if unrealized_pnl >= 0 else f"-${abs(unrealized_pnl):,.2f}"
    
    # Strict TP/SL Evaluation: ONLY evaluated if mark data is verified reliable and entry credit is authentic
    if has_eth_ic_open and mark_data_reliable and entry_credit_usd >= 5.0:
        min_pnl_threshold = max(3.0, (config.target_profit_pct / 100.0) * entry_credit_usd * 0.95)
        take_profit_triggered = (
            (profit_captured_pct >= config.target_profit_pct)
            and (unrealized_pnl >= min_pnl_threshold)
            and (unrealized_pnl > 0.0)
        )
        stop_loss_triggered = (
            (unrealized_pnl <= -(config.stop_loss_pct / 100.0 * entry_credit_usd))
            and (unrealized_pnl < -5.0)
        )
    else:
        take_profit_triggered = False
        stop_loss_triggered = False
        if has_eth_ic_open and not mark_data_reliable:
            logger.warning(
                f"Skipping TP/SL evaluation for this tick: Mark price data is missing or unreliable "
                f"(unreliable legs: {missing_or_unreliable_marks or 'N/A'}, cost_to_close: ${current_cost_to_close:.2f}). "
                f"Holding positions without executing orders."
            )
    
    if not config.enable_options_entry:
        if has_eth_ic_open and not mark_data_reliable:
            options_execution_status = f"HEDGE-ONLY MODE: HOLDING POSITION (MARK DATA UNRELIABLE - TP/SL SKIPPED | Est PnL: {pnl_disp})"
        else:
            options_execution_status = f"HEDGE-ONLY MODE: HOLDING POSITION (PnL: {pnl_disp} | {profit_captured_pct:.1f}% | Target: {config.target_profit_pct:.1f}%)" if has_eth_ic_open else "HEDGE-ONLY MODE: NEW ENTRY DISABLED (STANDBY)"
    elif signal_conflict and not has_eth_ic_open:
        options_execution_status = f"ENTRY BLOCKED (Derivatives Monkey Conflict: {conflict_reason})"
    else:
        if has_eth_ic_open and not mark_data_reliable:
            options_execution_status = f"HOLDING POSITION (MARK DATA UNRELIABLE - TP/SL SKIPPED | Est PnL: {pnl_disp})"
        else:
            options_execution_status = f"HOLDING POSITION (PnL: {pnl_disp} | {profit_captured_pct:.1f}% of ${entry_credit_usd:.2f} credit | Target: {config.target_profit_pct:.1f}%)" if has_eth_ic_open else "NO ACTIVE OPTIONS (MONITORING ENTRY)"
        
    position_change_occurred = False
    change_event_details = []

    # Slippage multiplier calculation based on config.max_perp_slippage_bps (default 50 bps = 0.50%)
    perp_slippage_mult = config.max_perp_slippage_bps / 10000.0

    # Default initialization for perp action & hedge status to prevent UnboundLocalError across all code paths
    target_perp_delta = - live_options_delta
    max_allowed_perp_delta = max(min_order_size, abs(live_options_delta) * config.max_perp_delta_hedge_ratio)
    delta_imbalance_init = target_perp_delta - live_perp_delta
    hedge_action_status = f"DELTA NEUTRAL (|Imbalance| {abs(delta_imbalance_init):.4f} <= {config.hedge_band:.2f})"
    perp_order_summary = "None (Aligned with Options Delta Hedge Cap)"

    # -------------------------------------------------------------
    # 6. Automated Take-Profit (>=60%) or Stop-Loss Unwind Execution with Multi-Tick Best Quote Selection
    # -------------------------------------------------------------
    if (take_profit_triggered or stop_loss_triggered) and mark_data_reliable and not config.paper_mode and session_key_wallet and smart_contract_wallet and subaccount_id:
        trigger_name = f"TAKE-PROFIT (>={config.target_profit_pct:.0f}% Captured)" if take_profit_triggered else "STOP-LOSS"
        logger.info(f"Triggering automated exit: {trigger_name} | Profit: {profit_captured_pct:.1f}% | PnL: ${unrealized_pnl:,.2f}")
        try:
            close_legs = []
            for p in eth_opt_legs:
                iname = p.get("instrument_name", "")
                amt = float(p.get("amount", 0.0))
                if amt == 0:
                    continue
                close_dir = "buy" if amt < 0 else "sell"
                c_amount = Decimal(str(abs(amt)))
                
                inst_info = (await http_client.post("https://api.lyra.finance/public/get_instrument", json={"instrument_name": iname}, timeout=3.5)).json().get("result", {})
                b_addr = inst_info.get("base_asset_address", "0x0000000000000000000000000000000000000000")
                b_sub = int(inst_info.get("base_asset_sub_id", 0))
                
                close_legs.append(RFQQuoteDetails(
                    instrument_name=iname,
                    direction=close_dir,
                    asset_address=b_addr,
                    sub_id=b_sub,
                    price=Decimal("0"),
                    amount=c_amount
                ))
                
            if close_legs:
                close_legs.sort(key=lambda x: x.instrument_name)
                rfq_close_data = RFQExecuteModuleData(global_direction="buy", max_fee=Decimal("1000"), legs=close_legs)
                send_rfq_resp = await http_client.post("https://api.lyra.finance/private/send_rfq", json={"subaccount_id": subaccount_id, **rfq_close_data.to_rfq_json()}, headers=auth_headers, timeout=5.0)
                close_rfq_id = send_rfq_resp.json().get("result", {}).get("rfq_id")
                
                if close_rfq_id:
                    # Maximum acceptable cost to close filter
                    max_exit_cost = None
                    if take_profit_triggered:
                        max_exit_cost = entry_credit_usd * (1.0 - (config.target_profit_pct / 100.0) * 0.85)
                    elif stop_loss_triggered:
                        max_exit_cost = entry_credit_usd + (config.stop_loss_pct / 100.0 * entry_credit_usd * 1.25)

                    # Multi-tick quote polling: selects lowest cost-to-close under max_exit_cost
                    best_quote = await poll_best_rfq_quote(
                        http_client, 
                        subaccount_id, 
                        auth_headers, 
                        close_rfq_id, 
                        rfq_close_data.legs, 
                        mode="exit", 
                        max_wait_seconds=5,
                        max_acceptable_cost=max_exit_cost
                    )
                    
                    if best_quote:
                        # Verify quoted net execution PnL before signing/submitting order
                        quote_close_cost = 0.0
                        for q_leg in best_quote.get("legs", []):
                            q_iname = q_leg.get("instrument_name")
                            q_px = float(q_leg.get("price", 0.0))
                            q_amt = float(q_leg.get("amount", 1.0))
                            for cl in close_legs:
                                if cl.instrument_name == q_iname:
                                    if cl.direction == "buy":
                                        quote_close_cost += q_px * q_amt
                                    else:
                                        quote_close_cost -= q_px * q_amt
                                    break
                        
                        quote_realized_pnl = round(entry_credit_usd - quote_close_cost, 2)
                        quote_profit_pct = round((quote_realized_pnl / entry_credit_usd * 100.0), 2) if entry_credit_usd > 0 else 0.0
                        
                        # HARD SAFETY GATE
                        can_execute_quote = True
                        if take_profit_triggered:
                            min_required_profit = max(2.0, (config.target_profit_pct / 100.0) * entry_credit_usd * 0.85)
                            if quote_realized_pnl < min_required_profit or quote_profit_pct < (config.target_profit_pct * 0.85):
                                logger.warning(
                                    f"Take-Profit Quote REJECTED: Quoted cost ${quote_close_cost:.2f} yields PnL +${quote_realized_pnl:.2f} "
                                    f"({quote_profit_pct:.1f}% profit vs minimum required ${min_required_profit:.2f}). "
                                    f"Aborting execution to prevent closing at insufficient profit or loss."
                                )
                                can_execute_quote = False
                        elif stop_loss_triggered:
                            if max_exit_cost is not None and quote_close_cost > max_exit_cost:
                                logger.warning(
                                    f"Stop-Loss Quote REJECTED: Quoted cost ${quote_close_cost:.2f} exceeds allowable cap ${max_exit_cost:.2f}."
                                )
                                can_execute_quote = False

                        if can_execute_quote:
                            prices_mapped = map_rfq_quote_prices(rfq_close_data.legs, best_quote.get("legs", []))
                            if prices_mapped:
                                action = SignedAction(subaccount_id=subaccount_id, owner=smart_contract_wallet, signer=session_key_wallet.address, signature_expiry_sec=utils.MAX_INT_32, nonce=utils.get_action_nonce(), module_address=RFQ_MODULE_ADDRESS, module_data=rfq_close_data, DOMAIN_SEPARATOR=DOMAIN_SEPARATOR, ACTION_TYPEHASH=ACTION_TYPEHASH)
                                action.sign(session_key_wallet.key)
                                
                                exec_resp = await http_client.post("https://api.lyra.finance/private/execute_quote", json={**action.to_json(), "label": f"{symbol}-IC-CLOSE-TP", "rfq_id": best_quote["rfq_id"], "quote_id": best_quote["quote_id"]}, headers=auth_headers, timeout=5.0)
                                if exec_resp.json().get("result", {}).get("status") == "filled":
                                    options_execution_status = f"CLOSED VIA {trigger_name} (+${quote_realized_pnl:,.2f} Realized PnL | {quote_profit_pct:.1f}% Captured)"
                                    position_change_occurred = True
                                    change_event_details.append(f"Options Package Closed via {trigger_name} (+${quote_realized_pnl:,.2f})")
            
            # Flatten Perpetual Hedge Position to 0.00 with 50 bps limit price protection
            if live_perp_delta != 0.0:
                is_perp_long = live_perp_delta > 0
                flat_side = "sell" if is_perp_long else "buy"
                flat_size = round(abs(live_perp_delta), size_decimals)
                # 50 bps execution price protection: Sell uses (1.0 - slippage), Buy uses (1.0 + slippage)
                dyn_limit_price = Decimal(str(round(spot_price * (1.0 - perp_slippage_mult) if is_perp_long else spot_price * (1.0 + perp_slippage_mult), 2)))
                trade_data = TradeModuleData(
                    asset_address=inst_perp_info.get("base_asset_address", "0x0000000000000000000000000000000000000000"),
                    sub_id=int(inst_perp_info.get("base_asset_sub_id", 0)),
                    limit_price=dyn_limit_price,
                    amount=Decimal(str(flat_size)),
                    max_fee=Decimal("100"),
                    recipient_id=subaccount_id,
                    is_bid=not is_perp_long
                )
                action_flat = SignedAction(subaccount_id=subaccount_id, owner=smart_contract_wallet, signer=session_key_wallet.address, signature_expiry_sec=utils.MAX_INT_32, nonce=utils.get_action_nonce(), module_address=TRADE_MODULE_ADDRESS, module_data=trade_data, DOMAIN_SEPARATOR=DOMAIN_SEPARATOR, ACTION_TYPEHASH=ACTION_TYPEHASH)
                action_flat.sign(session_key_wallet.key)
                order_resp = await http_client.post("https://api.lyra.finance/private/order", json={**action_flat.to_json(), "instrument_name": perp_symbol, "direction": flat_side, "order_type": "market", "reduce_only": True, "time_in_force": "ioc", "label": "tp-flatten-perp"}, headers=auth_headers, timeout=5.0)
                if order_resp.json().get("result", {}).get("order", {}).get("order_status") == "filled":
                    live_perp_delta = 0.0
                    change_event_details.append(f"Perpetual Hedge Flattened to 0.00 {symbol}")
                    perp_order_summary = f"{execution_mode_str} Flattened Perpetual Hedge to 0.00 {symbol} ({trigger_name})"
                    hedge_action_status = f"PERP FLATTENED ({trigger_name})"
                else:
                    err_msg = order_resp.json().get("error", {}).get("message", "Order not filled")
                    perp_order_summary = f"{execution_mode_str} Flatten Perp: {err_msg}"
                    hedge_action_status = f"FLATTEN FAILED ({trigger_name})"
            else:
                perp_order_summary = f"None (Perp already flat on {trigger_name})"
                hedge_action_status = f"FLAT ({trigger_name})"

        except Exception as e:
            logger.error(f"Error during automated exit execution: {e}", exc_info=True)
            options_execution_status = f"EXIT TRIGGER FAILED: {e}"
            await send_telegram_error_alert(
                context=context,
                chat_id=config.telegram_chat_id,
                error_title="CRITICAL: AUTOMATED EXIT EXECUTION FAILED",
                error=e,
                cycle_number=timestamp_str,
                cycle_type="Exit Order Execution",
                pair=config.trading_pair,
                client=http_client,
                extra_info=f"Automated exit failed ({trigger_name}). Please check account status immediately!",
                throttle_seconds=0.0
            )

    elif (take_profit_triggered or stop_loss_triggered) and mark_data_reliable and config.paper_mode:
        trigger_name = f"TAKE-PROFIT (>={config.target_profit_pct:.0f}% Captured)" if take_profit_triggered else "STOP-LOSS"
        logger.info(f"[PAPER MODE] Simulated Exit: {trigger_name} | Profit: {profit_captured_pct:.1f}% | PnL: ${unrealized_pnl:,.2f}")
        options_execution_status = f"[PAPER] SIMULATED EXIT VIA {trigger_name} (+${unrealized_pnl:,.2f} | {profit_captured_pct:.1f}%)"
        position_change_occurred = True
        change_event_details.append(f"[PAPER] Options Package Simulated Exit via {trigger_name} (+${unrealized_pnl:,.2f})")
        if live_perp_delta != 0.0:
            live_perp_delta = 0.0
            change_event_details.append(f"[PAPER] Perpetual Hedge Simulated Flatten to 0.00 {symbol}")
            perp_order_summary = f"[PAPER MODE] Simulated Flatten Perpetual Hedge to 0.00 {symbol} ({trigger_name})"
            hedge_action_status = f"PERP FLATTENED ({trigger_name})"
        else:
            perp_order_summary = f"None (Perp already flat on {trigger_name})"
            hedge_action_status = f"FLAT ({trigger_name})"

    # -------------------------------------------------------------
    # 7. Dynamic Volatility Scale-In with Derivatives Monkey Consensus Cross-Check
    # -------------------------------------------------------------
    can_scale_in = (
        config.enable_options_entry
        and edge_open
        and not signal_conflict
        and (margin_utilization_pct < config.max_margin_utilization_pct)
        and (margin_headroom_usd >= 25.0)
        and not (take_profit_triggered or stop_loss_triggered)
        and (mark_data_reliable or not has_eth_ic_open)
    )
    
    if can_scale_in:
        if not config.paper_mode:
            if not (session_key_wallet and smart_contract_wallet and subaccount_id):
                options_execution_status = f"LIVE RFQ SKIPPED (Credentials missing in .env)"
            else:
                try:
                    inst_resp = await http_client.post("https://api.lyra.finance/public/get_instruments", json={"currency": symbol, "instrument_type": "option", "expired": False}, timeout=5.0)
                    inst_data = inst_resp.json().get("result", [])
                    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
                    valid_expiries = sorted(list(set(
                        i.get("option_details", {}).get("expiry", 0)
                        for i in inst_data
                        if (i.get("option_details", {}).get("expiry", 0) - now_ts) >= 3 * 86400
                    )))
                    target_expiry_ts = now_ts + (config.dte * 86400)
                    chosen_expiry = min(valid_expiries, key=lambda exp: abs(exp - target_expiry_ts)) if valid_expiries else (now_ts + 14 * 86400)
                    
                    same_expiry_options = [i for i in inst_data if i.get("option_details", {}).get("expiry") == chosen_expiry]
                    calls = [i for i in same_expiry_options if i.get("option_details", {}).get("option_type") == "C"]
                    puts = [i for i in same_expiry_options if i.get("option_details", {}).get("option_type") == "P"]
                    
                    # Option B: Pure Target Delta Selection
                    inst_sc = find_closest_delta_option(calls, config.short_delta_target, True, spot_price, T_entry, r, sigma)
                    inst_sp = find_closest_delta_option(puts, config.short_delta_target, False, spot_price, T_entry, r, sigma)
                    inst_lc = find_closest_delta_option(calls, config.wing_delta_target, True, spot_price, T_entry, r, sigma)
                    inst_lp = find_closest_delta_option(puts, config.wing_delta_target, False, spot_price, T_entry, r, sigma)
                    
                    strike_sc_matched = float(inst_sc.get("option_details", {}).get("strike", k_short_call))
                    strike_lc_matched = float(inst_lc.get("option_details", {}).get("strike", k_long_call))
                    wing_width = abs(strike_lc_matched - strike_sc_matched) or 100.0
                    
                    base_scale = 0.50 if dealer_gex_m < 0.0 else 1.00
                    package_scales = [base_scale * 1.00, base_scale * 0.75, base_scale * 0.50, base_scale * 0.25]
                    package_filled = False
                    
                    for scale in package_scales:
                        candidate_size = round(config.contract_size * scale, size_decimals)
                        if candidate_size <= 0:
                            continue
                        est_package_margin = wing_width * candidate_size
                        projected_margin_used = positions_margin_used + est_package_margin
                        projected_utilization = (projected_margin_used / effective_capital * 100.0) if effective_capital > 0 else 100.0
                        
                        if projected_utilization > config.max_margin_utilization_pct or est_package_margin > buying_power:
                            continue
                            
                        c_amount = Decimal(str(candidate_size))
                        legs = [
                            RFQQuoteDetails(instrument_name=inst_sc["instrument_name"], direction="sell", asset_address=inst_sc["base_asset_address"], sub_id=int(inst_sc["base_asset_sub_id"]), price=Decimal("0"), amount=c_amount),
                            RFQQuoteDetails(instrument_name=inst_sp["instrument_name"], direction="sell", asset_address=inst_sp["base_asset_address"], sub_id=int(inst_sp["base_asset_sub_id"]), price=Decimal("0"), amount=c_amount),
                            RFQQuoteDetails(instrument_name=inst_lc["instrument_name"], direction="buy", asset_address=inst_lc["base_asset_address"], sub_id=int(inst_lc["base_asset_sub_id"]), price=Decimal("0"), amount=c_amount),
                            RFQQuoteDetails(instrument_name=inst_lp["instrument_name"], direction="buy", asset_address=inst_lp["base_asset_address"], sub_id=int(inst_lp["base_asset_sub_id"]), price=Decimal("0"), amount=c_amount),
                        ]
                        legs.sort(key=lambda x: x.instrument_name)
                        rfq_module_data = RFQExecuteModuleData(global_direction="buy", max_fee=Decimal("1000"), legs=legs)
                        
                        send_rfq_resp = await http_client.post("https://api.lyra.finance/private/send_rfq", json={"subaccount_id": subaccount_id, **rfq_module_data.to_rfq_json()}, headers=auth_headers, timeout=5.0)
                        live_rfq_id = send_rfq_resp.json().get("result", {}).get("rfq_id")
                        
                        if not live_rfq_id:
                            continue
                            
                        # Multi-tick quote polling: selects highest net premium credit among competing market makers
                        best_quote = await poll_best_rfq_quote(http_client, subaccount_id, auth_headers, live_rfq_id, rfq_module_data.legs, mode="entry", max_wait_seconds=5)
                        if best_quote:
                            prices_mapped = map_rfq_quote_prices(rfq_module_data.legs, best_quote.get("legs", []))
                            if prices_mapped:
                                action = SignedAction(subaccount_id=subaccount_id, owner=smart_contract_wallet, signer=session_key_wallet.address, signature_expiry_sec=utils.MAX_INT_32, nonce=utils.get_action_nonce(), module_address=RFQ_MODULE_ADDRESS, module_data=rfq_module_data, DOMAIN_SEPARATOR=DOMAIN_SEPARATOR, ACTION_TYPEHASH=ACTION_TYPEHASH)
                                action.sign(session_key_wallet.key)
                                
                                exec_resp = await http_client.post("https://api.lyra.finance/private/execute_quote", json={**action.to_json(), "label": f"{symbol}-IC-PACKAGE-{candidate_size}", "rfq_id": best_quote["rfq_id"], "quote_id": best_quote["quote_id"]}, headers=auth_headers, timeout=5.0)
                                if exec_resp.json().get("result", {}).get("status") == "filled":
                                    options_execution_status = f"LIVE RFQ PACKAGE FILLED (+{candidate_size} {symbol} Iron Condor | Margin Util: {projected_utilization:.1f}%)"
                                    position_change_occurred = True
                                    change_event_details.append(f"Gradual Scale-In: +{candidate_size} {symbol} Iron Condor")
                                    package_filled = True
                                    break
                    if not package_filled and has_eth_ic_open:
                        options_execution_status = f"HOLDING POSITION (PnL: +${unrealized_pnl:,.2f} | Margin Util: {margin_utilization_pct:.1f}% | Headroom: ${margin_headroom_usd:,.2f})"
                except Exception as e:
                    logger.error(f"Scale-in RFQ error: {e}", exc_info=True)
                    options_execution_status = f"SCALE-IN RFQ ERROR: {e}"
                    await send_telegram_error_alert(
                        context=context,
                        chat_id=config.telegram_chat_id,
                        error_title="OPTIONS SCALE-IN RFQ ERROR",
                        error=e,
                        cycle_number=timestamp_str,
                        cycle_type="Scale-In RFQ",
                        pair=config.trading_pair,
                        client=http_client,
                        extra_info="RFQ scale-in order encountered an error. Strategy continuing.",
                        throttle_seconds=60.0
                    )

    # -------------------------------------------------------------
    # 8. Strict Perpetual Delta Hedge Execution
    # -------------------------------------------------------------
    if not (take_profit_triggered or stop_loss_triggered):
        live_perp_delta, hedge_action_status, perp_order_summary, rebalanced = await execute_perp_rebalance(
            config=config,
            http_client=http_client,
            symbol=symbol,
            spot_price=spot_price,
            live_options_delta=live_options_delta,
            live_perp_delta=live_perp_delta,
            auth_headers=auth_headers,
            session_key_wallet=session_key_wallet,
            smart_contract_wallet=smart_contract_wallet,
            subaccount_id=subaccount_id,
            inst_perp_info=inst_perp_info,
            size_decimals=size_decimals,
            min_order_size=min_order_size,
            context=context
        )
        if rebalanced:
            position_change_occurred = True
            change_event_details.append(f"Perpetual Delta Rebalance: {perp_order_summary}")

    # -------------------------------------------------------------
    # 9. Robust Position Change Detection (With Float Tolerance) & Telegram Notification
    # -------------------------------------------------------------
    material_position_change = has_position_state_materially_changed(current_positions_summary, prev_state.get("positions", {}))
    if material_position_change or position_change_occurred:
        position_change_occurred = True
        save_position_state({
            "positions": current_positions_summary,
            "options_delta": live_options_delta,
            "perp_delta": live_perp_delta,
            "margin_utilization_pct": margin_utilization_pct,
            "entry_credit_usd": entry_credit_usd,
            "unrealized_pnl": unrealized_pnl,
            "profit_captured_pct": profit_captured_pct,
            "cost_to_close": current_cost_to_close,
            "leg_marks": valid_leg_marks,
            "mark_data_reliable": mark_data_reliable,
            "timestamp": timestamp_str
        })
        
    alerts_log = []
    
    if position_change_occurred or not config.notify_on_change_only:
        telegram_event_title = "⚡ *[POSITION CHANGE EXECUTED]* ⚡" if position_change_occurred else "⏱️ *[5-Min Monitoring Cycle]* ⏱️"
        cadence_alert_msg = (
            f"{telegram_event_title}\n"
            f"*Mode*: `{strategy_operating_mode}`\n"
            f"*Asset*: `{config.trading_pair}` | *Spot*: `${spot_price:,.2f}`\n\n"
            f"• *Vol Edge*: `{net_edge:.2f} pts` (IV {iv_14d:.1f}% vs RV {rv_7d:.1f}%)\n"
            f"• *Derivatives Monkey*: `{dm_strategy}` (Conf: `{dm_confidence}%` | GEX: `{dm_gex_regime}`)\n"
            f"• *Signal Consensus*: `{consensus_str}`\n"
            f"• *Target Deltas*: Short `{config.short_delta_target:.2f}Δ` | Wings `{config.wing_delta_target:.2f}Δ`\n"
            f"• *Market GEX*: `${dealer_gex_m:+.3f}M` | *DEX*: `${dealer_dex_m:+.3f}M`\n"
            f"• *25D IV Skew*: `{iv_skew_pts:+.2f} pts` | *Flow Bias*: `{block_rfq_bias}`\n"
            f"• *Net Entry Credit*: `${entry_credit_usd:,.2f}` | *Cost to Close*: `${current_cost_to_close:,.2f}`\n"
            f"• *Unrealized PnL*: `{pnl_disp}` (*{profit_captured_pct:.1f}%* / Target: `{config.target_profit_pct:.1f}%`)\n"
            f"• *Margin Utilization*: `{margin_utilization_pct:.2f}%` / *Cap*: `{config.max_margin_utilization_pct:.1f}%` (Headroom: `${margin_headroom_usd:,.2f}`)\n"
            f"• *Buying Power*: `${buying_power:,.2f}` | *Collateral*: `${collateral:,.2f}`\n"
            f"• *Options Delta*: `{live_options_delta:+.4f} {symbol}` ({active_options_count} active legs)\n"
            f"• *Perpetual Delta*: `{live_perp_delta:+.4f} {symbol}` (Target Cap: `{target_perp_delta:+.4f} {symbol}`)\n"
            f"• *Options Status*: `{options_execution_status}`\n"
            f"• *Perpetual Action*: `{perp_order_summary}`\n"
        )
        if change_event_details:
            cadence_alert_msg += f"• *Changes*: " + ", ".join(change_event_details) + "\n"
            
        await send_telegram_alert(context, config.telegram_chat_id, cadence_alert_msg, http_client)
        alerts_log.append({
            "Event": "Position Change Execution",
            "Asset": config.trading_pair,
            "Margin Utilization": f"{margin_utilization_pct:.2f}%",
            "Options Delta": f"{live_options_delta:+.4f} {symbol}",
            "Perp Delta": f"{live_perp_delta:+.4f} {symbol}",
            "Status": "DELIVERED TO TELEGRAM"
        })
    else:
        logger.info("Monitoring tick: No position change occurred. Telegram notification skipped.")
        alerts_log.append({
            "Event": "Idle Monitoring Tick",
            "Asset": config.trading_pair,
            "Margin Utilization": f"{margin_utilization_pct:.2f}%",
            "Options Delta": f"{live_options_delta:+.4f} {symbol}",
            "Perp Delta": f"{live_perp_delta:+.4f} {symbol}",
            "Status": "IDLE (TELEGRAM SKIPPED)"
        })

    # Save Heartbeat for external Watchdog Monitor
    save_heartbeat({
        "trading_pair": config.trading_pair,
        "operating_mode": strategy_operating_mode,
        "enable_options_entry": config.enable_options_entry,
        "derivatives_monkey_strategy": dm_strategy,
        "derivatives_monkey_confidence": dm_confidence,
        "derivatives_monkey_allowed": dm_short_vol_allowed,
        "signal_consensus": consensus_str,
        "short_delta_target": config.short_delta_target,
        "wing_delta_target": config.wing_delta_target,
        "spot_price": spot_price,
        "vol_edge": net_edge,
        "dealer_gex_m": dealer_gex_m,
        "dealer_dex_m": dealer_dex_m,
        "iv_skew_pts": iv_skew_pts,
        "block_rfq_bias": block_rfq_bias,
        "margin_utilization_pct": margin_utilization_pct,
        "buying_power": buying_power,
        "collateral": collateral,
        "options_delta": live_options_delta,
        "perp_delta": live_perp_delta,
        "entry_credit_usd": entry_credit_usd,
        "unrealized_pnl": unrealized_pnl,
        "profit_captured_pct": profit_captured_pct,
        "cost_to_close": current_cost_to_close,
        "mark_data_reliable": mark_data_reliable,
        "options_status": options_execution_status,
        "perp_action": perp_order_summary,
        "subaccount_id": subaccount_id,
        "status": "healthy"
    })

    # Greeks calculation for strikes table
    delta_sc = -round(bs_call_delta(spot_price, k_short_call, T_entry, r, sigma), 4)
    delta_sp = round(bs_put_delta(spot_price, k_short_put, T_entry, r, sigma), 4)
    delta_lc = round(bs_call_delta(spot_price, k_long_call, T_entry, r, sigma), 4)
    delta_lp = -round(bs_put_delta(spot_price, k_long_put, T_entry, r, sigma), 4)
    exp_str = datetime.datetime.fromtimestamp(datetime.datetime.now(datetime.timezone.utc).timestamp() + config.dte * 86400).strftime("%Y%m%d")

    strikes_table = [
        {"Leg": f"Short Call ({config.short_delta_target:.2f}Δ)", "Contract": f"{symbol}-{exp_str}-{int(k_short_call)}-C", "Strike": f"${k_short_call:.0f}", "Size": f"-{contracts:.{size_decimals}f}", "Delta": f"{delta_sc:+.4f}"},
        {"Leg": f"Short Put ({config.short_delta_target:.2f}Δ)", "Contract": f"{symbol}-{exp_str}-{int(k_short_put)}-P", "Strike": f"${k_short_put:.0f}", "Size": f"-{contracts:.{size_decimals}f}", "Delta": f"{delta_sp:+.4f}"},
        {"Leg": f"Long Call Wing ({config.wing_delta_target:.2f}Δ)", "Contract": f"{symbol}-{exp_str}-{int(k_long_call)}-C", "Strike": f"${k_long_call:.0f}", "Size": f"+{contracts:.{size_decimals}f}", "Delta": f"{delta_lc:+.4f}"},
        {"Leg": f"Long Put Wing ({config.wing_delta_target:.2f}Δ)", "Contract": f"{symbol}-{exp_str}-{int(k_long_put)}-P", "Strike": f"${k_long_put:.0f}", "Size": f"+{contracts:.{size_decimals}f}", "Delta": f"{delta_lp:+.4f}"},
    ]
    display_table = live_active_positions_table if live_active_positions_table else strikes_table

    metrics_dict = {
        "subaccount_id": subaccount_id,
        "section_01": {
            "Execution Mode": execution_mode_str,
            "Operating Mode": strategy_operating_mode,
            "Trading Pair": config.trading_pair,
            "Margin Utilization": f"{margin_utilization_pct:.2f}%",
            "Buying Power": f"${buying_power:,.2f}",
            "Collateral": f"${collateral:,.2f}",
            "Positions Margin Used": f"${positions_margin_used:,.2f}",
            "Margin Utilization Cap": f"{config.max_margin_utilization_pct:.1f}%",
            "Margin Headroom ($)": f"${margin_headroom_usd:,.2f}",
        },
        "section_02": {
            f"{symbol} Spot Price": f"${spot_price:,.2f}",
            "Net Volatility Edge": f"{net_edge:.2f} pts (IV {iv_14d:.1f}% vs RV {rv_7d:.1f}%)",
            "Target Deltas": f"Short: {config.short_delta_target:.2f}Δ | Wings: {config.wing_delta_target:.2f}Δ",
            "Derivatives Monkey Signal": f"{dm_strategy} (Confidence: {dm_confidence}%)",
            "DM Vol / GEX Regime": f"Vol: {dm_vol_regime.upper()} | GEX: {dm_gex_regime.upper()}",
            "Signal Consensus": consensus_str,
            "Net Entry Credit": f"${entry_credit_usd:.2f}",
            "Cost to Close ($)": f"${current_cost_to_close:.2f}",
            "Unrealized PnL": pnl_disp,
            "Profit Captured": f"{profit_captured_pct:.1f}% (Target: {config.target_profit_pct:.1f}%)",
            "Options Strategy Status": options_execution_status,
        },
        "section_03": {
            "Aggregate Options Delta": f"{live_options_delta:+.4f} {symbol}",
            "Current Perpetual Delta": f"{live_perp_delta:+.4f} {symbol}",
            "Target Perpetual Hedge": f"{target_perp_delta:+.4f} {symbol}",
            "Max Allowed Perp Cap": f"{max_allowed_perp_delta:+.4f} {symbol}",
            "Perp Slippage Cap": f"{config.max_perp_slippage_bps} bps (0.50%)",
            "Rehedge Sub-Loop": f"{config.perp_hedge_interval_seconds}s Fast Cadence",
            "Delta Rebalance Order": perp_order_summary,
            "Hedge Compliance Status": hedge_action_status,
        }
    }

    summary_text = (
        f"Derive 5-Minute Cadence Autonomous Volatility Loop Trader executed.\n"
        f"- Mode: {execution_mode_str} ({strategy_operating_mode}) | Asset: {config.trading_pair} (${spot_price:,.2f})\n"
        f"- Vol Edge: {net_edge:.2f} pts | DM Strategy: {dm_strategy} ({dm_confidence}% Conf) | Consensus: {consensus_str}\n"
        f"- Real GEX: ${dealer_gex_m:+.3f}M | 25D Skew: {iv_skew_pts:+.2f} pts ({block_rfq_bias})\n"
        f"- Net Entry Credit: ${entry_credit_usd:.2f} | Cost to Close: ${current_cost_to_close:.2f} | Unrealized PnL: {pnl_disp} ({profit_captured_pct:.1f}% captured)\n"
        f"- Subaccount: #{subaccount_id} | Subaccount Value: ${subaccount_value:,.2f} | Collateral: ${collateral:,.2f}\n"
        f"- Margin Utilization: {margin_utilization_pct:.2f}% (Cap: {config.max_margin_utilization_pct:.1f}% | Headroom: ${margin_headroom_usd:,.2f})\n"
        f"- Buying Power: ${buying_power:,.2f} | Positions Margin Used: ${positions_margin_used:,.2f}\n"
        f"- Options Delta: {live_options_delta:+.4f} {symbol} | Current Perp Delta: {live_perp_delta:+.4f} {symbol}\n"
        f"- Perpetual Delta Hedge Cap: Target {target_perp_delta:+.4f} {symbol} (Max Cap: {max_allowed_perp_delta:+.4f} {symbol} | Sub-Loop: {config.perp_hedge_interval_seconds}s)\n"
        f"- Options Action: {options_execution_status}\n"
        f"- Perpetual Action: {perp_order_summary}\n"
        f"- Telegram Notification: {'SENT (Position Change Triggered)' if position_change_occurred else 'SKIPPED (No Position Change)'}"
    )

    return metrics_dict, display_table, alerts_log, summary_text, active_options_legs_raw

async def execute_fast_perp_hedge_cycle(
    config: Config, 
    context: ContextTypes.DEFAULT_TYPE, 
    http_client: httpx.AsyncClient, 
    cached_legs: list,
    cached_metrics: dict,
    display_table: list,
    alerts_log: list,
    sub_cycle_count: int = 0
) -> tuple[dict, list, list, bool]:
    """Fast 30-second sub-loop that fetches live spot, recalculates option Greeks, and rebalances perpetual delta hedge if drift exceeds hedge_band."""
    symbol = config.trading_pair.upper().split("-")[0]
    perp_symbol = f"{symbol}-PERP"
    smart_contract_wallet, session_key_priv, subaccount_id = resolve_derive_credentials(config)
    
    # 1. Fetch live non-blocking spot price
    try:
        spot_price = await fetch_robust_spot_price(http_client, symbol)
    except Exception as e:
        logger.warning(f"Fast hedge cycle spot fetch failed: {e}")
        await send_telegram_error_alert(
            context=context,
            chat_id=config.telegram_chat_id,
            error_title="FAST HEDGE SPOT PRICE FETCH FAILED",
            error=e,
            cycle_number=sub_cycle_count,
            cycle_type="Fast Hedge Sub-Cycle",
            pair=config.trading_pair,
            client=http_client,
            extra_info="Unable to fetch live spot price across all venues; fast rehedge skipped for this tick.",
            throttle_seconds=120.0
        )
        return cached_metrics, display_table, alerts_log, False
        
    from web3 import Web3
    from eth_account.messages import encode_defunct
    auth_headers = {}
    session_key_wallet = None
    if session_key_priv and smart_contract_wallet:
        try:
            web3_client = Web3()
            session_key_wallet = web3_client.eth.account.from_key(session_key_priv)
            timestamp_str_ms = str(int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000))
            sig_obj = web3_client.eth.account.sign_message(encode_defunct(text=timestamp_str_ms), private_key=session_key_priv)
            sig_hex = "0x" + (sig_obj.signature.hex() if hasattr(sig_obj.signature, "hex") else str(sig_obj.signature))
            auth_headers = {
                "accept": "application/json",
                "content-type": "application/json",
                "X-LyraWallet": smart_contract_wallet,
                "X-LyraTimestamp": timestamp_str_ms,
                "X-LyraSignature": sig_hex,
            }
        except Exception:
            pass

    # 2. Query live perpetual and options delta from subaccount clearinghouse
    live_perp_delta = None
    clearinghouse_options_delta = 0.0
    has_clearinghouse_options_delta = False
    subaccount_fetch_ok = False
    
    if auth_headers and subaccount_id:
        for attempt in range(2):
            try:
                sub_resp = await http_client.post(
                    "https://api.lyra.finance/private/get_subaccount", 
                    json={"subaccount_id": subaccount_id}, 
                    headers=auth_headers, 
                    timeout=8.0
                )
                if sub_resp.status_code == 200:
                    sub_res = sub_resp.json().get("result", {})
                    live_perp_delta = 0.0
                    for pos in sub_res.get("positions", []):
                        pos_amt = float(pos.get("amount", 0.0))
                        iname = pos.get("instrument_name", "")
                        itype = pos.get("instrument_type", "")
                        if symbol in iname:
                            if itype == "perp":
                                live_perp_delta += pos_amt
                            elif itype == "option":
                                unit_d = float(pos.get("delta") or 0.0)
                                if unit_d != 0.0:
                                    has_clearinghouse_options_delta = True
                                    clearinghouse_options_delta += pos_amt * unit_d
                    subaccount_fetch_ok = True
                    break
            except Exception as e:
                logger.debug(f"Fast hedge subaccount fetch attempt {attempt+1} failed: {e}")
                if attempt == 0:
                    await asyncio.sleep(0.5)

    # Strict Safety Barrier: If we could not verify subaccount positions from Derive, skip fast rehedge
    if not subaccount_fetch_ok or live_perp_delta is None:
        logger.warning("Fast hedge cycle skipped: Live subaccount positions unavailable from Derive clearinghouse. Holding hedge.")
        return cached_metrics, display_table, alerts_log, False

    # 3. Determine live options delta: Authoritative Clearinghouse Delta (with verified cache fallback)
    if has_clearinghouse_options_delta:
        updated_options_delta = round(clearinghouse_options_delta, 4)
    else:
        # Fallback to last known verified options delta from cached metrics / cached legs
        cached_d_str = cached_metrics.get("section_03", {}).get("Aggregate Options Delta", "").split()[0] if "section_03" in cached_metrics else ""
        if cached_d_str and cached_d_str.replace("+", "").replace("-", "").replace(".", "").isdigit():
            updated_options_delta = float(cached_d_str)
        elif cached_legs:
            updated_options_delta = round(sum(float(l.get("amount", 0.0)) * float(l.get("unit_delta", 0.0)) for l in cached_legs), 4)
        else:
            updated_options_delta = 0.0

    live_perp_delta = round(live_perp_delta, 4)

    # 4. Get Instrument specs
    inst_perp_info = {}
    try:
        inst_perp_resp = (await http_client.post("https://api.lyra.finance/public/get_instrument", json={"instrument_name": perp_symbol}, timeout=3.5)).json()
        inst_perp_info = inst_perp_resp.get("result", {})
    except Exception:
        pass
    size_decimals, min_order_size = get_asset_precision_and_min_size(symbol, inst_perp_info)

    # 5. Execute fast rebalance if delta drift > hedge_band
    live_perp_delta, hedge_action_status, perp_order_summary, rebalanced = await execute_perp_rebalance(
        config=config,
        http_client=http_client,
        symbol=symbol,
        spot_price=spot_price,
        live_options_delta=updated_options_delta,
        live_perp_delta=live_perp_delta,
        auth_headers=auth_headers,
        session_key_wallet=session_key_wallet,
        smart_contract_wallet=smart_contract_wallet,
        subaccount_id=subaccount_id,
        inst_perp_info=inst_perp_info,
        size_decimals=size_decimals,
        min_order_size=min_order_size,
        context=context
    )

    if rebalanced:
        logger.info(f"Fast 30s Perpetual Rehedge Executed: {perp_order_summary}")
        # Save position state immediately on fast rehedge
        prev_st = load_previous_position_state()
        pos_dict = prev_st.get("positions", {}).copy()
        pos_dict[perp_symbol] = live_perp_delta
        save_position_state({
            **prev_st,
            "positions": pos_dict,
            "perp_delta": live_perp_delta,
            "options_delta": updated_options_delta,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        })
        # Send Telegram notification for the rehedge event
        alert_msg = (
            f"⚡ *[FAST 30S PERPETUAL REHEDGE EXECUTED]* ⚡\n"
            f"*Asset*: `{config.trading_pair}` | *Spot*: `${spot_price:,.2f}`\n\n"
            f"• *Options Delta*: `{updated_options_delta:+.4f} {symbol}`\n"
            f"• *New Perp Delta*: `{live_perp_delta:+.4f} {symbol}`\n"
            f"• *Order*: `{perp_order_summary}`\n"
            f"• *Hedge Status*: `{hedge_action_status}`"
        )
        await send_telegram_alert(context, config.telegram_chat_id, alert_msg, http_client)
        alerts_log.append({
            "Event": "Fast 30s Perp Rehedge",
            "Asset": config.trading_pair,
            "Margin Utilization": cached_metrics.get("section_01", {}).get("Margin Utilization", "-"),
            "Options Delta": f"{updated_options_delta:+.4f} {symbol}",
            "Perp Delta": f"{live_perp_delta:+.4f} {symbol}",
            "Status": "FILLED & DELIVERED"
        })

    # Update cached metrics
    if "section_02" in cached_metrics:
        cached_metrics["section_02"][f"{symbol} Spot Price"] = f"${spot_price:,.2f}"
    if "section_03" in cached_metrics:
        cached_metrics["section_03"]["Aggregate Options Delta"] = f"{updated_options_delta:+.4f} {symbol}"
        cached_metrics["section_03"]["Current Perpetual Delta"] = f"{live_perp_delta:+.4f} {symbol}"
        cached_metrics["section_03"]["Target Perpetual Hedge"] = f"{-updated_options_delta:+.4f} {symbol}"
        cached_metrics["section_03"]["Delta Rebalance Order"] = perp_order_summary
        cached_metrics["section_03"]["Hedge Compliance Status"] = hedge_action_status

    return cached_metrics, display_table, alerts_log, rebalanced

async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Continuous decoupled dual-cadence loop runner: Derivatives Monkey consensus cross-check, 30s fast perpetual delta hedge sub-loop, and 300s macro options RFQ cycle."""
    chat_id = context._chat_id if hasattr(context, "_chat_id") else None

    # Setup LiveReport
    report = LiveReport(
        "Derive Autonomous Volatility Loop Trader",
        source_name="derive_volatility_loop_trader",
        tags=["derive", "options", "continuous", "derivatives_monkey_consensus", "target_delta_selection", "dual_cadence_30s_hedge", "hedge_only_mode", "tight_slippage_50bps", "multi_tick_rfq", "non_blocking_async", "atomic_writes", "delta_cap", "take_profit", "telegram"],
        auto_refresh_seconds=30,
    )

    macro_cycle_count = 0
    fast_sub_cycle_count = 0
    last_macro_time = 0.0
    cached_metrics = {}
    cached_display_table = []
    cached_alerts_log = []
    cached_legs = []

    try:
        limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)
        timeout = httpx.Timeout(10.0, connect=5.0)
        async with httpx.AsyncClient(limits=limits, timeout=timeout) as http_client:
            while True:
                now = time.time()
                time_since_macro = now - last_macro_time
                
                if time_since_macro >= config.poll_interval_seconds or last_macro_time == 0.0:
                    macro_cycle_count += 1
                    try:
                        metrics_dict, display_table, alerts_log, summary_text, active_legs = await execute_cycle(config, context, http_client)
                        cached_metrics = metrics_dict
                        cached_display_table = display_table
                        cached_alerts_log = alerts_log
                        cached_legs = active_legs
                        last_macro_time = time.time()
                    except Exception as e:
                        logger.error(f"Error in macro volatility cycle #{macro_cycle_count}: {e}", exc_info=True)
                        err_timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        # 1. Dispatch automated Telegram error alert
                        await send_telegram_error_alert(
                            context=context,
                            chat_id=config.telegram_chat_id,
                            error_title="MACRO VOLATILITY CYCLE ERROR",
                            error=e,
                            cycle_number=macro_cycle_count,
                            cycle_type="Macro Cycle",
                            pair=config.trading_pair,
                            client=http_client,
                            extra_info=f"Macro cycle #{macro_cycle_count} failed. Routine remains resilient and will retry next cycle."
                        )
                        # 2. Append to alerts log for UI audit trail
                        cached_alerts_log.append({
                            "Event": f"Macro Cycle #{macro_cycle_count} Error",
                            "Asset": config.trading_pair,
                            "Margin Utilization": cached_metrics.get("section_01", {}).get("Margin Utilization", "-"),
                            "Options Delta": cached_metrics.get("section_03", {}).get("Aggregate Options Delta", "-"),
                            "Perp Delta": cached_metrics.get("section_03", {}).get("Current Perpetual Delta", "-"),
                            "Status": f"ERROR: {type(e).__name__} ({str(e)[:40]}...)"
                        })
                        # 3. Update heartbeat with degraded status
                        save_heartbeat({
                            "trading_pair": config.trading_pair,
                            "status": "degraded",
                            "macro_cycle": macro_cycle_count,
                            "last_error": f"{type(e).__name__}: {str(e)}",
                            "last_error_time": err_timestamp
                        })
                        # Advance macro timer so we do not busy-spin on failure
                        last_macro_time = time.time()
                else:
                    # Execute fast 30s perpetual delta rebalance sub-cycle
                    fast_sub_cycle_count += 1
                    try:
                        cached_metrics, cached_display_table, cached_alerts_log, rebalanced = await execute_fast_perp_hedge_cycle(
                            config=config,
                            context=context,
                            http_client=http_client,
                            cached_legs=cached_legs,
                            cached_metrics=cached_metrics,
                            display_table=cached_display_table,
                            alerts_log=cached_alerts_log,
                            sub_cycle_count=fast_sub_cycle_count
                        )
                    except Exception as e:
                        logger.error(f"Error in fast 30s perpetual hedge sub-cycle #{fast_sub_cycle_count}: {e}", exc_info=True)
                        err_timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        # 1. Dispatch automated Telegram error alert
                        await send_telegram_error_alert(
                            context=context,
                            chat_id=config.telegram_chat_id,
                            error_title="FAST PERPETUAL HEDGE CYCLE ERROR",
                            error=e,
                            cycle_number=fast_sub_cycle_count,
                            cycle_type="Fast Hedge Sub-Cycle",
                            pair=config.trading_pair,
                            client=http_client,
                            extra_info=f"Fast hedge #{fast_sub_cycle_count} failed. Sub-loop continuing on next 30s cadence.",
                            throttle_seconds=60.0
                        )
                        # 2. Append to alerts log
                        cached_alerts_log.append({
                            "Event": f"Fast Hedge #{fast_sub_cycle_count} Error",
                            "Asset": config.trading_pair,
                            "Margin Utilization": cached_metrics.get("section_01", {}).get("Margin Utilization", "-"),
                            "Options Delta": cached_metrics.get("section_03", {}).get("Aggregate Options Delta", "-"),
                            "Perp Delta": cached_metrics.get("section_03", {}).get("Current Perpetual Delta", "-"),
                            "Status": f"ERROR: {type(e).__name__} ({str(e)[:40]}...)"
                        })
                        # 3. Update heartbeat
                        save_heartbeat({
                            "trading_pair": config.trading_pair,
                            "status": "degraded",
                            "fast_hedge_cycle": fast_sub_cycle_count,
                            "last_error": f"{type(e).__name__}: {str(e)}",
                            "last_error_time": err_timestamp
                        })

                # Update LiveReport dashboard every 30s
                if cached_metrics:
                    try:
                        report.clear()
                        report.builder.manual_order()
                        
                        report.builder.section("01 / DYNAMIC AUTONOMOUS STATUS", f"Real-Time Risk & Margin Assessment (Macro: {config.poll_interval_seconds}s | Sub-Loop: {config.perp_hedge_interval_seconds}s | Macro Cycle #{macro_cycle_count} | Fast Hedge #{fast_sub_cycle_count})")
                        for k, v in cached_metrics.get("section_01", {}).items():
                            report.builder.kpi(k, v)
                            
                        report.builder.section("02 / REAL SURFACE GEX & DERIVATIVES MONKEY CONSENSUS", "Live Derive Analytics & External Intelligence Cross-Check")
                        for k, v in cached_metrics.get("section_02", {}).items():
                            report.builder.kpi(k, v)

                        report.builder.section("03 / DECOUPLED PERPETUAL DELTA HEDGE (30S SUB-LOOP)", "Continuous Rebalancing Against Gamma Drift")
                        for k, v in cached_metrics.get("section_03", {}).items():
                            report.builder.kpi(k, v)

                        sub_label = cached_metrics.get("subaccount_id") or "50061"
                        report.builder.section("04 / ACTIVE CONTRACTS & DELTA BREAKDOWN", f"Live Derive Subaccount #{sub_label}")
                        report.builder.table(cached_display_table, ["Leg", "Contract", "Strike", "Size", "Delta"])

                        report.builder.section("05 / AUDIT LOG", "Continuous Event Records")
                        report.builder.table(cached_alerts_log, ["Event", "Asset", "Margin Utilization", "Options Delta", "Perp Delta", "Status"])

                        await report.update()
                    except Exception as e:
                        logger.warning(f"Error updating LiveReport: {e}")

                await asyncio.sleep(config.perp_hedge_interval_seconds)
            
    except asyncio.CancelledError:
        if report.report_id is not None:
            report.clear()
            report.builder.auto_refresh(None)
            report.builder.section("MONITOR STOPPED", "Autonomous Trader Stopped")
            await report.update()
        return f"Derive Volatility Loop Trader stopped after {macro_cycle_count} macro cycles."
    except Exception as fatal_e:
        logger.critical(f"FATAL: Unhandled crash in Derive Volatility Loop Trader: {fatal_e}", exc_info=True)
        crash_timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        save_heartbeat({
            "trading_pair": config.trading_pair,
            "status": "crashed",
            "fatal_error": f"{type(fatal_e).__name__}: {str(fatal_e)}",
            "fatal_cycle": f"Macro #{macro_cycle_count}",
            "crash_time": crash_timestamp
        })
        try:
            await send_telegram_error_alert(
                context=context,
                chat_id=config.telegram_chat_id,
                error_title="FATAL ROUTINE CRASH",
                error=fatal_e,
                cycle_number=macro_cycle_count,
                cycle_type="Fatal Crash",
                pair=config.trading_pair,
                extra_info="CRITICAL: Routine crashed with unhandled fatal exception and has terminated!",
                throttle_seconds=0.0
            )
        except Exception as tel_e:
            logger.error(f"Failed to dispatch fatal crash Telegram alert: {tel_e}")
        raise fatal_e

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    parser = argparse.ArgumentParser(description="Derive Autonomous Volatility Loop Trader")
    parser.add_argument("--pair", type=str, default="ETH-USDT", help="Trading pair (e.g. ETH-USDT)")
    parser.add_argument("--paper", action="store_true", help="Run in paper mode")
    parser.add_argument("--hedge-only", action="store_true", help="Run in hedge-only mode (disable new entries)")
    parser.add_argument("--chat-id", type=str, default=None, help="Telegram Chat ID for notifications")
    args = parser.parse_args()

    cfg = Config(
        trading_pair=args.pair,
        paper_mode=args.paper,
        enable_options_entry=not args.hedge_only,
        telegram_chat_id=args.chat_id,
    )
    asyncio.run(run(cfg, None))
