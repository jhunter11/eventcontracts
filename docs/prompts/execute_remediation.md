# Remediation Execution Prompt

**Instructions for the User:** 
You can copy the prompt below and paste it into a new chat with a coding agent (or use the `invoke_agent` tool to pass it to the `generalist` sub-agent) to begin executing the remediation plan.

---

**Prompt:**

You are a senior systems engineer operating in a high-stakes, low-latency trading environment. Your task is to execute a critical remediation plan for the `eventcontracts` trading framework.

Before making any file modifications, you MUST ensure that no Python or Rust processes related to `eventcontracts` (such as `live_paper.py`, background backtests, or Rust live-runners) are currently running. Use shell commands to check for and terminate any active background processes.

You will implement the fixes outlined in `docs/remediation-plan.md`. Please proceed sequentially, focusing entirely on **Phase 1: Critical Market Logic & Safety Fixes** and **Phase 2: Feedback Loops & Memory Leaks** for this session.

### Execution Mandates:
1. **Safety First:** Do not bypass or remove existing tests unless they are fundamentally flawed (e.g., masking the bugs we are fixing). 
2. **Test-Driven Remediation:** For every bug you fix in Phase 1, you MUST write a failing test case *first* to prove the bug exists, then apply the fix to make the test pass.
   - Example: Write a test that submits a 1-lot order, then replaces it with a 10,000-lot order to prove the `SleeveRiskGate` currently bypasses the notional limit. Then fix `SleeveRiskGate.evaluate` to make the test pass.
3. **Idiomatic Consistency:** Ensure all changes maintain strict typing (`mypy` compliant) and adhere to the existing Ports and Adapters architecture. Do not introduce new third-party dependencies unless absolutely necessary.
4. **End-to-End Validation:** After completing Phase 1 and Phase 2, you must run the full Python test suite (`pytest tests/`) to ensure no regressions were introduced in the vertical slice.

### Step-by-Step Instructions:

**Step 1: Terminate Processes**
Check for and terminate any running `python` or `cargo` processes related to this workspace.

**Step 2: Execute Phase 1 (Critical Logic Fixes)**
Refer to `docs/remediation-plan.md`. You must address:
- The Risk Gate Bypass (`ReplaceOrder` in `policy.py`).
- The Simulator YES/NO Conflation (`_apply_trade` in `market_simulator.py`).
- The Kalshi Ask Ladder Sorting Bug (`reversed()` logic in `normalization/kalshi.py` and `adapters/...`).
- The NO-Price Inversion Bug (Kalshi normalization).
- Ghost Liquidity pruning (`replay/order_book.py`).
- Optimistic Queueing (`execution/queue.py`).

*Remember to write reproduction tests for these before fixing them.*

**Step 3: Execute Phase 2 (Feedback Loops)**
Refer to `docs/remediation-plan.md`. You must address:
- Implementing the `StatefulContextProvider` so the strategy context updates with filled positions.
- Fixing the `PnLTracker` memory leak (stop eagerly creating `PositionRecord`s on quotes).
- Updating the `DailyLossLedger` to calculate actual net PnL.

**Step 4: Full System Validation**
Run `pytest` and `mypy` (or `ruff check .`) across the python workspace to guarantee the remediation was successful and structurally sound. 

Provide a detailed summary of the changes made, the tests added, and the final state of the test suite once complete.
