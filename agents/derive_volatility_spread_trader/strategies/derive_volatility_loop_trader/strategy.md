---
name: Derive Volatility Loop Trader
description: Autonomous decoupled dual-cadence short-volatility Iron Condor strategy
  on Derive with Black-Scholes continuous delta modeling (30Δ/10Δ), Derivatives Monkey
  institutional GEX/DEX consensus filtering, multi-source credential resolution, pre-trade
  projected margin checks (75% cap), discrete complete package sizing (100%, 75%,
  50%, 25%), 30s fast perpetual delta hedging with 50 bps slippage protection, and atomic
  state tracking.
agent_key: null
skills: []
default_config:
  capital_allocation: 10000.0
  contract_size: 1.0
  dte: 14
  emergency_band: 0.2
  enable_options_entry: true
  frequency_sec: 60
  hedge_band: 0.05
  max_margin_utilization_pct: 75.0
  max_perp_delta_hedge_ratio: 1.0
  max_perp_slippage_bps: 50
  min_edge: 5.0
  notify_on_change_only: true
  options_route: RFQ_COMBO_PACKAGE
  paper_mode: false
  perp_hedge_interval_seconds: 30
  poll_interval_seconds: 300
  short_delta_target: 0.3
  stop_loss_pct: 100.0
  target_profit_pct: 60.0
  trading_pair: ETH-USDT
  use_derivatives_monkey_intel: true
  wing_delta_target: 0.1
default_trading_context: ''
created_by: 1934595831
created_at: '2026-08-14T10:35:34.048157+00:00'
---

# Derive Volatility Loop Trader — Strategy Playbook

Autonomous Decoupled Dual-Cadence Short-Volatility Iron Condor & Delta Hedging Strategy on Derive (Lyra Finance).

---

## 1. Strategy Overview & Architecture

The strategy monetizes the volatility risk premium between Implied Volatility (IV) and Realized Volatility (RV) by systematically writing defined-risk 4-leg Iron Condors on Derive options while maintaining strict delta neutrality using dynamic perpetual futures hedging.

### Core Architectural Pillars
- **Decoupled Dual-Cadence Execution**:
  - **Macro Cycle (`300s` / 5m)**: Volatility surface analysis, 7D RV vs 14D IV Black-Scholes inversion, Derivatives Monkey institutional consensus filtering, discrete package sizing, RFQ entry/scale-in, and lifecycle take-profit evaluation.
  - **Fast Sub-Loop (`30s`)**: High-frequency perpetual delta rebalancing and spot price tracking to prevent gamma drift between macro option cycles.
- **Execution Route**: Atomic RFQ Combo Packages via EIP-712 session key signing (`derive_action_signing`) with fallback to sequential orderbook.
- **Venue & Primary Underlyings**: Derive Protocol (`ETH-USDT`, `BTC-USDT`, `HYPE-USDT`).

---

## 2. Quantitative Model & Signal Triggers

### 2.1 Dynamic Volatility Engine
- **Live Spot Price ($S_0$)**: Fetched in real time from Derive `/public/get_ticker` (`{SYMBOL}-PERP`).
- **Dynamic Implied Volatility ($\text{IV}_{14\text{D}}$)**: Discovers live ATM options on Derive `/public/get_instruments`, reads live mark prices, and inverts the Black-Scholes pricing formula via Newton-Raphson:
  $$\text{BS\_Price}(S_0, K_{\text{ATM}}, T, r=0.03, \sigma_{\text{IV}}) = \text{Mark Price} \implies \sigma_{\text{IV}}$$
- **Dynamic 7-Day Realized Volatility ($\text{RV}_{7\text{D}}$)**: Fetches trailing 168 1-hour candle closes from Binance / Hyperliquid / CoinGecko REST APIs and computes annualized variance:
  $$\text{RV}_{7\text{D}} = \sqrt{\frac{24 \times 365}{N-1} \sum_{t=1}^N \left(\ln\frac{S_t}{S_{t-1}} - \bar{r}\right)^2} \times 100\%$$
