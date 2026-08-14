---
name: Derive Volatility Loop Trader
description: Autonomous 1-minute cadence short-volatility Iron Condor strategy on
  Derive with Black-Scholes pricing, Derivatives Monkey GEX/DEX intel, pre-trade projected
  margin checks (75% cap), discrete complete package sizing (100%, 75%, 50%, 25%),
  strict perpetual delta hedging, and change-only Telegram alerts.
agent_key: null
skills: []
default_config:
  contract_size: 1
  frequency_sec: 60
  max_margin_utilization_pct: 75
  max_perp_delta_hedge_ratio: 1
  min_edge: 5
  notify_on_change_only: true
  paper_mode: false
  target_profit_pct: 60
  trading_pair: ETH-USDT
  use_derivatives_monkey_intel: true
default_trading_context: ''
created_by: 1934595831
created_at: '2026-08-14T10:35:34.048157+00:00'
---

# Derive Volatility Loop Trader — Strategy Playbook

Autonomous 1-Minute Cadence Short Volatility Arbitrage Strategy on Derive (Lyra Finance).

## 1. Strategy Overview & Core Mechanics

The strategy monetizes the volatility risk premium between Implied Volatility (IV) and Realized Volatility (RV) by systematically writing defined-risk 4-Leg Iron Condors and dynamically delta-hedging with perpetual futures.

- **Market Venue**: Derive Protocol (Loaded dynamically via `DERIVE_SMART_CONTRACT_WALLET`, `DERIVE_SESSION_KEY_PRIV`, `DERIVE_SUBACCOUNT_ID` from `.env` or routine config)
- **Primary Underlyings**: `ETH-USDT`, `BTC-USDT`, `HYPE-USDT`
- **Execution Route**: Atomic RFQ Combo Packages via EIP-712 session key signing (`derive_action_signing`)
- **Loop Cadence**: 60 Seconds (1 Minute)

---

## 2. Quantitative Model & Signal Triggers

### 2.1 Dynamic Volatility Engine (No Hardcoding)
- **Live Spot Price ($S_0$)**: Fetched in real time from Derive `/public/get_ticker` (`ETH-PERP` / `{SYMBOL}-PERP`).
- **Dynamic Implied Volatility ($\text{IV}_{14\text{D}}$)**: Discovers live ATM options on Derive `/public/get_instruments`, reads live `mark_price` from `/public/get_ticker`, and inverts the Black-Scholes pricing formula via Newton-Raphson:
  $$\text{BS\_Price}(S_0, K_{\text{ATM}}, T, r=0.03, \sigma_{\text{IV}}) = \text{Mark Price} \implies \sigma_{\text{IV}}$$
- **Dynamic 7-Day Realized Volatility ($\text{RV}_{7\text{D}}$)**: Fetches trailing 168 1-hour candle closes from Binance / Hyperliquid / CoinGecko REST APIs and computes annualized variance:
  $$\text{RV}_{7\text{D}} = \sqrt{\frac{24 \times 365}{N-1} \sum_{t=1}^N \left(\ln\frac{S_t}{S_{t-1}} - \bar{r}\right)^2} \times 100\%$$
- **Net Volatility Edge**:
  $$\text{Net Edge} = (\text{IV}_{14\text{D}} - \text{RV}_{7\text{D}}) - 2.50\text{ pts}$$
- **Entry / Scale-In Threshold**: $\text{Net Edge} \ge 5.0\text{ vol points}$.

### 2.2 Derivatives Monkey Institutional Intelligence
- Parses Dealer Gamma Exposure (GEX) and Delta Exposure (DEX) dynamically from the live options surface:
  $$\text{GEX} = \sum_{K} \text{OI}_K \cdot \Gamma_K \cdot S^2 \cdot 0.01$$
- **High Conviction Filter**: Dealer GEX $\ge +\$5.0\text{M}$ confirms spot volatility compression, validating short-volatility premium collection.

---

## 3. Structural Integrity & Discrete Package Sizing

