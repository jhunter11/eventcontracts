# Using LLMQuant/quant-mind for our Kalshi work — decision memo

**Question:** what is the most useful way to use
[quant-mind](https://github.com/LLMQuant/quant-mind) for our event-contract work?
**Date:** 2026-06-04. **TL;DR:** Use **only its mature piece — the arXiv paper
extraction/tagging pipeline (Stage 1) — as an offline literature-mining feeder for the
*front* of our edge-discovery funnel**, vendored (MIT) behind a thin wrapper, targeted at
the sleeves where edge is most plausible. **Do not** depend on its retrieval/RAG/MCP/agent
layer (not shipped) and **do not** wire it into the live runtime or treat any extraction as
ground truth. It accelerates *idea/feature discovery*; it does **not** create or validate
edge.

## What quant-mind actually is (and isn't)

A research **knowledge-extraction + retrieval framework** for quant finance: ingest
arXiv/news/blogs → parse PDF/HTML → tag → embed → RAG/DeepResearch/"Data MCP". Python 3.8+,
`uv`, OpenAI (`gpt-4o-mini`), Pydantic, async. Core: `quantmind/`, `flows/` (`paper_flow`),
`magic.py`.

It is **not** a trading engine, backtester, execution stack, or pricing library. It is a
*research accelerator*.

**Maturity (the binding fact):** early/alpha, **mid-migration to the OpenAI Agents SDK**.
- **Works today:** arXiv `paper_flow` extraction, `batch_run`, PDF/HTML parse, tagging,
  async orchestration, `magic.resolve_magic_input`.
- **Roadmap / not shipped:** MCP server (only a "Data MCP" *vision*), working RAG/semantic
  search (no live demo), news/blog/report connectors, standardized `knowledge/` format,
  `mind/memory`. The repo states plainly: *"this section describes our long-term vision, not
  current capabilities."*
- **License:** MIT (we may vendor/fork/modify freely).

## Why it's relevant to us (and why it's not a silver bullet)

Our **binding constraint is edge discovery + validation, not infrastructure**
(see `docs/production-readiness-assessment.md` §0, and the prove-before-expand philosophy).
A tool that turns the research firehose into structured, reviewable leads attacks exactly
that constraint — *at the front of the funnel*. But per our own discipline: **a literature
idea, like calibration, is not edge.** Every lead it surfaces must still survive the full
funnel (point-in-time leakage check → fee `0.07·p·(1−p)` → spread → realized depth → markout
→ settlement → CLV-vs-market-at-the-tradable-moment). And because it is an LLM summarizer,
**every extracted number/claim is a lead to verify against the primary source, never a fact
to feed a model.**

## Most useful applications, ranked by value × current feasibility

### 1. Offline literature-mining for feature/model discovery — **DO NOW** (Stage 1 works)
Point `paper_flow` at curated arXiv queries for the families where we *lack a sharp
reference* (per the Kalshi edge map: entertainment > weather > macro > walled parlay; avoid
mainline/crypto/in-play where a sharp reference already prices it). Concretely:
- **Entertainment/box-office:** opening-weekend gross forecasting, pre-release demand signals.
- **Weather (KXHIGH):** NWP statistical post-processing / MOS, ensemble calibration, lead-aware
  bias correction — directly feeds the *open* items in the weather validation spec (lead-aware
  σ, intraday high-so-far, ensemble tails).
- **Macro (CPI/NFP/Fed):** nowcasting methods, release-surprise modeling — feeds the
  specced-not-built CPI/Fed/NFP producers.
- **Sports:** in-play win-probability (tennis Markov refinements), player-cut models.
- **Cross-cutting:** prediction-market microstructure, favorite–longshot bias,
  calibration-vs-tradability — sharpens the *philosophy*, not just one sleeve.

**Output contract:** a tagged, deduped shortlist of candidate features/methods + primary-source
links → reviewed by a human → promoted (or killed) as a **versioned audit/validation spec**
(our existing workflow). This is the high-leverage, low-risk use and it uses only the mature
component.

### 2. A queryable corpus of *our own* research + the mined papers — **BUILD THIN, DON'T WAIT**
The dream use (an MCP/RAG the audit/edge-validation skills query: *"what did we conclude about
tennis tradeability / BTC arb / weather lead-time?"*) is blocked — retrieval/MCP isn't shipped.
Don't take a dependency on vapor. Instead: reuse quant-mind's **extraction → Pydantic knowledge
units** (the durable, working part) to build a **thin local RAG we own** (embeddings + vector
search) over (a) the mined papers and (b) our `docs/*.md`, validation specs, and kill reports.
That gives institutional memory + agent-queryable context now, without betting on PR6/PR7. If
their MCP layer matures, swap our thin retriever for it later (MIT, low switching cost).

### 3. Guarded external-signal *producer* for text/news markets — **DEFER**
Tempting for macro-headline / entertainment markets, but: the news/blog connectors aren't
implemented, and LLM extraction carries hallucination + **look-ahead/leakage + freshness**
risk — precisely the "guilty until proven" zone. Only consider after a point-in-time,
provenance-stamped, freshness-gated producer harness exists, and only feeding **paper**, behind
the `external_edge` archetype. Lowest priority; highest footgun.

## Integration shape (keep it at arm's length)

- Run as a **separate offline research service** in its own `uv` env; **pin a commit** (alpha,
  churning). Vendor under MIT.
- Its outputs are **artifacts that feed the front of the edge funnel** — not inputs to the live
  runtime, not into the typed event lake, not into a Rust scorer.
- **Verify every extracted figure against the primary source before it informs a model.**
- Cost: OpenAI per-paper, bounded for offline batch; cap concurrency.

## What NOT to do
- Don't wire it to live/paper signal production yet (Stage 2 immature + leakage risk).
- Don't treat LLM summaries/numbers as ground truth.
- Don't adopt its agent/`magic` layer as a hard dependency mid-migration.
- Don't let a literature idea skip the edge funnel — that is how false edges (Zverev/Jodar,
  the stale-`c` +$101 phantom) get funded.

## Recommendation
Adopt **Application 1 now** (a small, vendored, pinned literature-mining job targeting the
plausible-edge sleeves, output → audit specs). Spike **Application 2** as a thin in-house RAG
over the extracted corpus + our docs. **Defer Application 3.** Net: quant-mind earns its keep
as a **discovery accelerator at the top of the funnel**, never as a source of edge or a live
signal — which is exactly where a research-knowledge tool helps a system whose real bottleneck
is finding provable, market-beating mispricings.
