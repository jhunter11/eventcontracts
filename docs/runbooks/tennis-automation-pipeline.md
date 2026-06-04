# Tennis Automation Pipeline (two-plane)

Auto-identifies tennis matches, calcs odds, builds model snapshots, and feeds the
live runner — split into a **research/data plane** (cheap/ephemeral, e.g. RunPod)
and an **execution plane** (the trading host). The seam between them is a
**bundle directory** (`snapshots.jsonl` + `manifest.json`); nothing else crosses.

```
  PLANE A (RunPod, scheduled)                     PLANE B (execution host)
  ┌───────────────────────────────┐   bundle/    ┌──────────────────────────┐
  │ tennis_pipeline.py             │  ─────────►  │ tennis_run_from_bundle.py│
  │  • discover Kalshi KXATPMATCH  │  (sync via   │  • freshness gate        │
  │  • fetch odds (pluggable)      │   S3/rsync/  │  • build runner command  │
  │  • resolve players (Sackmann)  │   runpodctl) │  • cargo live-runner     │
  │  • build v2 snapshots          │              │    (ONNX scoring + exec) │
  │  → snapshots.jsonl + manifest  │              └──────────────────────────┘
  └───────────────────────────────┘
   needs: Sackmann CSVs + odds key                 needs: ONNX bundle + Kalshi creds
   NO model, NO Kalshi creds                        NO odds key, NO history
```

Why this split works: Plane A only needs the Sackmann history + an odds source —
the **model (ONNX) lives on Plane B** and the Rust runner re-scores each snapshot.
So Plane A can run on cheap CPU/ephemeral compute and never holds trading
credentials; Plane B never needs the odds API key or the history data.

## Plane A — produce a bundle (one-shot, schedule it)

```bash
# RunPod pod / any box with the repo + .venv + data/tennis + THE_ODDS_API_KEY
THE_ODDS_API_KEY=<key> .venv/bin/python python/scripts/tennis_pipeline.py \
    --series KXATPMATCH --surface Clay --round R16 --tourney-level G --best-of 5 \
    --sport-key tennis_atp_french_open \
    --out /shared/tennis-bundle
# then push the bundle to where Plane B reads it:
aws s3 sync /shared/tennis-bundle s3://my-bucket/tennis-bundle      # or
runpodctl send /shared/tennis-bundle  / rsync -a /shared/tennis-bundle exec-host:~/bundle
```

- Set `--surface` / `--round` / `--sport-key` per active tournament (defaults are
  Roland Garros clay R16). Tournament-specific; one active event at a time.
- Already-started matches are auto-skipped via The Odds API `commence_time`
  (the model is pre-match). The favorite leg (lower decimal odds) is the YES side.
- Odds providers (pluggable): `--odds-provider the-odds-api` (default, sharp
  Pinnacle line) or `--odds-provider manual --odds-csv player_odds.csv`
  (`player,decimal_odds` rows) when you have no key or want to override.
- ATP only — the model is ATP-trained and there's no WTA history in the repo.

Cron (run every 30 min during the event):

```cron
*/30 * * * * cd /workspace/eventcontracts && THE_ODDS_API_KEY=$KEY \
  .venv/bin/python python/scripts/tennis_pipeline.py --out /shared/tennis-bundle \
  >> /var/log/tennis_pipeline.log 2>&1 && aws s3 sync /shared/tennis-bundle s3://my-bucket/tennis-bundle
```

## Plane B — run from the bundle

```bash
# observe (default): subscribe + score + show would-be intents, NO orders
python python/scripts/tennis_run_from_bundle.py --bundle ./tennis-bundle

# live (real money): explicit flags AND the runner's own `yes` prompt
KALSHI_ENV=prod python python/scripts/tennis_run_from_bundle.py \
    --bundle ./tennis-bundle --live-submit --max-live-orders 1 --execute
```

The launcher reads `manifest.json` for the `--tickers` list, the model bundle, and
the schema-version pin, then constructs the live-runner command. A **freshness
gate** refuses a bundle older than `--max-age-min` (default 60) or one whose
matches have all started (override with `--include-started`). Credentials
(`KALSHI_ENV`, `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`) must be in the
Plane-B shell — the runner reads them from the process env, not `.env`.

## Bundle contract

`manifest.json`:
- `generated_at`, `series`, `tournament{surface,tourney_level,best_of,round}`
- `model{expect_tennis_schema_version, bundle}` — what Plane B must score with
- `odds{provider, sport_key, book}` — provenance
- `matches[]` — per match: `market_id`, players, `p1_odds`/`p2_odds`,
  `commence_time`, live Kalshi bid/ask at build time
- `tickers[]` — exactly what to pass to `--tickers`
- `earliest_commence`, `skipped[]`

`snapshots.jsonl`: one row per tradeable `market_id` (the favorite/YES leg), the
flattened v2 `TennisV2Snapshot` the Rust runner re-scores.

## Caveats (read before funding)

- The model **backtests worse than the closing line** — these runs validate the
  live path + collect real fills; not a proven edge. See
  `docs/tennis-tradeability-findings-and-plan.md`.
- `verify-strategy sports-tennis-xgboost` must be green before a live session.
- The `fair_price` ≤4-decimal fix (the bug the first $5 run surfaced) is in place;
  the pipeline's snapshots score to risk-parseable intents (verified via
  `dryrun_score_snapshot.py`).
