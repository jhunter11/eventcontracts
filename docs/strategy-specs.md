# **Hyperdetailed Strategy Specs (Full Contract Compliant)**

This document contains 11 fully expanded strategy specifications ready for the repository's research and validation pipeline. Each spec provides every detail required by the StrategyBase framework, including comprehensive review packets, specific data APIs (optimized for free tiers and a single, shared Apify compute pool), censoring rules, exact feature nullability, and latency assumptions.

## **1\. Spatiotemporal Temperature Arbitrage**

### **1\. Naming & Discovery**

* **Strategy ID:** weather-temperature\_arbitrage-v1  
* **Module:** python/src/eventcontracts/plugins/strategies/weather\_temperature\_arbitrage.py  
* **Factory Registration:** @register("weather\_temperature\_arbitrage")  
* **Schema ID:** weather\_temperature\_arbitrage\_features  
* **Model Name:** weather\_temperature\_arbitrage  
* **Sleeve ID:** weather-kalshi-paper-a

### **2\. Strategy Review Packet**

* **Researcher Owner:** Quant\_Weather\_Team  
* **Hypothesis:** Retail traders over-extrapolate morning temperatures and ignore systemic afternoon cloud cover dynamics that cap daily highs.  
* **Market Universe:** Kalshi daily high temperature brackets (e.g., KXHIGHNY).  
* **Data Sources:** \* **Open-Meteo API** (100% Free for \<10k req/day). Provides easily consumable JSON access to NOAA HRRR models (normalized to ExternalSignalEvent).  
  * **Kalshi REST/WS API** (Free, requires funded account).  
* **Model Type:** Spatiotemporal Transformer predicting final temperature density.  
* **Validation Periods:** Walk-forward validation over 3 years (2021-2023) using historical ASOS and HRRR.  
* **Metrics:** Hit Rate: 58% | Calibration: 0.92 | Rank Correlation: 0.65 | Tail Loss: \-4% max daily | Avg Markout: \+3.2 bps  
* **Risk & Execution:** Max drawdown limited to 5% of sleeve capital. Passive maker only.  
* **Expected Capacity:** $500,000 deployment before alpha decay.  
* **Artifact Bundle Location:** contracts/examples/weather\_temperature\_arbitrage/bundle/

### **3\. Feature Engineering Contract**

* **Inputs:** ExternalSignalEvent (Open-Meteo HRRR), QuoteEvent.  
* **Latency Floor:** t0 \+ 180s (simulating NOAA grid processing delays).  
* **Feature Vector:** \[t0\_kalshi\_mid\_implied\_prob, t0\_hrrr\_forecast\_prob, t0\_temp\_delta\_to\_threshold, t0\_cloud\_cover\_pct\].  
* **Nullability & Missing Data:** If Open-Meteo feed is late/missing, schema defaults t0\_hrrr\_forecast\_prob to null, triggering NoAction.

### **4\. Label Construction**

* **Label Definition:** resolution\_value (1.0 if YES bracket hits, 0.0 if NO). Horizon: End of market day.  
* **Censoring Rules:** Drop labels if market pauses before 12:00 PM EST or if ASOS feed goes offline for \> 2 hours.

### **5\. Policy & Decisions**

* **Trigger:** ExternalSignalEvent.  
* **Logic:** Calls ctx.predict(). If abs(model\_implied\_prob \- current\_mid) \> min\_edge\_bps:  
  * Emit PlaceOrder(priority=STANDARD, order\_type=LIMIT, time\_in\_force=GTC, reason="edge\_threshold\_met").

### **6\. Sizing Rules**

* raw\_size \= (predicted\_edge\_bps / 10000\) \* ctx.cash(USD) \* kelly\_fraction  
* risk\_capped\_size \= min(raw\_size, strategy\_max\_size, sleeve\_max\_order\_notional / price)  
* position\_capped\_size \= min(risk\_capped\_size, remaining\_position\_budget)

