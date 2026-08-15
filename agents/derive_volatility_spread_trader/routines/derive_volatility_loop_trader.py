import math
import json
import logging
import datetime
import os
import time
import asyncio
import httpx
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

class Config(BaseModel):
    """Autonomous 5-Minute Continuous Loop Trader: Fully Non-Blocking Async I/O, Atomic State Writes, Portable Paths & 60% Take-Profit"""
    trading_pair: str = Field(default="ETH-USDT", description="Underlying asset trading pair (ETH-USDT, BTC-USDT, HYPE-USDT)")
    poll_interval_seconds: int = Field(default=300, description="Loop monitoring interval in seconds (5-minute default cadence)")
    paper_mode: bool = Field(default=False, description="Dry-run paper mode (True = Simulated execution & alerts; False = Live exchange orders)")
    dte: int = Field(default=14, description="Target Days to Expiration for newly initiated options structures")
    min_edge: float = Field(default=5.0, description="Minimum net volatility edge required for entry/scale-in (vol points)")
    hedge_band: float = Field(default=0.05, description="Rebalance delta threshold (|Net Delta| > band) triggering perp hedge")
    emergency_band: float = Field(default=0.20, description="Emergency delta threshold triggering market hedge rebalance")
    max_margin_utilization_pct: float = Field(default=75.0, description="Maximum subaccount margin utilization cap (%) beyond which no new options are entered")
    max_perp_delta_hedge_ratio: float = Field(default=1.0, description="Cap multiplier ensuring perpetual delta never exceeds options delta hedge requirement")
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
                return json.load(f)
        except Exception:
            pass
    return {}

def save_position_state(state: dict):
    save_json_atomic(STATE_FILE, state)

def save_heartbeat(data: dict):
    payload = {
        **data,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "timestamp_epoch": datetime.datetime.now(datetime.timezone.utc).timestamp(),
        "status": "healthy"
    }
    save_json_atomic(HEARTBEAT_FILE, payload)

def resolve_derive_credentials(config: Config) -> tuple[str, str, int | None]:
    wallet = (config.smart_contract_wallet or "").strip()
    priv = (config.session_key_priv or "").strip()
    sub_id = config.subaccount_id

    # 1. Fallback to .env / environment variables
    if not wallet:
        wallet = (os.getenv("DERIVE_SMART_CONTRACT_WALLET") or "").strip()
    if not priv:
        priv = (os.getenv("DERIVE_SESSION_KEY_PRIV") or "").strip()
    if sub_id is None:
        raw_env_sub = os.getenv("DERIVE_SUBACCOUNT_ID")
        if raw_env_sub:
            try:
                sub_id = int(str(raw_env_sub).strip())
            except ValueError:
                pass

    # 2. Fallback to Condor / Hummingbot portfolio connector keys with dynamic path discovery
    if not (wallet and priv and sub_id):
        import binascii
        import yaml
        from eth_account import Account

        hummingbot_base = Path(os.getenv("HUMMINGBOT_API_DIR", Path.home() / "hummingbot-api"))
        connector_candidates = [
            hummingbot_base / "bots" / "credentials" / "master_account" / "connectors" / "derive.yml",
            hummingbot_base / "bots" / "credentials" / "master_account" / "connectors" / "derive_perpetual.yml",
            Path("/app/hummingbot-api/bots/credentials/master_account/connectors/derive.yml"),
            Path("/workspace/hummingbot-api/bots/credentials/master_account/connectors/derive.yml"),
        ]
        pwd = os.getenv("CONFIG_PASSWORD")
        if not pwd:
            hb_env_file = hummingbot_base / ".env"
            if hb_env_file.exists():
                try:
                    from dotenv import dotenv_values
                    pwd = dotenv_values(hb_env_file).get("CONFIG_PASSWORD")
                except Exception:
                    pass

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
                                wallet = (dec.get("derive_api_key") or dec.get("derive_perpetual_api_key") or "").strip()
                            if not priv:
                                priv = (dec.get("derive_api_secret") or dec.get("derive_perpetual_api_secret") or "").strip()
                            if sub_id is None and "sub_id" in dec:
                                try:
                                    sub_id = int(dec["sub_id"])
                                except (ValueError, TypeError):
                                    pass
                    except Exception as e:
                        logger.debug(f"Could not load credentials from {p}: {e}")
        else:
            logger.debug("CONFIG_PASSWORD not set in environment or .env; skipping connector decryption.")

    return wallet, priv, sub_id