- **Net Volatility Edge**:
  $$\text{Net Edge} = (\text{IV}_{14\text{D}} - \text{RV}_{7\text{D}}) - 2.50\text{ pts}$$
- **Entry / Scale-In Threshold**: $\text{Net Edge} \ge 5.0\text{ vol points}$.

### 2.2 Derivatives Monkey Institutional Consensus & Precedence Rules
On each macro cycle, the engine queries institutional surface signals via `derivatives_monkey_extractor.py --asset <ASSET> --json` (GEX, DEX, Gamma Flip, Term Structure, Put/Call Ratio, and AI Confidence):
$$\text{GEX} = \sum_{K} \text{OI}_K \cdot \Gamma_K \cdot S^2 \cdot 0.01$$

#### Precedence Rule & Anti-Blowup Guard:
Options entry and scale-in are **strictly blocked** if any of the following adverse conditions occur:
1. **Negative Dealer GEX** ($< -\$2.0\text{M}$): Signals high volatility expansion regime.
2. **Spot Below Gamma Flip Level** ($S_0 < \text{Flip Level}$): Signals dealer selling into downturns.
3. **Deep Backwardation**: Signals extreme near-term panic / inverted term structure.
4. **Low Monkey AI Confidence** ($< 65\%$): Signals conflicting institutional flows.

*Under blocked conditions, the strategy operates in **Hedge-Only Mode** (managing and delta-hedging open inventory without taking new short-gamma risk).*

---

## 3. Pure Target Delta Strike Selection ($30\Delta / 10\Delta$)

Strikes are selected via continuous Black-Scholes inversion using the Abramowitz & Stegun rational approximation for the inverse standard normal CDF ($\text{norm\_inv}$), and matched to live Derive option tickers minimizing absolute Delta error ($\min |\Delta_{\text{actual}} - \Delta_{\text{target}}|$):

1. **Short Call**: Target $+0.30\Delta$ ($\approx 30\text{ Delta}$)
2. **Short Put**: Target $-0.30\Delta$ ($\approx -30\text{ Delta}$)
3. **Long Call Wing**: Target $+0.10\Delta$ ($\approx 10\text{ Delta}$ outer wing protection)
4. **Long Put Wing**: Target $-0.10\Delta$ ($\approx -10\text{ Delta}$ outer wing protection)

### Strict Structural Constraints:
- **Strict Single-Tenor Expiry Rule**: All 4 legs of the Iron Condor must belong strictly to the **same single expiration timestamp** (default `14 DTE`, e.g., `Aug 28, 2026 08:00 UTC`).
- **Asset Precision & Min Sizes**: Automatically scales size precision and order minimums per underlying (e.g. BTC: 4 decimals, 0.01 min; ETH: 3 decimals, 0.10 min; HYPE: 1 decimal, 1.0 min).

---

## 4. Discrete Sizing & Margin Safety Gates

### 4.1 Discrete Complete Package Sizing
Options are never entered as split or unbalanced legs. Sizing evaluates discrete complete packages based on base `contract_size`:
- **100% Package**: `1.00x` base size across all 4 legs
- **75% Package**: `0.75x` base size across all 4 legs
- **50% Package**: `0.50x` base size across all 4 legs
- **25% Package**: `0.25x` base size across all 4 legs

### 4.2 Pre-Trade Margin Utilization Projection
$$\text{Projected Margin Used} = \text{Current Positions Margin} + (\text{Wing Width} \times \text{Package Size})$$
$$\text{Projected Margin Utilization} = \frac{\text{Projected Margin Used}}{\text{Subaccount Total Collateral}} \times 100\%$$
- **Hard Safety Gate**: Package is submitted **ONLY IF** $\text{Projected Margin Utilization} \le \mathbf{75.0\%}$ AND $\text{Est. Package Margin} \le \text{Buying Power}$.
- If a larger tier breaches $75\%$, the engine downscales to the next tier ($75\% \to 50\% \to 25\%$). If no tier fits, scale-in pauses (`SCALE-IN CAPPED`).

