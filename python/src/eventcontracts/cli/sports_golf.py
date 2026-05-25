"""Sports-golf setup and smoke-test commands."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from eventcontracts.cli.backtest import run_backtest
from eventcontracts.config import load_sleeve_spec, load_strategy_spec
from eventcontracts.domain.events import EventProvenance, QuoteEvent, TimerEvent
from eventcontracts.domain.ids import EventId
from eventcontracts.domain.models import InstrumentId, Venue
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.env import load_default_env
from eventcontracts.sports import (
    CutLineBracket,
    GolfCutLineMonteCarloModel,
    GolfPlayerSnapshot,
    GolfTournamentPrediction,
    GolfTournamentState,
    MarketPriceBar,
)
from eventcontracts.storage import ParquetEventStore

LOCAL_SMOKE_REQUIRED_KEYS: tuple[str, ...] = ()
FREE_RESEARCH_KEYS = (
    "NOAA_TOKEN",
    "FRED_API_KEY",
    "BLS_API_KEY",
    "APIFY_TOKEN",
    "TMDB_API_KEY",
    "TRUFLATION_API_KEY",
    "PROPUBLICA_API_KEY",
)
SPORTS_GOLF_PROVIDER_KEYS = ("DATAGOLF_API_KEY", "PGA_TOUR_API_KEY", "SHOTLINK_API_KEY")
KALSHI_KEYS = ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH")
POLYMARKET_KEYS = (
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
    "POLYMARKET_FUNDER_ADDRESS",
)
FAST_GOLF_KEYS = ("PGA_TOUR_API_KEY", "SHOTLINK_API_KEY")

DEFAULT_CONFIG_ROOT = Path("configs")
DEFAULT_AS_OF = datetime(2026, 5, 25, 15, 0, tzinfo=UTC)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    preflight = subparsers.add_parser(
        "sports-golf-preflight",
        help="Check dotenv keys and sports-golf strategy config readiness.",
    )
    preflight.add_argument("--configs-root", type=Path, default=DEFAULT_CONFIG_ROOT)
    preflight.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when local-smoke required keys are missing.",
    )
    preflight.add_argument(
        "--require-sports-provider",
        action="store_true",
        help="Exit non-zero unless at least one DataGolf/PGA/ShotLink key is present.",
    )
    preflight.set_defaults(handler=_handle_preflight)

    smoke = subparsers.add_parser(
        "sports-golf-smoke",
        help="Generate bar-compatible golf test data and run sports strategies end to end.",
    )
    smoke.add_argument("--configs-root", type=Path, default=DEFAULT_CONFIG_ROOT)
    smoke.add_argument("--out", type=Path, default=Path("data/sports-golf-smoke"))
    smoke.add_argument("--simulations", type=int, default=750)
    smoke.add_argument("--starting-equity", type=str, default="10000")
    smoke.add_argument(
        "--require-keys",
        action="store_true",
        help="Fail before running if core research keys are absent.",
    )
    smoke.add_argument("--as-of", type=str, default=DEFAULT_AS_OF.isoformat())
    smoke.set_defaults(handler=_handle_smoke)


def _handle_preflight(args: argparse.Namespace) -> int:
    payload = sports_golf_preflight(_resolve_configs_root(args.configs_root))
    print(json.dumps(payload, indent=2, sort_keys=True))
    if args.strict and payload["required_missing"]:
        return 2
    provider_status = payload["sports_golf_optional_provider_keys"]
    if args.require_sports_provider and not any(provider_status.values()):
        return 2
    return 0


def _handle_smoke(args: argparse.Namespace) -> int:
    if args.require_keys:
        payload = sports_golf_preflight(_resolve_configs_root(args.configs_root))
        if payload["required_missing"]:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 2
    summary = run_sports_golf_smoke(
        configs_root=_resolve_configs_root(args.configs_root),
        out=args.out,
        simulations=args.simulations,
        starting_equity=Decimal(str(args.starting_equity)),
        as_of=_parse_datetime(args.as_of),
    )
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 0


def sports_golf_preflight(configs_root: Path) -> dict[str, Any]:
    env_path = load_default_env()
    strategies = {
        "player_cut": configs_root / "strategies" / "sports-player-cut-lgbm.toml",
        "cut_line": configs_root / "strategies" / "sports-cut-line-shifter.toml",
        "frl_weather": configs_root / "strategies" / "sports-frl-weather-arb.toml",
        "hole_by_hole": configs_root / "strategies" / "sports-hole-by-hole-pin.toml",
    }
    sleeves = {
        "player_cut": configs_root / "sleeves" / "sports-kalshi-paper-a.toml",
        "cut_line": configs_root / "sleeves" / "sports-cut-line-kalshi-paper-a.toml",
        "frl_weather": configs_root / "sleeves" / "sports-polymarket-paper-a.toml",
        "hole_by_hole": configs_root / "sleeves" / "sports-hole-by-hole-polymarket-paper-a.toml",
    }
    config_status: dict[str, Any] = {}
    for name, path in strategies.items():
        spec = load_strategy_spec(path)
        config_status[f"strategy:{name}"] = {
            "path": str(path),
            "strategy_id": str(spec.strategy_id),
            "loaded": True,
            "operator_maps": _strategy_map_status(spec),
        }
    for name, path in sleeves.items():
        sleeve = load_sleeve_spec(path)
        config_status[f"sleeve:{name}"] = {
            "path": str(path),
            "sleeve_id": str(sleeve.sleeve_id),
            "venue": sleeve.venue.value,
            "loaded": True,
        }

    required_status = _key_status(LOCAL_SMOKE_REQUIRED_KEYS)
    return {
        "env_path": str(env_path) if env_path is not None else None,
        "required_for_local_smoke": required_status,
        "required_missing": tuple(key for key, present in required_status.items() if not present),
        "free_research_keys": _key_status(FREE_RESEARCH_KEYS),
        "sports_golf_optional_provider_keys": _key_status(SPORTS_GOLF_PROVIDER_KEYS),
        "kalshi_optional_for_live_capture": _key_status(KALSHI_KEYS),
        "polymarket_optional_for_live_execution": _key_status(POLYMARKET_KEYS),
        "fast_golf_optional_for_shotlink_work": _key_status(FAST_GOLF_KEYS),
        "configs": config_status,
        "next_command": (
            "PYTHONPATH=python/src .venv/bin/python -m eventcontracts.cli "
            "sports-golf-smoke --out data/sports-golf-smoke"
        ),
    }


def run_sports_golf_smoke(
    *,
    configs_root: Path,
    out: Path,
    simulations: int,
    starting_equity: Decimal,
    as_of: datetime,
) -> dict[str, Any]:
    run_root = out / datetime.now(UTC).strftime("run-%Y%m%dT%H%M%S%fZ")
    data_root = run_root / "event_lake"
    reports_root = run_root / "reports"
    reports_root.mkdir(parents=True, exist_ok=True)

    state = _synthetic_tournament_state(as_of)
    player_map = _player_market_map(state)
    brackets = (
        CutLineBracket(cut_line=-1, market_id="KX-PGA-DEMO-CUT-NEG1"),
        CutLineBracket(cut_line=0, market_id="KX-PGA-DEMO-CUT-EVEN"),
        CutLineBracket(cut_line=1, market_id="KX-PGA-DEMO-CUT-PLUS1"),
    )
    bracket_map = _bracket_market_map(brackets)
    prediction = GolfCutLineMonteCarloModel(simulations=simulations, seed=31).predict(state, brackets=brackets)

    store = ParquetEventStore(data_root)
    _write_smoke_events(store, state=state, prediction=prediction, brackets=brackets, as_of=as_of)
    store.flush()

    player_strategy = _with_parameters(
        load_strategy_spec(configs_root / "strategies" / "sports-player-cut-lgbm.toml"),
        {"player_market_map": player_map},
    )
    cut_line_strategy = _with_parameters(
        load_strategy_spec(configs_root / "strategies" / "sports-cut-line-shifter.toml"),
        {"bracket_market_map": bracket_map},
    )
    player_report, player_summary = run_backtest(
        player_strategy,
        load_sleeve_spec(configs_root / "sleeves" / "sports-kalshi-paper-a.toml"),
        data_root,
        starting_equity=starting_equity,
        latency_ms=50.0,
        queue_fraction="1.0",
    )
    cut_report, cut_summary = run_backtest(
        cut_line_strategy,
        load_sleeve_spec(configs_root / "sleeves" / "sports-cut-line-kalshi-paper-a.toml"),
        data_root,
        starting_equity=starting_equity,
        latency_ms=50.0,
        queue_fraction="1.0",
    )

    player_report_path = reports_root / "sports-player-cut-lgbm.json"
    cut_report_path = reports_root / "sports-cut-line-shifter.json"
    player_report_path.write_text(json.dumps(player_report.to_dict(), indent=2, default=str) + "\n", encoding="utf-8")
    cut_report_path.write_text(json.dumps(cut_report.to_dict(), indent=2, default=str) + "\n", encoding="utf-8")

    manifest = {
        "as_of": as_of.isoformat(),
        "data_root": str(data_root),
        "reports_root": str(reports_root),
        "simulations": simulations,
        "player_market_map": player_map,
        "bracket_market_map": bracket_map,
        "cut_line_expected": prediction.expected_cut_line,
        "player_cut_orders": player_summary.decisions_emitted,
        "cut_line_orders": cut_summary.decisions_emitted,
        "player_report": str(player_report_path),
        "cut_line_report": str(cut_report_path),
    }
    manifest_path = run_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return {"manifest": str(manifest_path), **manifest}


def _write_smoke_events(
    store: ParquetEventStore,
    *,
    state: GolfTournamentState,
    prediction: GolfTournamentPrediction,
    brackets: tuple[CutLineBracket, ...],
    as_of: datetime,
) -> None:
    quote_ts = as_of
    signal_ts = as_of + timedelta(seconds=1)
    timer_ts = as_of + timedelta(seconds=2)

    player_prices = {
        "leader": Decimal("0.36"),
        "steady": Decimal("0.44"),
        "bubble": Decimal("0.35"),
        "volatile": Decimal("0.28"),
        "struggler": Decimal("0.62"),
    }
    for player in state.players:
        if player.market_id is None:
            continue
        store.append_normalized(
            _bar_quote(player.market_id, player_prices[player.player_id], quote_ts, event_suffix=player.player_id)
        )
    for signal in prediction.to_player_cut_signals(received_at=signal_ts):
        store.append_normalized(signal)
    store.append_normalized(
        TimerEvent(
            event_id=EventId("sports-golf-smoke-player-cut-timer"),
            timestamp=timer_ts,
            label="player_round_complete",
            provenance=_smoke_provenance(),
        )
    )

    for bracket in brackets:
        store.append_normalized(
            _bar_quote(
                bracket.market_id,
                Decimal("0.10"),
                quote_ts + timedelta(seconds=5),
                event_suffix=str(bracket.cut_line),
            )
        )
    store.append_normalized(prediction.to_cut_line_signal(received_at=signal_ts + timedelta(seconds=5)))
    store.append_normalized(
        TimerEvent(
            event_id=EventId("sports-golf-smoke-cut-line-timer"),
            timestamp=timer_ts + timedelta(seconds=5),
            label="cut_line_recompute",
            provenance=_smoke_provenance(),
        )
    )


def _bar_quote(market_id: str, close: Decimal, timestamp: datetime, *, event_suffix: str) -> QuoteEvent:
    return MarketPriceBar(
        instrument_id=InstrumentId(venue=Venue.KALSHI, market_id=market_id),
        timestamp=timestamp,
        open=close,
        high=min(Decimal("0.99"), close + Decimal("0.01")),
        low=max(Decimal("0.01"), close - Decimal("0.01")),
        close=close,
        volume=Decimal("100"),
    ).to_quote_event(event_id=EventId(f"sports-golf-smoke-quote-{event_suffix}"), half_spread=Decimal("0.005"))


def _synthetic_tournament_state(as_of: datetime) -> GolfTournamentState:
    return GolfTournamentState(
        tournament_id="pga-smoke",
        as_of=as_of,
        cut_rule_size=3,
        cut_holes=36,
        tournament_holes=72,
        wind_forecast_mph=13.0,
        course_baseline_cut_line=0.0,
        players=(
            GolfPlayerSnapshot("leader", -4.0, 18, 2.0, 0.2, -0.02, 0.1, "KX-PGA-DEMO-LEADER-CUT"),
            GolfPlayerSnapshot("steady", -1.0, 18, 0.5, 0.1, 0.0, 0.0, "KX-PGA-DEMO-STEADY-CUT"),
            GolfPlayerSnapshot("bubble", 1.0, 18, 0.7, -1.0, 0.01, 0.2, "KX-PGA-DEMO-BUBBLE-CUT"),
            GolfPlayerSnapshot("volatile", 2.0, 18, 1.5, -2.0, 0.02, 0.3, "KX-PGA-DEMO-VOLATILE-CUT"),
            GolfPlayerSnapshot("struggler", 5.0, 18, -1.5, 0.4, 0.04, 0.3, "KX-PGA-DEMO-STRUGGLER-CUT"),
        ),
    )


def _with_parameters(spec: StrategySpec, values: Mapping[str, str]) -> StrategySpec:
    parameters = dict(spec.parameters)
    parameters.update(values)
    return replace(spec, parameters=parameters)


def _strategy_map_status(spec: StrategySpec) -> dict[str, bool]:
    return {
        "player_market_map_present": bool(str(spec.parameters.get("player_market_map", "")).strip()),
        "bracket_market_map_present": bool(str(spec.parameters.get("bracket_market_map", "")).strip()),
    }


def _player_market_map(state: GolfTournamentState) -> str:
    return ";".join(
        f"{player.player_id}:{player.market_id}"
        for player in state.players
        if player.market_id is not None
    )


def _bracket_market_map(brackets: tuple[CutLineBracket, ...]) -> str:
    return ";".join(f"{bracket.cut_line}:{bracket.market_id}" for bracket in brackets)


def _key_status(keys: tuple[str, ...]) -> dict[str, bool]:
    return {key: bool(os.getenv(key)) for key in keys}


def _smoke_provenance() -> EventProvenance:
    return EventProvenance(source="sports-golf-smoke", channel="timer", schema_version="sports-golf-smoke-v1")


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _resolve_configs_root(path: Path) -> Path:
    if path.exists():
        return path
    for directory in (Path.cwd(), *Path.cwd().parents):
        candidate = directory / path
        if candidate.exists():
            return candidate
    return path