async def send_telegram_alert(context: ContextTypes.DEFAULT_TYPE, chat_id: str | None, message: str, client: httpx.AsyncClient | None = None):
    target_chat = chat_id or os.environ.get("ADMIN_USER_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not target_chat:
        logger.info("Telegram alert skipped: No chat_id provided and ADMIN_USER_ID/TELEGRAM_CHAT_ID not set in .env")
        return

    if hasattr(context, "bot") and context.bot:
        try:
            await context.bot.send_message(chat_id=target_chat, text=message, parse_mode="Markdown")
            logger.info(f"Telegram notification sent to chat {target_chat} via context.bot")
            return
        except Exception as e:
            logger.warning(f"Failed to send Telegram alert via context.bot: {e}")

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
    if bot_token:
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {"chat_id": target_chat, "text": message, "parse_mode": "Markdown"}
            if client:
                resp = await client.post(url, json=payload, timeout=5.0)
                if resp.status_code == 200:
                    logger.info(f"Telegram notification delivered to chat {target_chat} via async client")
                else:
                    logger.warning(f"Telegram API returned status {resp.status_code}: {resp.text}")
            else:
                async with httpx.AsyncClient(timeout=5.0) as temp_client:
                    resp = await temp_client.post(url, json=payload)
                    if resp.status_code == 200:
                        logger.info(f"Telegram notification delivered to chat {target_chat}")
        except Exception as e:
            logger.warning(f"Failed to send Telegram alert via async HTTP fallback: {e}")
    else:
        logger.info("Telegram direct alert skipped: No TELEGRAM_TOKEN or TELEGRAM_BOT_TOKEN set in .env")

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

async def resolve_actual_entry_credit(client: httpx.AsyncClient, subaccount_id: int, auth_headers: dict, active_legs: list, fallback_spread_unit_credit: float) -> float:
    """Resolve the true net entry credit received when opening the active Iron Condor position."""
    if not auth_headers or not subaccount_id or not active_legs:
        contracts = sum(abs(float(p.get("amount", 0.0))) for p in active_legs if float(p.get("amount", 0.0)) < 0) or 1.0
        return round(fallback_spread_unit_credit * contracts, 2)
        
    try:
        trades_res = (await client.post(
            "https://api.lyra.finance/private/get_trade_history",
            json={"subaccount_id": subaccount_id, "page_size": 20},
            headers=auth_headers,
            timeout=5.0
        )).json().get("result", {}).get("trades", [])
        
        active_inames = {p.get("instrument_name") for p in active_legs}
        
        by_rfq = {}
        for t in trades_res:
            rfq = t.get("rfq_id")
            if rfq:
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
        
    contracts = sum(abs(float(p.get("amount", 0.0))) for p in active_legs if float(p.get("amount", 0.0)) < 0) or 1.0
    return round(fallback_spread_unit_credit * contracts, 2)

async def execute_cycle(config: Config, context: ContextTypes.DEFAULT_TYPE, http_client: httpx.AsyncClient) -> tuple[dict, list, list, str]:
    """Execute a single complete monitoring, real GEX surface analysis, gradual scale-in, exact DTE Greeks, adaptive precision rounding, take-profit evaluation, and execution cycle non-blockingly."""
    timestamp_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    symbol = config.trading_pair.upper().split("-")[0]
    perp_symbol = f"{symbol}-PERP"
    execution_mode_str = "[PAPER MODE - DRY RUN]" if config.paper_mode else "[LIVE ORDER EXECUTION]"
    
    # -------------------------------------------------------------
    # 1. Multi-Venue Spot, Dynamic RV, ATM IV & Real Surface GEX/DEX/Skew
    # -------------------------------------------------------------
    spot_price, iv_14d, rv_7d, dealer_gex_m, dealer_dex_m, iv_skew_pts, block_rfq_bias, gex_conviction = await fetch_dynamic_market_volatility(http_client, config.trading_pair, config.dte)
    
    r = 0.03
    raw_vol_premium = iv_14d - rv_7d
    friction_cost = 2.50
    net_edge = raw_vol_premium - friction_cost
    
    edge_open = net_edge >= config.min_edge
    signal_status = "ACTIVE (ENTRY THRESHOLD MET)" if edge_open else "INACTIVE (EDGE BELOW THRESHOLD)"
    regime_mode = f"Short Volatility (Defined-Risk Iron Condor)" if edge_open else "Neutral / Standby"
    
    sigma = iv_14d / 100.0
    T_entry = config.dte / 365.0
    
    put_skew_offset = max(0.0, (iv_skew_pts / 100.0) * 0.10)
    raw_k_sp = spot_price * math.exp(-(0.45 + put_skew_offset) * sigma * math.sqrt(T_entry))
    raw_k_sc = spot_price * math.exp(0.45 * sigma * math.sqrt(T_entry))
    raw_k_lp = spot_price * math.exp(-(1.15 + put_skew_offset) * sigma * math.sqrt(T_entry))
    raw_k_lc = spot_price * math.exp(1.15 * sigma * math.sqrt(T_entry))
    
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
    # 2. Derive Credentials & Subaccount Resolution
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
                            active_options_legs_raw.append({**pos, "exact_dte": contract_dte, "exact_T": contract_T, "unit_delta": unit_d})
                            
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
    
    if has_eth_ic_open:
        for p in eth_opt_legs:
            iname = p.get("instrument_name", "")
            amt = float(p.get("amount", 0.0))
            leg_mark = 0.0
            try:
                t_resp = (await http_client.post("https://api.lyra.finance/public/get_ticker", json={"instrument_name": iname}, timeout=3.5)).json().get("result", {})
                leg_mark = float(t_resp.get("mark_price") or 0.0)
            except Exception:
                pass
            current_cost_to_close += (-amt) * leg_mark

    current_cost_to_close = round(max(0.0, current_cost_to_close), 2)
    unrealized_pnl = round(entry_credit_usd - current_cost_to_close, 2)
    profit_captured_pct = round((unrealized_pnl / entry_credit_usd * 100.0), 2) if entry_credit_usd > 0 else 0.0
    
    take_profit_triggered = has_eth_ic_open and (profit_captured_pct >= config.target_profit_pct) and (unrealized_pnl >= 0.60 * entry_credit_usd) and (unrealized_pnl > 3.0)
    stop_loss_triggered = has_eth_ic_open and (unrealized_pnl <= -(config.stop_loss_pct / 100.0 * entry_credit_usd))
    
    options_execution_status = f"HOLDING POSITION (PnL: +${unrealized_pnl:,.2f} | {profit_captured_pct:.1f}% of ${entry_credit_usd:.2f} credit | Target: {config.target_profit_pct:.1f}%)" if has_eth_ic_open else "NO ACTIVE OPTIONS (MONITORING ENTRY)"
    position_change_occurred = False
    change_event_details = []

    # -------------------------------------------------------------
    # 6. Automated Take-Profit (>=60%) or Stop-Loss Unwind Execution
    # -------------------------------------------------------------
    if (take_profit_triggered or stop_loss_triggered) and not config.paper_mode and session_key_wallet and smart_contract_wallet and subaccount_id:
        trigger_name = "TAKE-PROFIT (>=60% Captured)" if take_profit_triggered else "STOP-LOSS"
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
                    await asyncio.sleep(2)
                    poll_resp = await http_client.post("https://api.lyra.finance/private/poll_quotes", json={"subaccount_id": subaccount_id, "status": "open"}, headers=auth_headers, timeout=5.0)
                    quotes = poll_resp.json().get("result", {}).get("quotes", [])
                    matching_quotes = [q for q in quotes if q.get("rfq_id") == close_rfq_id and q.get("direction") == "sell"]
                    if matching_quotes:
                        best_quote = matching_quotes[0]
                        
                        prices_mapped = map_rfq_quote_prices(rfq_close_data.legs, best_quote.get("legs", []))
                        if prices_mapped:
                            action = SignedAction(subaccount_id=subaccount_id, owner=smart_contract_wallet, signer=session_key_wallet.address, signature_expiry_sec=utils.MAX_INT_32, nonce=utils.get_action_nonce(), module_address=RFQ_MODULE_ADDRESS, module_data=rfq_close_data, DOMAIN_SEPARATOR=DOMAIN_SEPARATOR, ACTION_TYPEHASH=ACTION_TYPEHASH)
                            action.sign(session_key_wallet.key)
                            
                            exec_resp = await http_client.post("https://api.lyra.finance/private/execute_quote", json={**action.to_json(), "label": f"{symbol}-IC-CLOSE-TP", "rfq_id": best_quote["rfq_id"], "quote_id": best_quote["quote_id"]}, headers=auth_headers, timeout=5.0)
                            if exec_resp.json().get("result", {}).get("status") == "filled":
                                options_execution_status = f"CLOSED VIA {trigger_name} (+${unrealized_pnl:,.2f} Realized PnL | {profit_captured_pct:.1f}% Captured)"
                                position_change_occurred = True
                                change_event_details.append(f"Options Package Closed via {trigger_name} (+${unrealized_pnl:,.2f})")
            
            # Flatten Perpetual Hedge Position to 0.00
            if live_perp_delta != 0.0:
                is_perp_long = live_perp_delta > 0
                flat_side = "sell" if is_perp_long else "buy"
                flat_size = round(abs(live_perp_delta), size_decimals)
                dyn_limit_price = Decimal(str(round(spot_price * 0.90 if is_perp_long else spot_price * 1.10, 2)))
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

        except Exception as e:
            logger.error(f"Error during automated exit execution: {e}")
            options_execution_status = f"EXIT TRIGGER FAILED: {e}"

    # -------------------------------------------------------------
    # 7. Dynamic Volatility Scale-In (Gradual Scaling while Margin Util < 75%)
    # -------------------------------------------------------------
    can_scale_in = edge_open and (margin_utilization_pct < config.max_margin_utilization_pct) and (margin_headroom_usd >= 25.0) and not (take_profit_triggered or stop_loss_triggered)
    
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
                    
                    def find_closest(opts, target_strike):
                        return min(opts, key=lambda x: abs(float(x.get("option_details", {}).get("strike", 0)) - target_strike))
                    
                    inst_sc = find_closest(calls, k_short_call)
                    inst_sp = find_closest(puts, k_short_put)
                    inst_lc = find_closest(calls, k_long_call)
                    inst_lp = find_closest(puts, k_long_put)
                    
                    wing_width = abs(k_long_call - k_short_call) or 100.0
                    
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
                            
                        await asyncio.sleep(2)
                        poll_resp = await http_client.post("https://api.lyra.finance/private/poll_quotes", json={"subaccount_id": subaccount_id, "status": "open"}, headers=auth_headers, timeout=5.0)
                        quotes = poll_resp.json().get("result", {}).get("quotes", [])
                        matching_quotes = [q for q in quotes if q.get("rfq_id") == live_rfq_id and q.get("direction") == "sell"]
                        if matching_quotes:
                            best_quote = matching_quotes[0]
                            
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
                    logger.error(f"Scale-in RFQ error: {e}")

    # -------------------------------------------------------------
    # 8. Strict Perpetual Delta Hedge & Adaptive Precision Execution
    # -------------------------------------------------------------
    target_perp_delta = - live_options_delta
    max_allowed_perp_delta = max(min_order_size, abs(live_options_delta) * config.max_perp_delta_hedge_ratio)
    
    delta_imbalance = target_perp_delta - live_perp_delta
    hedge_action_status = f"DELTA NEUTRAL (|Imbalance| {abs(delta_imbalance):.4f} <= {config.hedge_band:.2f})"
    perp_order_summary = "None (Aligned with Options Delta Hedge Cap)"
    
    is_overhedged = live_perp_delta > (max_allowed_perp_delta + 0.05) or live_perp_delta < (-max_allowed_perp_delta - 0.05)
    
    if (is_overhedged or abs(delta_imbalance) > config.hedge_band) and not (take_profit_triggered or stop_loss_triggered):
        rebalance_size = round(abs(delta_imbalance), size_decimals)
        if rebalance_size >= min_order_size:
            is_sell = delta_imbalance < 0
            side_str = "SELL" if is_sell else "BUY"
            hedge_action_status = f"PERPETUAL DELTA REBALANCE ({side_str} {rebalance_size:.{size_decimals}f} {perp_symbol})"
            perp_order_summary = f"{execution_mode_str} Submit {side_str} {rebalance_size:.{size_decimals}f} {perp_symbol} (Target: {target_perp_delta:+.4f} {symbol})"
            
            if not config.paper_mode:
                if not (session_key_wallet and smart_contract_wallet and subaccount_id):
                    perp_order_summary += " | Skipped (Credentials missing in .env)"
                else:
                    try:
                        dyn_limit_price = Decimal(str(round(spot_price * 0.90 if is_sell else spot_price * 1.10, 2)))
                        trade_data = TradeModuleData(
                            asset_address=inst_perp_info.get("base_asset_address", "0x0000000000000000000000000000000000000000"),
                            sub_id=int(inst_perp_info.get("base_asset_sub_id", 0)),
                            limit_price=dyn_limit_price,
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
                                "order_type": "market",
                                "reduce_only": is_overhedged,
                                "time_in_force": "ioc",
                                "label": "rebalance-perp"
                            },
                            headers=auth_headers,
                            timeout=5.0
                        )
                        res_order = order_resp.json().get("result", {})
                        if res_order.get("order", {}).get("order_status") == "filled":
                            perp_order_summary += f" | FILLED @ ${float(res_order['order']['average_price']):,.2f}"
                            live_perp_delta += (-rebalance_size if is_sell else rebalance_size)
                            position_change_occurred = True
                            change_event_details.append(f"Perpetual Delta Rebalance: {side_str} {rebalance_size:.{size_decimals}f} {perp_symbol}")
                    except Exception as e:
                        logger.error(f"Perp rebalance execution error: {e}")
                        perp_order_summary += f" | Error: {e}"

    # -------------------------------------------------------------
    # 9. Robust Position Change Detection (With Float Tolerance) & Telegram Notification
    # -------------------------------------------------------------
    material_position_change = has_position_state_materially_changed(current_positions_summary, prev_state.get("positions", {}))
    if material_position_change:
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
            "timestamp": timestamp_str
        })
        
    alerts_log = []
    
    if position_change_occurred or not config.notify_on_change_only:
        telegram_event_title = "⚡ *[POSITION CHANGE EXECUTED]* ⚡" if position_change_occurred else "⏱️ *[5-Min Monitoring Cycle]* ⏱️"
        cadence_alert_msg = (
            f"{telegram_event_title}\n"
            f"*Asset*: `{config.trading_pair}` | *Spot*: `${spot_price:,.2f}`\n\n"
            f"• *Vol Edge*: `{net_edge:.2f} pts` (IV {iv_14d:.1f}% vs RV {rv_7d:.1f}%)\n"
            f"• *Market GEX*: `${dealer_gex_m:+.3f}M` | *DEX*: `${dealer_dex_m:+.3f}M`\n"
            f"• *25D IV Skew*: `{iv_skew_pts:+.2f} pts` | *Flow Bias*: `{block_rfq_bias}`\n"
            f"• *Net Entry Credit*: `${entry_credit_usd:,.2f}` | *Cost to Close*: `${current_cost_to_close:,.2f}`\n"
            f"• *Unrealized PnL*: `+${unrealized_pnl:,.2f}` (*{profit_captured_pct:.1f}%* / Target: `{config.target_profit_pct:.1f}%`)\n"
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
        {"Leg": "Short Call", "Contract": f"{symbol}-{exp_str}-{int(k_short_call)}-C", "Strike": f"${k_short_call:.0f}", "Size": f"-{contracts:.{size_decimals}f}", "Delta": f"{delta_sc:+.4f}"},
        {"Leg": "Short Put", "Contract": f"{symbol}-{exp_str}-{int(k_short_put)}-P", "Strike": f"${k_short_put:.0f}", "Size": f"-{contracts:.{size_decimals}f}", "Delta": f"{delta_sp:+.4f}"},
        {"Leg": "Long Call Wing", "Contract": f"{symbol}-{exp_str}-{int(k_long_call)}-C", "Strike": f"${k_long_call:.0f}", "Size": f"+{contracts:.{size_decimals}f}", "Delta": f"{delta_lc:+.4f}"},
        {"Leg": "Long Put Wing", "Contract": f"{symbol}-{exp_str}-{int(k_long_put)}-P", "Strike": f"${k_long_put:.0f}", "Size": f"+{contracts:.{size_decimals}f}", "Delta": f"{delta_lp:+.4f}"},
    ]
    display_table = live_active_positions_table if live_active_positions_table else strikes_table

    metrics_dict = {
        "subaccount_id": subaccount_id,
        "section_01": {
            "Execution Mode": execution_mode_str,
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
            "Real Dealer GEX": f"${dealer_gex_m:+.3f}M",
            "Real Dealer DEX": f"${dealer_dex_m:+.3f}M",
            "Real 25D IV Skew": f"{iv_skew_pts:+.2f} pts",
            "Market Flow Bias": block_rfq_bias,
            "GEX Regime": gex_conviction,
            "Net Entry Credit": f"${entry_credit_usd:.2f}",
            "Cost to Close ($)": f"${current_cost_to_close:.2f}",
            "Unrealized PnL": f"+${unrealized_pnl:,.2f}",
            "Profit Captured": f"{profit_captured_pct:.1f}% (Target: {config.target_profit_pct:.1f}%)",
            "Options Strategy Status": options_execution_status,
        },
        "section_03": {
            "Aggregate Options Delta": f"{live_options_delta:+.4f} {symbol}",
            "Current Perpetual Delta": f"{live_perp_delta:+.4f} {symbol}",
            "Target Perpetual Hedge": f"{target_perp_delta:+.4f} {symbol}",
            "Max Allowed Perp Cap": f"{max_allowed_perp_delta:+.4f} {symbol}",
            "Delta Rebalance Order": perp_order_summary,
            "Hedge Compliance Status": hedge_action_status,
        }
    }

    summary_text = (
        f"Derive 5-Minute Cadence Autonomous Volatility Loop Trader executed.\n"
        f"- Mode: {execution_mode_str} | Asset: {config.trading_pair} (${spot_price:,.2f})\n"
        f"- Vol Edge: {net_edge:.2f} pts | Real GEX: ${dealer_gex_m:+.3f}M | 25D Skew: {iv_skew_pts:+.2f} pts ({block_rfq_bias})\n"
        f"- Net Entry Credit: ${entry_credit_usd:.2f} | Cost to Close: ${current_cost_to_close:.2f} | Unrealized PnL: +${unrealized_pnl:,.2f} ({profit_captured_pct:.1f}% captured)\n"
        f"- Subaccount: #{subaccount_id} | Subaccount Value: ${subaccount_value:,.2f} | Collateral: ${collateral:,.2f}\n"
        f"- Margin Utilization: {margin_utilization_pct:.2f}% (Cap: {config.max_margin_utilization_pct:.1f}% | Headroom: ${margin_headroom_usd:,.2f})\n"
        f"- Buying Power: ${buying_power:,.2f} | Positions Margin Used: ${positions_margin_used:,.2f}\n"
        f"- Options Delta: {live_options_delta:+.4f} {symbol} | Current Perp Delta: {live_perp_delta:+.4f} {symbol}\n"
        f"- Perpetual Delta Hedge Cap: Target {target_perp_delta:+.4f} {symbol} (Max Cap: {max_allowed_perp_delta:+.4f} {symbol})\n"
        f"- Options Action: {options_execution_status}\n"
        f"- Perpetual Action: {perp_order_summary}\n"
        f"- Telegram Notification: {'SENT (Position Change Triggered)' if position_change_occurred else 'SKIPPED (No Position Change)'}"
    )

    return metrics_dict, display_table, alerts_log, summary_text