---

## 5. Decoupled Fast Perpetual Delta Hedging

- **Aggregate Options Delta**: $\Delta_{\text{options}} = \sum (\text{Leg Amount} \times \Delta_{\text{leg}})$
- **Target Perp Hedge**: $\Delta_{\text{perp}}^{\text{target}} = - \Delta_{\text{options}}$
- **Strict Perp Ratio Cap**: Total perpetual delta must NEVER exceed options delta hedge requirement ($\text{Max Perp Delta} = |\Delta_{\text{options}}| \times 1.0$).
- **Rebalance Triggers**:
  - **Standard Rebalance**: $|\Delta_{\text{options}} + \Delta_{\text{perp}}| > 0.05\text{ unit}$ (30s sub-loop check)
  - **Emergency Market Hedge**: $|\Delta_{\text{options}} + \Delta_{\text{perp}}| > 0.20\text{ unit}$
- **Limit Price Protection**: Perpetual hedge orders use tight limit price protection capped at **50 bps slippage tolerance** (`max_perp_slippage_bps: 50`).

---

## 6. Multi-Source Credential & Operational Security

- **Credential Resolution Priority**:
  1. Primary: Ingests `derive_perpetual.yml` or `derive.yml` encrypted connector files from `hummingbot-api` using `GATEWAY_PASSPHRASE` / `CONFIG_PASSWORD`.
  2. Secondary: Fallback to root `.env` / environment variables (`DERIVE_SMART_CONTRACT_WALLET`, `DERIVE_SESSION_KEY_PRIV`, `DERIVE_SUBACCOUNT_ID`).
- **Atomic State Persistence**: Writes `.position_state.json` and `.heartbeat.json` atomically via temp files and `os.replace` to eliminate race conditions.
- **Change-Only Notifications**: Telegram notifications dispatch only on material position or order changes, with a $10^{-4}$ floating-point tolerance filter to eliminate jitter noise.

---

## 7. Lifecycle & Profit-Taking Endpoints

1. **Take-Profit (TP)**: Close short body legs when mark value decays by **`60.0%`** of initial collected net credit.
2. **Stop-Loss (SL)**: Close/derisk structure if position mark loss reaches **`100.0%`** of net collected premium.
3. **Expiration Target**: Full cash settlement at expiration date (**`14 DTE`**).
4. **Emergency De-Risking**: Unwinds or hedge-locks positions if volatility regime flips or margin limits are breached.

---

## 8. Each Tick Execution & Supervision Instructions

On every loop tick (e.g. 60-second supervisory cadence):

### Step 1: Run Domain Routine
Invoke the quantitative execution engine with full dual-cadence parameters:
```
manage_routines(
  action="run",
  name="derive_volatility_loop_trader",
  agent="derive_volatility_spread_trader",
  config={
    "trading_pair": "ETH-USDT",
    "poll_interval_seconds": 300,
    "perp_hedge_interval_seconds": 30,
    "paper_mode": false,
    "enable_options_entry": true,
    "short_delta_target": 0.30,
    "wing_delta_target": 0.10,
    "dte": 14,
    "min_edge": 5.0,
    "hedge_band": 0.05,
    "emergency_band": 0.20,
    "max_margin_utilization_pct": 75.0,
    "max_perp_delta_hedge_ratio": 1.0,
    "max_perp_slippage_bps": 50,
    "target_profit_pct": 60.0,
    "stop_loss_pct": 100.0,
    "contract_size": 1.0,
    "notify_on_change_only": true,
    "options_route": "RFQ_COMBO_PACKAGE"
  }
)
```

### Step 2: Live Portfolio Verification
Verify subaccount state and positions:
```
get_portfolio_overview()
```

### Step 3: Journal Reasoning & State
Record the tick's Margin Utilization %, Net Delta, Derivatives Monkey Consensus status, execution events, and risk metrics into the session journal.