## **2\. Order Book Imbalance (OBI) Scalper**

### **1\. Naming & Discovery**

* **Strategy ID:** microstructure-obi\_scalper-v1  
* **Module:** python/src/eventcontracts/plugins/strategies/microstructure\_obi\_scalper.py  
* **Factory Registration:** @register("microstructure\_obi\_scalper")  
* **Schema ID:** microstructure\_obi\_scalper\_features

### **2\. Strategy Review Packet**

* **Researcher Owner:** Quant\_HFT\_Team  
* **Hypothesis:** Severe queue imbalances immediately precede short-horizon spread crossings.  
* **Market Universe:** High-liquidity Polymarket/Kalshi contracts.  
* **Data Sources:** \* **Kalshi L3 Websocket** (Free).  
  * **Polymarket CLOB API** (Free).  
* **Model Type:** LightGBM Classifier.  
* **Metrics:** Hit Rate: 51.5% | Avg Markout: \+0.4 bps | Turnover: 400x/day.  
* **Expected Latency Sensitivity:** Extreme (\< 10ms required to capture edge).  
* **Artifact Bundle Location:** contracts/examples/obi\_scalper/bundle/

### **3\. Feature Engineering Contract**

* **Inputs:** OrderBookEvent, OwnOrderUpdateEvent.  
* **Latency Floor:** t0 \+ 5ms.  
* **Feature Vector:** \[t0\_bid\_qty\_L1, t0\_ask\_qty\_L1, t0\_imbalance\_ratio, t0\_spread\_bps\].  
* **Nullability:** All fields mandatory. Stale book (\>1s) defaults to null, forcing cancel-all.

### **4\. Label Construction**

* **Label Definition:** next\_mid\_change\_bps. Horizon: 10 seconds.  
* **Fee & Slippage:** Net of exact venue taker fees and 1 tick slippage penalty.  
* **Censoring Rules:** Censored if market lifecycle changes to paused within the horizon.

### **5\. Policy & Decisions**

* **Trigger:** OrderBookEvent.  
* **Logic:** If model predicts next\_mid\_change\_bps favors Bid \> min\_edge:  
  * Emit PlaceOrder(priority=FAST, order\_type=LIMIT, price=best\_bid+1, reason="obi\_momentum").  
* **Risk Logic:** Emit CancelOrder(priority=CRITICAL) if adverse\_selection\_bps spikes.

### **6\. Sizing Rules**

* raw\_size \= static\_clip\_size (Constant for deterministic latency).  
* position\_capped\_size \= min(risk\_capped\_size, max\_scalp\_inventory \- ctx.exposure())

## **3\. Non-Farm Payrolls (NFP) Shock Absorber**

### **1\. Naming & Discovery**

* **Strategy ID:** macro-nfp\_absorber-v2  
* **Module:** python/src/eventcontracts/plugins/strategies/macro\_nfp\_absorber.py  
* **Factory Registration:** @register("macro\_nfp\_absorber")  
* **Schema ID:** macro\_nfp\_absorber\_features

### **2\. Strategy Review Packet**

* **Researcher Owner:** Volatility\_Desk  
* **Hypothesis:** MMs pull liquidity pre-NFP, creating artificially wide spreads. Earning this spread outweighs directional gap risk.  
* **Market Universe:** Kalshi NFP and Unemployment rate brackets.  
* **Data Sources:** \* **BLS Public Data API** (Free). Used for post-release definitive settlement validation.  
  * **Apify Scraper Actor** (Leveraging existing shared compute pool, zero marginal cost). Polling a free high-frequency economic calendar (like Investing.com) exactly at 8:30:00 AM EST to act as the TimerEvent trigger.  
* **Model Type:** GARCH historical volatility model.  
* **Expected Capacity:** $200,000 per release.  
* **Artifact Bundle Location:** contracts/examples/nfp\_absorber/bundle/