async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Continuous 5-minute autonomous volatility loop runner with persistent non-blocking HTTP client and atomic state writes."""
    chat_id = context._chat_id if hasattr(context, "_chat_id") else None

    # Setup LiveReport
    report = LiveReport(
        "Derive Autonomous Volatility Loop Trader",
        source_name="derive_volatility_loop_trader",
        tags=["derive", "options", "continuous", "non_blocking_async", "atomic_writes", "noise_free_alerts", "adaptive_precision", "exact_contract_dte", "delta_cap", "take_profit", "telegram"],
        auto_refresh_seconds=60,
    )

    cycle_count = 0
    try:
        limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)
        timeout = httpx.Timeout(10.0, connect=5.0)
        async with httpx.AsyncClient(limits=limits, timeout=timeout) as http_client:
            while True:
                cycle_count += 1
                try:
                    metrics_dict, display_table, alerts_log, summary_text = await execute_cycle(config, context, http_client)
                    
                    # Update LiveReport dashboard
                    report.clear()
                    report.builder.manual_order()
                    
                    report.builder.section("01 / DYNAMIC AUTONOMOUS STATUS", f"Real-Time Risk & Margin Assessment (Cadence: {config.poll_interval_seconds}s | Cycle #{cycle_count})")
                    for k, v in metrics_dict.get("section_01", {}).items():
                        report.builder.kpi(k, v)
                        
                    report.builder.section("02 / REAL SURFACE GEX & TAKE-PROFIT TRACKER", "Live Derive Surface Analytics & PnL Monitoring")
                    for k, v in metrics_dict.get("section_02", {}).items():
                        report.builder.kpi(k, v)

                    report.builder.section("03 / STRICT PERPETUAL DELTA HEDGE & CAP COMPLIANCE", "Enforces Perp Delta <= Options Delta Hedge Requirement")
                    for k, v in metrics_dict.get("section_03", {}).items():
                        report.builder.kpi(k, v)

                    sub_label = metrics_dict.get("subaccount_id") or "50061"
                    report.builder.section("04 / ACTIVE CONTRACTS & DELTA BREAKDOWN", f"Live Derive Subaccount #{sub_label}")
                    report.builder.table(display_table, ["Leg", "Contract", "Strike", "Size", "Delta"])

                    report.builder.section("05 / AUDIT LOG", "Continuous Event Records")
                    report.builder.table(alerts_log, ["Event", "Asset", "Margin Utilization", "Options Delta", "Perp Delta", "Status"])

                    await report.update()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Error in volatility loop cycle #{cycle_count}: {e}")
                
                await asyncio.sleep(config.poll_interval_seconds)
            
    except asyncio.CancelledError:
        if report.report_id is not None:
            report.clear()
            report.builder.auto_refresh(None)
            report.builder.section("MONITOR STOPPED", "Autonomous Trader Stopped")
            await report.update()
        return f"Derive Volatility Loop Trader stopped after {cycle_count} cycles."
