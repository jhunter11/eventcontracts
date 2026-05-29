# RunPod Cloud Deployment Plan

**Target Environment:** RunPod Pod (2 vCPU, 4GB RAM, 5GHz CPU)
**Workload:** `eventcontracts` high-frequency trading platform

## 1. Hardware Sizing & Suitability Analysis

The proposed hardware (2 vCPU, 4GB RAM, 5GHz) is **highly specialized** and excellent for specific parts of the `eventcontracts` lifecycle, but insufficient for others.

### A. The Live Trading Runner (Rust) ✅ **Perfect Fit**
*   **CPU (5GHz):** Ultra-high clock speed is exactly what you want for a low-latency trading loop. It reduces single-thread execution time. 
*   **vCPUs (2):** Sufficient. One core can be pinned for network I/O (Websockets/REST) and the other pinned strictly for the Strategy Execution/OMS loop.
*   **RAM (4GB):** More than enough. The Rust binary, running a fully optimized strategy, will consume less than 100MB of RAM.
*   *Verdict:* This pod is ideal for running the production `rust/crates/live-runner`.

### B. Python Backtesting & Data Ingestion ⚠️ **Warning**
*   **RAM (4GB):** Very tight. As noted in the Logic Audit, the current `ParquetEventStore.read()` pulls all data into memory to sort it. A multi-month backtest will easily exceed 4GB and crash with an Out-Of-Memory (OOM) error.
*   *Verdict:* You can use this pod for lightweight paper-trading (`live_paper.py`) or capturing real-time ticks, but do not run historical backtests over large timeframes until the streaming memory fixes are applied.

### C. ML Training & Feature Generation ❌ **Insufficient**
*   **RAM / CPU:** Training LightGBM or PyTorch models on large parquet datasets requires significantly more RAM (16GB - 64GB+) and more CPU cores for parallel feature extraction.
*   *Verdict:* Do not train models on this pod. 

**Recommendation:** Adopt a "Hub and Spoke" deployment.
1.  **The Hub (Local PC or Beefy Cloud Instance):** Use your local machine or a temporary high-RAM instance to train models, run heavy DuckDB queries, and generate the `ArtifactBundle`.
2.  **The Spoke (This RunPod):** Deploy the `ArtifactBundle` to this 5GHz RunPod to execute the live trades.

---

## 2. Network & Location Strategy

Clock speed (5GHz) is useless if the network distance to the exchange is far.
*   **Kalshi / Polymarket:** Both are generally hosted in AWS `us-east-1` (N. Virginia). 
*   **Action:** When renting the RunPod, ensure the data center is geographically as close to Ashburn, Virginia (US East) as possible to minimize packet transit time.

---

## 3. OS & System Tuning for Low Latency

To get the most out of the 5GHz CPU, the operating system must be tuned so it doesn't throttle the CPU down to save power.

*   **CPU Governor:** Set the CPU to `performance` mode to lock it at 5GHz.
*   **Network Tuning:** Increase TCP window sizes and disable Nagle's algorithm (`TCP_NODELAY`) inside the Rust websocket adapter.

---

## 4. Setup & Deployment Workflow

I have created an automated initialization script located at `scripts/runpod-setup.sh`. 

**Deployment Steps:**
1. Spin up the RunPod instance using a standard Ubuntu container image.
2. Connect via SSH or the RunPod Web Terminal.
3. Clone your repository.
4. Run the setup script: `bash scripts/runpod-setup.sh`
5. Configure your `.env` file with Kalshi/Polymarket API keys.
6. Transfer your pre-trained `ArtifactBundle` from your local machine to the pod.
7. Start the Rust live runner using `cargo run --release`.