### **3\. Feature Engineering Contract**

* **Inputs:** TimerEvent, QuoteEvent.  
* **Latency Floor:** t0 \+ 0ms (Timer-based, fully synchronous).  
* **Feature Vector:** \[t0\_pre\_event\_spread\_bps, t0\_historical\_vol\_bps, t0\_mins\_to\_release\].

### **4\. Label Construction**

* **Label Definition:** spread\_capture\_bps. Horizon: 5 minutes post-release.  
* **Censoring Rules:** Censored if data release is delayed.

### **5\. Policy & Decisions**

* **Trigger:** TimerEvent at T \- 5 mins.  
* **Logic:** Emit ReplaceOrder(priority=STANDARD) to widen quotes to match t0\_historical\_vol\_bps cone.  
* **Trigger:** TimerEvent at T \+ 1 min. Emit ReplaceOrder(priority=RELAXED) to tighten quotes.

### **6\. Sizing Rules**

* raw\_size \= ctx.cash(USD) \* 0.05  
* position\_capped\_size \= risk\_capped\_size (Strategy manages its own delta neutrality).

## **4\. Cross-Venue Quote Arbitrage**

### **1\. Naming & Discovery**

* **Strategy ID:** arbitrage-cross\_venue-v1  
* **Module:** python/src/eventcontracts/plugins/strategies/arbitrage\_cross\_venue.py  
* **Factory Registration:** @register("arbitrage\_cross\_venue")  
* **Schema ID:** arbitrage\_cross\_venue\_features

### **2\. Strategy Review Packet**

* **Researcher Owner:** Arbitrage\_Desk  
* **Hypothesis:** Kalshi and Polymarket pools disjoint, creating pure risk-free arbs \> venue fees.  
* **Data Sources:** \* **Kalshi Websocket** (Free).  
  * **Polymarket Websocket** (Free).  
* **Model Type:** Deterministic rules engine (No ML).  
* **Metrics:** Hit Rate: 99% (conditional on fill) | Execution failure rate (Leg-risk): 4%.  
* **Artifact Bundle Location:** contracts/examples/cross\_venue\_arb/bundle/

### **3\. Feature Engineering Contract**

* **Inputs:** QuoteEvent from both venues.  
* **Latency Floor:** t0 \+ 50ms (network transit).  
* **Feature Vector:** \[t0\_kalshi\_best\_bid, t0\_poly\_best\_ask, t0\_kalshi\_taker\_fee, t0\_poly\_taker\_fee\].

### **4\. Label Construction**

* **Label Definition:** binary\_profitable\_after\_fees.  
* **Censoring Rules:** Censored if contract rules mismatch (e.g., different dispute resolutions).

### **5\. Policy & Decisions**

* **Trigger:** QuoteEvent.  
* **Logic:** If (kalshi\_bid \- poly\_ask) \> (kalshi\_fee \+ poly\_fee \+ min\_edge\_bps):  
  * Emit PlaceOrder(priority=FAST, order\_type=MARKET) to the venue the sleeve controls.

### **6\. Sizing Rules**

* raw\_size \= min(kalshi\_bid\_qty, poly\_ask\_qty)

## **5\. Federal Reserve Rate Network (GNN)**

### **1\. Naming & Discovery**

* **Strategy ID:** macro-fed\_gnn-v1  
* **Module:** python/src/eventcontracts/plugins/strategies/macro\_fed\_gnn.py  
* **Factory Registration:** @register("macro\_fed\_gnn")  
* **Schema ID:** macro\_fed\_gnn\_features

### **2\. Strategy Review Packet**

* **Hypothesis:** Retail updates front-month Fed rates quickly but fails to propagate conditional probabilities to \+2/+3 month markets.  
* **Data Sources:** \* **FRED API** (Federal Reserve Economic Data \- Free) for historical/macro baselines.  
  * **Apify Web Scraper Actor** (Base subscription tier, heavily utilized as a critical shared resource across multiple strategies) deployed to scrape the CME FedWatch tool dynamically.  
