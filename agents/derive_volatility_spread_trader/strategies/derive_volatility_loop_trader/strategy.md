---
name: Derive Volatility Loop Trader
description: Autonomous Hybrid Architecture short-volatility Iron Condor strategy
  combining dedicated Derive EIP-712 Options RFQ package execution (30Δ/10Δ) with
  Hummingbot API perpetual delta hedging (derive_perpetual), Derivatives Monkey
  institutional GEX/DEX consensus filtering, 75% margin safety caps, and 50 bps slippage protection.
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
  hedge_execution_mode: STRATEGY_EXECUTOR
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

# Derive Volatility Loop Trader — Hybrid Architecture Playbook

Autonomous Hybrid Short-Volatility Iron Condor & Perpetual Delta-Hedging Strategy across Derive Protocol (Lyra Finance) and Hummingbot API.

---

## 1. Hybrid Architecture & Division of Responsibilities

Because Hummingbot API natively supports standard Spot & Perpetual orderbooks (via the `derive_perpetual` connector) but does **not** have built-in support for Derive's multi-leg EIP-712 Options RFQ combo contract, the **Hybrid Architecture** strictly decouples Options Execution from Perpetual Delta Hedging.

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                                   HYBRID ARCHITECTURE                                   │
├────────────────────────────────────────────┬────────────────────────────────────────────┤
│         1. OPTIONS DOMAIN (ROUTINE)        │       2. PERPETUAL DOMAIN (STRATEGY)       │
│             [Derive Native RFQ]            │           [Hummingbot Executors]           │
├────────────────────────────────────────────┼────────────────────────────────────────────┤
│ • Volatility Surface & GEX Analysis        │ • Strategy Agent Governance                │
│ • Pure Delta Strike Matching (30Δ/10Δ)      │ • Delta Imbalance Monitoring (Δ_net)       │
│ • EIP-712 RFQ Multi-Leg Package Sign       │ • Hummingbot `manage_executors` for        │
│ • Atomic Multi-Tick Quote & Fills          │   `derive_perpetual` (ETH-USDC / ETH-USDT) │
│ • Options TP (60%) & SL (100%) Management  │ • Native Slippage (50 bps) & Rate-Limiting │
│ • Net Options Delta Emission (Δ_options)   │ • Strict Rebalancing (|Δ_net| > 0.05)      │
└────────────────────────────────────────────┴────────────────────────────────────────────┘
```

### 1.1 Division of Responsibilities

#### A. Options Side: Dedicated Routine (`derive_volatility_loop_trader.py`)
* **Why**: Derive Options RFQs require custom EIP-712 typed data hashing (`RFQExecuteModuleData`), signing with session keys against Derive's RFQ contract (`0x9371352CCef6f5b36EfDFE90942fFE622Ab77F1D`), broadcasting RFQ requests to market makers, and polling quotes across multiple ticks to select the highest net credit.
* **What it does**:
  1. Scans implied volatility ($14\text{D IV}$), realized volatility ($7\text{D RV}$), surface GEX, and skew.
  2. Cross-checks Derivatives Monkey institutional consensus (GEX, DEX, Gamma Flip, Term Structure).
  3. Resolves optimal strikes for the Iron Condor (30Δ short body / 10Δ long wings, single-tenor expiry).
  4. Enforces pre-trade margin utilization projections (75% cap, discrete package sizing).
  5. Signs and executes the atomic 4-leg RFQ package on Derive.
  6. Tracks options position lifecycle (Take-Profit at 60%, Stop-Loss at 100%, expiry settlement).
  7. Reads subaccount clearinghouse positions and **emits exact delta telemetry**:
     - $\Delta_{\text{options}}$ (Aggregate Options Delta)
     - $\Delta_{\text{perp}}^{\text{target}} = - \Delta_{\text{options}}$ (Target Perpetual Position)
     - $\Delta_{\text{net}} = \Delta_{\text{options}} + \Delta_{\text{perp}}$ (Net Delta Imbalance)
     - Emits structured `hedge_signal` to `.heartbeat.json`, `.position_state.json`, and LiveReport.

#### B. Perpetual Delta Hedging: Strategy Agent & Hummingbot Executors (`manage_executors`)
* **Why**: Hummingbot API provides battle-tested perpetual futures order management, continuous position tracking, slippage controls, orderbook execution, and retry mechanisms on `derive_perpetual`.
* **What it does**:
  1. Reads the routine's output telemetry and heartbeat (`options_delta`, `perp_delta`, `delta_imbalance`).
  2. Evaluates net delta imbalance against the rebalance threshold ($|\Delta_{\text{net}}| > 0.05\text{ unit}$).
  3. Triggers `manage_executors` on the `derive_perpetual` connector (`order_executor` with `LIMIT_MAKER`, `LIMIT_CHASER`, or `MARKET`).
  4. Maintains strict portfolio delta neutrality ($\Delta_{\text{net}} \approx 0.00$) with tight 50 bps slippage protection.

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
- **Strict Single-Tenor Expiry Rule**: All 4 legs of the Iron Condor must belong strictly to the **same single expiration timestamp** (default `14 DTE`, e.g., `Sep 04, 2026 08:00 UTC`).
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

## 5. Perpetual Delta Hedging via Hummingbot Executors

In the Hybrid Architecture, perpetual delta hedging is executed by the strategy agent dispatching Hummingbot executors on the `derive_perpetual` connector.

### 5.1 Delta Hedging Parameters & Calculations
- **Aggregate Options Delta**: $\Delta_{\text{options}} = \sum (\text{Leg Amount} \times \Delta_{\text{leg}})$
- **Current Perpetual Position**: $\Delta_{\text{perp}}$
- **Target Perpetual Hedge**: $\Delta_{\text{perp}}^{\text{target}} = - \Delta_{\text{options}}$
- **Net Delta Imbalance**: $\Delta_{\text{net}} = \Delta_{\text{perp}}^{\text{target}} - \Delta_{\text{perp}} = - (\Delta_{\text{options}} + \Delta_{\text{perp}})$
- **Strict Perp Ratio Cap**: Total perpetual delta must NEVER exceed options delta hedge requirement ($\text{Max Perp Delta} = |\Delta_{\text{options}}| \times 1.0$).

### 5.2 Hedging Triggers & Execution Bands
- **Neutral Band ($|\Delta_{\text{net}}| \le 0.05$)**: No rebalance required; portfolio is delta neutral.
- **Standard Rebalance ($|\Delta_{\text{net}}| > 0.05$)**: Spawn `order_executor` on `derive_perpetual` with `LIMIT_MAKER` or `LIMIT_CHASER` (passive maker order with 1 bp offset and 50 bps slippage limit).
- **Emergency Band ($|\Delta_{\text{net}}| > 0.20$)**: Immediate hedge execution with `MARKET` or aggressive `LIMIT` on `derive_perpetual` to restore delta neutrality.

### 5.3 Hummingbot Executor Dispatch Specification
```python
manage_executors(
    action="create",
    executor_type="order_executor",
    executor_config={
        "connector_name": "derive_perpetual",
        "trading_pair": "ETH-USDC",  # or ETH-USDT
        "side": 1 if delta_imbalance > 0 else 2,  # 1=BUY, 2=SELL
        "amount": round(abs(delta_imbalance), 3),
        "execution_strategy": "LIMIT_MAKER",  # Post-only resting maker
        "price": maker_price,
        "leverage": 1,
        "position_action": "OPEN"
    }
)
```

---

## 6. Options Lifecycle & Profit-Taking Endpoints

1. **Take-Profit (TP)**: Close short body legs when mark value decays by **`60.0%`** of initial collected net credit.
2. **Stop-Loss (SL)**: Close/derisk structure if position mark loss reaches **`100.0%`** of net collected premium.
3. **Expiration Target**: Full cash settlement at expiration date (**`14 DTE`**).
4. **Emergency De-Risking**: Unwinds or hedge-locks positions if volatility regime flips or margin limits are breached.
5. **Flatten Hedge on Exit**: When options close via TP/SL, perpetual positions are flattened to $0.00$.

---

## 7. Dual-Cadence Supervisory Workflow

```
[Macro Cadence: 300s / 5m]
  │
  ├─► Options Routine executes surface analysis & GEX cross-check
  ├─► Strikes matched (30Δ/10Δ single-tenor expiry)
  ├─► EIP-712 RFQ Multi-Leg Package signed & executed on Derive
  ├─► TP (60%) / SL (100%) lifecycle checked
  └─► Emits live Δ_options & hedge_signal to Heartbeat & LiveReport