### 3.1 Strict Single-Tenor Expiry Rule
All 4 legs of the Iron Condor must belong strictly to the **same single expiration timestamp** (14 DTE, e.g. `Aug 28, 2026 08:00 UTC`):
1. **Short Call**: $K_{\text{SC}} \approx S_0 \cdot e^{+0.45 \sigma \sqrt{T}}$
2. **Short Put**: $K_{\text{SP}} \approx S_0 \cdot e^{-0.45 \sigma \sqrt{T}}$
3. **Long Call Wing**: $K_{\text{LC}} \approx S_0 \cdot e^{+1.15 \sigma \sqrt{T}}$
4. **Long Put Wing**: $K_{\text{LP}} \approx S_0 \cdot e^{-1.15 \sigma \sqrt{T}}$

### 3.2 Discrete Scale Tiers
Options are NEVER entered as ad-hoc or split legs. Sizing evaluates discrete complete packages:
- **100% Package**: `1.00 ETH` across all 4 legs
- **75% Package**: `0.75 ETH` across all 4 legs
- **50% Package**: `0.50 ETH` across all 4 legs
- **25% Package**: `0.25 ETH` across all 4 legs

### 3.3 Pre-Trade Margin Utilization Projection
$$\text{Projected Margin Used} = \text{Current Positions Margin} + (\text{Wing Width} \times \text{Package Size})$$
$$\text{Projected Margin Utilization} = \frac{\text{Projected Margin Used}}{\text{Subaccount Total Collateral}} \times 100\%$$
- **Safety Gate**: Package submitted **ONLY IF** $\text{Projected Margin Utilization} \le \mathbf{75.0\%}$ AND $\text{Est. Package Margin} \le \text{Buying Power}$.
- If a larger package exceeds the cap, the engine gracefully sizes down to the next discrete tier. If no tier fits, scaling pauses (`SCALE-IN CAPPED`).

---

## 4. Perpetual Delta Hedging & Cap Enforcement

- **Aggregate Options Delta**: $\Delta_{\text{options}} = \sum (\text{Leg Amount} \times \Delta_{\text{leg}})$
- **Target Perp Hedge**: $\Delta_{\text{perp}}^{\text{target}} = - \Delta_{\text{options}}$
- **Perp Delta Cap**: Total perpetual delta must NEVER exceed the options delta hedge requirement ($\text{Max Perp Delta} = |\Delta_{\text{options}}| \times 1.0$).
- **Rebalance Bands**:
  - **Standard Rebalance**: $|\Delta_{\text{options}} + \Delta_{\text{perp}}| > 0.05\text{ ETH}$
  - **Emergency Market Hedge**: $|\Delta_{\text{options}} + \Delta_{\text{perp}}| > 0.20\text{ ETH}$

---

## 5. Lifecycle & Profit-Taking Endpoints

1. **Take-Profit (TP)**: Close short body legs when mark value decays by **`60.0%`** of initial collected premium.
2. **Expiration Target**: Full cash settlement at expiration date (**`14 DTE`**).
3. **Emergency Stop**: Closes / derisks structure if Net Vol Edge turns negative or margin utilization breaches critical limits.

---

## 6. Each Tick Execution Instructions

On every loop tick (60-second frequency):

### Step 1: Run Domain Routine
Execute the primary options risk & execution routine:
```
manage_routines(
  action="run",
  name="derive_volatility_loop_trader",
  agent="derive_volatility_spread_trader",
  config={
    "trading_pair": "ETH-USDT",
    "poll_interval_seconds": 60,
    "paper_mode": false,
    "min_edge": 5.0,
    "contract_size": 1.0,
    "max_margin_utilization_pct": 75.0,
    "max_perp_delta_hedge_ratio": 1.0,
    "target_profit_pct": 60.0,
    "notify_on_change_only": true
  }
)
```

### Step 2: Live Portfolio Verification
Verify subaccount state and positions via:
```
get_portfolio_overview()
```

### Step 3: Journal Findings
Journal the tick's Margin Utilization, Net Delta, Execution Status, and whether any position change occurred.