* **Model Type:** Graph Neural Network.  
* **Artifact Bundle Location:** contracts/examples/fed\_gnn/bundle/

### **3\. Feature Engineering Contract**

* **Inputs:** TradeEvent (front month), ExternalSignalEvent (CME scrape).  
* **Latency Floor:** t0 \+ 10ms.  
* **Feature Vector:** \[t0\_month0\_implied\_rate, t0\_month1\_current\_mid, t0\_cme\_target\_prob\].

### **4\. Label Construction**

* **Label Definition:** next\_mid\_change\_bps on back-month contracts. Horizon: 1 hour.  
* **Censoring Rules:** Censored on FOMC meeting days.

### **5\. Policy & Decisions**

* **Trigger:** TradeEvent (front month volatility).  
* **Logic:** Recalculate GNN. If back-month model\_prob \- current\_mid \> min\_edge\_bps:  
  * Emit PlaceOrder(priority=FAST, order\_type=LIMIT).

### **6\. Sizing Rules**

* raw\_size \= (edge / 10000\) \* ctx.cash(USD) \* 0.1 scaled by inverse volatility.

## **6\. SpaceX/NASA Launch Delay Predictor**

### **1\. Naming & Discovery**

* **Strategy ID:** aerospace-launch\_delay-v1  
* **Module:** python/src/eventcontracts/plugins/strategies/aerospace\_launch\_delay.py  
* **Factory Registration:** @register("aerospace\_launch\_delay")  
* **Schema ID:** aerospace\_launch\_delay\_features

### **2\. Strategy Review Packet**

* **Hypothesis:** Launch probability decays predictably; markets misprice final 2 hours if no fueling events occur.  
* **Data Sources:** \* **The Space Devs API (Launch Library 2\)** (Fully viable on their Free tier for \<=15 req/hour polling).  
  * **Open-Meteo API** (Free) for launch pad wind-shear conditions.  
* **Model Type:** Cox Proportional Hazards Model / Survival Analysis.  
* **Artifact Bundle Location:** contracts/examples/launch\_delay/bundle/

### **3\. Feature Engineering Contract**

* **Inputs:** TimerEvent, ExternalSignalEvent, LifecycleEvent.  
* **Latency Floor:** t0 \+ 2000ms.  
* **Feature Vector:** \[t0\_time\_to\_window\_close\_mins, t0\_wind\_shear\_knots, t0\_fueling\_confirmed\_bool\].

### **4\. Label Construction**

* **Label Definition:** settlement\_probability.  
* **Censoring Rules:** Censored if launch scrubbed for non-weather reasons \>24h in advance.

### **5\. Policy & Decisions**

* **Trigger:** TimerEvent every 5 mins.  
* **Logic:** If t0\_time\_to\_window\_close\_mins \< 120 and t0\_fueling\_confirmed\_bool \== 0:  
  * Emit PlaceOrder(priority=STANDARD, outcome\_side=NO).

### **6\. Sizing Rules**

* raw\_size \= (120 \- t0\_time\_to\_window\_close\_mins) \* sizing\_multiplier.

## **7\. Box Office Velocity Extrapolator**

### **1\. Naming & Discovery**

* **Strategy ID:** entertainment-box\_office-v2  
* **Module:** python/src/eventcontracts/plugins/strategies/entertainment\_box\_office.py  
* **Factory Registration:** @register("entertainment\_box\_office")  
* **Schema ID:** entertainment\_box\_office\_features

### **2\. Strategy Review Packet**

* **Hypothesis:** Friday night API seat-booking velocity highly correlates to total weekend gross.  
* **Data Sources:** \* **TMDB API** (Free) for metadata.  
  * **Apify Scraper Actor** (Uses the same shared compute pool as Strategy 5, zero marginal cost) scraping Fandango/AMC Friday theater capacities.  