[Strategy / Hedging Cadence: Fast 30s / Event-Driven]
  │
  ├─► Strategy Agent inspects Δ_options vs current Δ_perp
  ├─► Computes Net Delta Imbalance Δ_net = - (Δ_options + Δ_perp)
  ├─► If |Δ_net| > 0.05:
  │     └─► Calls manage_executors on derive_perpetual (LIMIT_MAKER / LIMIT_CHASER)
  ├─► Fills & position updates tracked via Hummingbot API
  └─► Records state, margin utilization %, and delta metrics to Journal
```

---

## 8. Tick Supervision & Execution Instructions

On every supervisory cycle:

### Step 1: Verify Options Routine & Read Delta Telemetry
Check the active continuous routine and read the latest heartbeat / LiveReport telemetry:
```python
manage_routines(action="list_instances")
```
Read the emitted `options_delta`, `perp_delta`, `delta_imbalance`, and `hedge_signal`.

### Step 2: Live Portfolio Verification
Verify subaccount balances and perpetual positions from Hummingbot:
```python
get_portfolio_overview()
```

### Step 3: Delta Imbalance & Hedge Evaluation
Evaluate the net delta imbalance:
$$\Delta_{\text{net}} = - (\Delta_{\text{options}} + \Delta_{\text{perp}})$$
- If $|\Delta_{\text{net}}| \le 0.05$: **Delta Neutral** — no order required.
- If $|\Delta_{\text{net}}| > 0.05$: **Rebalance Required** — proceed to Step 4.

### Step 4: Execute Perpetual Hedge via Hummingbot
Dispatch an `order_executor` on `derive_perpetual` to rebalance delta:
```python
manage_executors(
    action="create",
    executor_type="order_executor",
    executor_config={
        "connector_name": "derive_perpetual",
        "trading_pair": "ETH-USDC",
        "side": 1 if delta_imbalance > 0 else 2,
        "amount": round(abs(delta_imbalance), 3),
        "execution_strategy": "LIMIT_MAKER",
        "price": maker_price
    }
)
```

### Step 5: Journal State & Risk Metrics
Record Margin Utilization %, Net Delta ($\Delta_{\text{net}}$), Derivatives Monkey consensus status, options PnL, and executor execution status into the trading session journal via `trading_agent_journal_write`.

