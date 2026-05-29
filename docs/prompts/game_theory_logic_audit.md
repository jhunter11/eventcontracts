# Game Theory & Core Logic Audit: EventContracts

**Role:** You are a Quantitative Researcher and Game Theory Expert specializing in adverse selection, market microstructure, and prediction market dynamics.

**Objective:** Conduct a rigorous logic audit of the `eventcontracts` trading strategies and execution core. Your goal is to aggressively challenge every assumption the system makes about market state, pricing, liquidity, and execution certainty. You must ensure the system reacts to *reality* (hard data from the exchange) rather than *synthetic expectations* (what the system thinks should happen).

## Execution Instructions for the Agent:

Perform this audit by deeply analyzing the logic within the `rust/crates/` (specifically `oms`, `risk`, `model-runtime`, `runner`) and the Python strategy implementations/notebooks (`python/src/`, `notebooks/`). Pay special attention to the strategy configuration files (`configs/strategies/`).

Structure your final audit report using the following phases:

### Phase 1: The "Real Data" vs. "Expected Data" Check
- **Tasks:**
  1. **Price Verification:** Trace how strategies determine the "current price" to make trading decisions. Does the system use a locally computed/cached price, or does it strictly rely on the actual order book top-of-book (BBO) or recent trades provided by the exchange?
  2. **State Assumptions:** Search for instances where the system updates its internal state *assuming* an action was successful (e.g., assuming an order was filled because it was sent, or assuming a position exists before the exchange confirms it via a drop copy/fill message).
  3. **Queue Position:** Does the system make naïve assumptions about its place in the matching engine queue?

### Phase 2: Adverse Selection & Toxic Flow Analysis
- **Tasks:**
  1. **Stale Data:** How does the system handle latency? If a signal is generated based on data that is `N` milliseconds old, is there a risk of being picked off by faster participants?
  2. **Information Asymmetry:** In the strategies (e.g., `macro-cpi-predictor`, `sports-tennis-xgboost`), is there a scenario where the model's prediction is correct, but the market has already priced it in, leading to the system buying at the top or selling at the bottom?
  3. **Order Types:** Are we using Market orders (risking unbounded slippage) or Limit orders? If using Limit orders, is there logic to cancel them if they become stale, or are they left to become optionality for other traders?

### Phase 3: Edge Cases & Adversarial Mechanics
- **Tasks:**
  1. **Partial Fills:** How does the logic handle an order that is only 10% filled? Does it leave the rest open, cancel the remainder, or aggressively cross the spread? Does it break the assumed "position state" for the strategy?
  2. **Market Reversals (Whipsaws):** If the market violently reverses immediately after a signal is generated, does the system have a mechanism to abort, or will it blindly execute into a moving market?
  3. **Fake Liquidity / Spoofing:** Does the feature builder or strategy logic rely on order book depth that could easily be spoofed by adversaries to trigger our models?

### Phase 4: Risk and Capital Allocation Logic
- **Tasks:**
  1. **Over-Allocation:** Can a single malfunctioning strategy, or a feedback loop, drain the available capital? Is the allocator logic (`rust/crates/allocator`) mathematically sound and strictly bounded?
  2. **Correlated Exposures:** If multiple strategies (or multiple markets within a strategy) trigger simultaneously, does the risk system recognize the correlated exposure, or does it treat them as independent?

### Phase 5: The "No Assumptions" Verdict
- **Tasks:**
  1. List every instance where the codebase makes an assumption about the market, the exchange API, or the execution outcome.
  2. Provide a concrete path to refactor that logic to be strictly reactive to *empirical exchange data*.

## Output Requirements:
Deliver a comprehensive markdown report. For every logical flaw, naive assumption, or game-theoretic vulnerability found, provide:
1. **The exact file path and logic block.**
2. **The vulnerability scenario:** A step-by-step description of how the market or an adversary could exploit this logic.
3. **A concrete remediation:** How to rewrite the logic to eliminate the assumption and rely solely on confirmed, real data.