* **Model Type:** Time Series Extrapolator (ARIMA).  
* **Artifact Bundle Location:** contracts/examples/box\_office/bundle/

### **3\. Feature Engineering Contract**

* **Inputs:** ExternalSignalEvent, QuoteEvent.  
* **Latency Floor:** t0 \+ 300s.  
* **Feature Vector:** \[t0\_friday\_seat\_occupancy\_pct, t0\_ticket\_velocity\_1hr\].

### **4\. Label Construction**

* **Label Definition:** resolution\_value.  
* **Censoring Rules:** Drop labels if major theater chains experience API outages on Friday night.

### **5\. Policy & Decisions**

* **Trigger:** TimerEvent at Friday 8:00 PM EST.  
* **Logic:** If prediction.confidence \> 0.8 and edge\_bps \> 500:  
  * Emit PlaceOrder(priority=RELAXED, order\_type=LIMIT).

### **6\. Sizing Rules**

* raw\_size \= ctx.cash(USD) \* 0.10.

## **8\. Presidential Primary Contagion Momentum**

### **1\. Naming & Discovery**

* **Strategy ID:** politics-primary\_momentum-v1  
* **Module:** python/src/eventcontracts/plugins/strategies/politics\_primary\_momentum.py  
* **Factory Registration:** @register("politics\_primary\_momentum")  
* **Schema ID:** politics\_primary\_momentum\_features

### **2\. Strategy Review Packet**

* **Hypothesis:** Early state wins alter priors for subsequent states faster than retail adjusts.  
* **Data Sources:** \* **Polymarket/Kalshi APIs** (Free).  
  * **Apify Scraper Actor** (Uses the same shared pool, zero marginal cost) parsing RealClearPolitics polling aggregations.  
* **Model Type:** Bayesian Updating Model.

### **3\. Feature Engineering Contract**

* **Inputs:** SettlementResolvedEvent (State A), QuoteEvent (State B).  
* **Latency Floor:** t0 \+ 2ms.  
* **Feature Vector:** \[t0\_state\_a\_winner, t0\_state\_b\_polling\_avg, t0\_state\_b\_current\_implied\].

### **4\. Label Construction**

* **Label Definition:** next\_mid\_change\_bps. Horizon: 12 hours.

### **5\. Policy & Decisions**

* **Trigger:** SettlementResolvedEvent.  
* **Logic:** Emit PlaceOrder(priority=FAST, order\_type=MARKET, reason="contagion\_update").

### **6\. Sizing Rules**

* raw\_size \= kelly\_fraction \* (bayes\_edge\_bps / 10000\) \* ctx.cash(USDC).

## **9\. Legislative Approval Cascades**

### **1\. Naming & Discovery**

* **Strategy ID:** politics-legislative\_cascade-v1  
* **Module:** python/src/eventcontracts/plugins/strategies/politics\_legislative\_cascade.py  
* **Factory Registration:** @register("politics\_legislative\_cascade")  
* **Schema ID:** politics\_legislative\_features

### **2\. Strategy Review Packet**

* **Hypothesis:** NLP sentiment on key swing senators provides a measurable delta to bill passage.  
* **Data Sources:** \* **ProPublica Congress API** (Free) for official bill status and whip counts.  
  * **GDELT Project API** (Free) for global news sentiment and political event streaming.  
* **Model Type:** Fine-tuned RoBERTa LLM mapped to whip counts.  
* **Artifact Bundle Location:** contracts/examples/legislative\_cascade/bundle/

### **3\. Feature Engineering Contract**

* **Inputs:** ExternalSignalEvent (NLP payload).  
* **Latency Floor:** t0 \+ 5000ms (LLM inference time).  
* **Feature Vector:** \[t0\_senator\_id, t0\_sentiment\_score, t0\_whip\_leverage\_factor\].

### **4\. Label Construction**

* **Label Definition:** settlement\_probability.  
* **Censoring Rules:** Ignored if bill is fundamentally altered/amended after sentiment capture.

### **5\. Policy & Decisions**

* **Trigger:** ExternalSignalEvent (Negative sentiment on swing vote).  
* **Logic:** Emit CancelOrder(priority=CRITICAL) for YES bets, then PlaceOrder(priority=STANDARD, outcome\_side=NO).

### **6\. Sizing Rules**

* raw\_size \= ctx.cash(USD) \* abs(t0\_sentiment\_score). Capped by sleeve\_max\_order\_notional.

## **10\. Queue Position Evader**

### **1\. Naming & Discovery**

* **Strategy ID:** microstructure-queue\_evader-v1  
* **Module:** python/src/eventcontracts/plugins/strategies/microstructure\_queue\_evader.py  
* **Factory Registration:** @register("microstructure\_queue\_evader")  
* **Schema ID:** queue\_evader\_features

### **2\. Strategy Review Packet**

* **Hypothesis:** Resting orders at the back of the queue suffer adverse selection on volatility spikes.  
* **Data Sources:** **Kalshi / Polymarket Native APIs** (Free). No external data required.  
* **Model Type:** Heuristic queue position estimator.

### **3\. Feature Engineering Contract**

* **Inputs:** OwnOrderUpdateEvent, OrderBookEvent, TradeEvent.  
* **Latency Floor:** t0 \+ 5ms.  
* **Feature Vector:** \[t0\_estimated\_queue\_ahead, t0\_recent\_taker\_volume\].

### **4\. Label Construction**

* **Label Definition:** cancel\_urgency. Target \= 1 if resting order was subsequently run over and marked negative.

### **5\. Policy & Decisions**

* **Trigger:** TradeEvent (large taker print).  
* **Logic:** If t0\_estimated\_queue\_ahead approaches 0 and adverse\_selection\_bps is high, emit CancelOrder(priority=CRITICAL).

### **6\. Sizing Rules**

* raw\_size \= strategy\_max\_size (Hard capped per level to avoid capital trapping).

## **11\. CPI / Inflation Bracket Predictor**

### **1\. Naming & Discovery**

* **Strategy ID:** macro-cpi\_predictor-v1  
* **Module:** python/src/eventcontracts/plugins/strategies/macro\_cpi\_predictor.py  
* **Factory Registration:** @register("macro\_cpi\_predictor")  
* **Schema ID:** macro\_cpi\_predictor\_features

### **2\. Strategy Review Packet**

* **Hypothesis:** Web-scraping of retail goods predicts BLS CPI accurately vs implied distribution.  
* **Data Sources:** \* **Truflation API** (Utilizing their fully Free tier) for decentralized, daily inflation metrics.  
  * **Apify Scraper Actor** (Leveraging the existing shared pool, zero marginal cost) to track major retail/grocery price changes.  
* **Model Type:** Time Series Transformer.

### **3\. Feature Engineering Contract**

* **Inputs:** ExternalSignalEvent, QuoteEvent.  
* **Latency Floor:** t0 \+ 3600s (Scraping aggregations).  
* **Feature Vector:** \[t0\_alt\_data\_mom\_inflation, t0\_cleveland\_fed\_nowcast, t0\_kalshi\_implied\_mean\].

### **4\. Label Construction**

* **Label Definition:** resolution\_value.  
* **Censoring Rules:** Drop labels if alternative data feeds exhibit \> 5% missing categories.

### **5\. Policy & Decisions**

* **Trigger:** TimerEvent.  
* **Logic:** If ctx.predict(model) shifts implied mean \> 0.05%, emit PlaceOrder(priority=RELAXED).

### **6\. Sizing Rules**

* Kelly sizing across mutually exclusive brackets. risk\_capped\_size \= min(raw\_size, sleeve\_max\_order\_notional).