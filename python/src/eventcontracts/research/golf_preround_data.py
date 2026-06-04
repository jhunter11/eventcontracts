"""Point-in-time data builder for pre-round golf top-N research.

The builder joins four explicitly separated planes:

* pre-round player features;
* public Kalshi bid/ask snapshots captured before scheduled start;
* optional bookmaker/reference odds captured before scheduled start;
* final top-N labels.

Labels are written only to ``target``. Any feature/market/odds row timestamped
after scheduled start is rejected, because that would be post-start leakage for
the pre-round model.
"""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from eventcontracts.research.golf_preround import (
    MODEL_FEATURES,
    GolfPreRoundRow,
    devig_decimal_odds,
    synthetic_preround_fixture,
    write_preround_rows_csv,
)

FEATURE_REQUIRED_COLUMNS: tuple[str, ...] = (
    "event_date",
    "tournament_id",
    "player_id",
    "player_name",
    "feature_as_of",
    "scheduled_start",
)

LABEL_REQUIRED_COLUMNS: tuple[str, ...] = ("tournament_id", "player_id")

SNAPSHOT_COLUMNS: tuple[str, ...] = (
    "captured_at",
    "tournament_id",
    "player_id",
    "player_name",
    "market_ticker",
    "yes_sub_title",
    "no_sub_title",
    "title",
    "rules_primary",
    "yes_bid",
    "yes_ask",
    "yes_bid_size",
    "yes_ask_size",
    "volume",
    "volume_24h",
    "open_interest",
    "market_status",
    "expected_expiration_time",
    "close_time",
)

ODDS_COLUMNS: tuple[str, ...] = (
    "source",
    "tournament_id",
    "player_id",
    "player_name",
    "odds_as_of",
    "market_type",
    "decimal_odds",
    "odds_probability",
    "reference_price_source",
    "overround",
)


@dataclass(frozen=True)
class BuildReport:
    """Summary of a point-in-time CSV build."""

    output_path: str
    rows_written: int
    features_read: int
    labels_read: int
    snapshots_read: int
    odds_rows_read: int
    rows_missing_market: int
    rows_missing_odds: int
    decision_gate: str

    def as_dict(self) -> dict[str, object]:
        return {
            "output_path": self.output_path,
            "rows_written": self.rows_written,
            "features_read": self.features_read,
            "labels_read": self.labels_read,
            "snapshots_read": self.snapshots_read,
            "odds_rows_read": self.odds_rows_read,
            "rows_missing_market": self.rows_missing_market,
            "rows_missing_odds": self.rows_missing_odds,
            "decision_gate": self.decision_gate,
        }


def build_preround_topn_dataset(
    *,
    feature_rows: Sequence[Mapping[str, object]],
    label_rows: Sequence[Mapping[str, object]],
    snapshot_rows: Sequence[Mapping[str, object]] = (),
    odds_rows: Sequence[Mapping[str, object]] = (),
    out: Path,
    top_n: int = 20,
) -> BuildReport:
    """Join raw point-in-time inputs and write a research-ready CSV."""

    labels = _labels_by_key(label_rows, top_n=top_n)
    snapshots = _latest_snapshots_by_key(snapshot_rows)
    odds = _odds_probabilities_by_key(odds_rows)
    built: list[GolfPreRoundRow] = []
    missing_market = 0
    missing_odds = 0

    for feature in feature_rows:
        _require_columns(feature, FEATURE_REQUIRED_COLUMNS, "feature row")
        key = _row_key(feature, top_n=top_n)
        label = labels.get(key)
        if label is None:
            continue
        scheduled_start = _parse_datetime(_required(feature, "scheduled_start"))
        feature_as_of = _parse_datetime(_required(feature, "feature_as_of"))
        if feature_as_of > scheduled_start:
            raise ValueError(f"feature row for {key} is after scheduled_start")
        snapshot = _latest_before(snapshots.get(key, ()), scheduled_start, timestamp_field="captured_at")
        odds_item = _latest_before(odds.get(key, ()), scheduled_start, timestamp_field="odds_as_of")
        if snapshot is None:
            missing_market += 1
        if odds_item is None:
            missing_odds += 1
        built.append(
            _build_row_from_inputs(
                feature,
                label,
                snapshot=snapshot,
                odds=odds_item,
                top_n=top_n,
                scheduled_start=scheduled_start,
            )
        )

    if not built:
        raise ValueError("no rows built; check feature/label keys and top_n")
    write_preround_rows_csv(out, built)
    return BuildReport(
        output_path=str(out),
        rows_written=len(built),
        features_read=len(feature_rows),
        labels_read=len(label_rows),
        snapshots_read=len(snapshot_rows),
        odds_rows_read=len(odds_rows),
        rows_missing_market=missing_market,
        rows_missing_odds=missing_odds,
        decision_gate=(
            "research-ready CSV only; rerun golf_preround_research.py and require chronological OOS before "
            "tick logging or paper promotion"
        ),
    )


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Read a CSV as plain dict rows with UTF-8 BOM tolerance."""

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"empty CSV: {path}")
        return [dict(row) for row in reader]


def write_snapshot_rows(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    """Write public Kalshi snapshot rows in the importer schema."""

    _write_dicts(path, SNAPSHOT_COLUMNS, rows)


def write_odds_rows(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    """Write normalized odds/reference rows in the importer schema."""

    _write_dicts(path, ODDS_COLUMNS, rows)


def fixture_input_rows() -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    """Return deterministic raw inputs for no-network builder tests."""

    features: list[dict[str, object]] = []
    labels: list[dict[str, object]] = []
    snapshots: list[dict[str, object]] = []
    odds_rows: list[dict[str, object]] = []
    for row in synthetic_preround_fixture():
        scheduled_start = datetime.combine(row.event_date, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)
        feature_as_of = scheduled_start - timedelta(hours=18)
        captured_at = scheduled_start - timedelta(hours=2)
        odds_as_of = scheduled_start - timedelta(hours=3)
        feature_payload: dict[str, object] = {
            "event_date": row.event_date.isoformat(),
            "tournament_id": row.tournament_id,
            "player_id": row.player_id,
            "player_name": row.player_name,
            "feature_as_of": feature_as_of.isoformat(),
            "scheduled_start": scheduled_start.isoformat(),
            "top_n": row.top_n,
            "field_size": row.field_size,
            "course_archetype": row.course_archetype,
            "tee_wave": row.tee_wave,
            "round_number": row.round_number,
        }
        for feature in MODEL_FEATURES:
            if feature not in {"market_mid", "liquidity", "spread"}:
                feature_payload[feature] = row.feature_value(feature)
        features.append(feature_payload)
        labels.append(
            {
                "event_date": row.event_date.isoformat(),
                "tournament_id": row.tournament_id,
                "player_id": row.player_id,
                "top_n": row.top_n,
                "made_top_n": row.target,
            }
        )
        snapshots.append(
            {
                "captured_at": captured_at.isoformat(),
                "tournament_id": row.tournament_id,
                "player_id": row.player_id,
                "market_ticker": f"KXPGATOP20-{row.tournament_id.upper()}-{row.player_id.upper()}",
                "yes_bid": row.market_bid,
                "yes_ask": row.market_ask,
                "yes_bid_size": 100.0,
                "yes_ask_size": 100.0,
                "volume": row.feature_value("liquidity"),
                "volume_24h": row.feature_value("liquidity") / 2.0,
                "open_interest": row.feature_value("liquidity"),
                "market_status": "active",
                "expected_expiration_time": (scheduled_start + timedelta(days=4)).isoformat(),
                "close_time": (scheduled_start + timedelta(days=4)).isoformat(),
            }
        )
        odds_rows.append(
            {
                "source": "fixture-direct-top20",
                "tournament_id": row.tournament_id,
                "player_id": row.player_id,
                "player_name": row.player_name,
                "odds_as_of": odds_as_of.isoformat(),
                "market_type": "top20",
                "decimal_odds": "",
                "odds_probability": row.odds_probability,
                "reference_price_source": "fixture-direct-top20",
                "overround": "",
            }
        )
    return features, labels, snapshots, odds_rows


def normalize_decimal_odds_rows(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Devig decimal odds boards into normalized probability rows.

    Rows with ``odds_probability`` already present pass through. Rows with only
    ``decimal_odds`` are grouped by source/tournament/market_type/as_of and
    de-vigged within that board.
    """

    direct: list[dict[str, object]] = []
    boards: defaultdict[tuple[str, str, str, str], dict[str, float]] = defaultdict(dict)
    by_player_row: dict[tuple[str, str, str, str, str], Mapping[str, object]] = {}
    for row in rows:
        probability = _optional_float(row.get("odds_probability") or row.get("probability"))
        if probability is not None:
            direct.append({**dict(row), "odds_probability": probability})
            continue
        decimal_odds = _optional_float(row.get("decimal_odds"))
        if decimal_odds is None:
            continue
        source = str(row.get("source") or "unknown")
        tournament_id = str(row.get("tournament_id") or "")
        market_type = str(row.get("market_type") or "outright_reference")
        odds_as_of = str(row.get("odds_as_of") or row.get("last_update") or "")
        player_id = str(row.get("player_id") or row.get("player_name") or "")
        board_key = (source, tournament_id, market_type, odds_as_of)
        boards[board_key][player_id] = decimal_odds
        by_player_row[(*board_key, player_id)] = row
    normalized = list(direct)
    for board_key, odds_board in boards.items():
        if not odds_board:
            continue
        devigged = devig_decimal_odds(odds_board)
        source, tournament_id, market_type, odds_as_of = board_key
        for player_id, probability in devigged.probabilities.items():
            raw = by_player_row[(*board_key, player_id)]
            normalized.append(
                {
                    **dict(raw),
                    "source": source,
                    "tournament_id": tournament_id,
                    "player_id": player_id,
                    "odds_as_of": odds_as_of,
                    "market_type": market_type,
                    "odds_probability": probability,
                    "reference_price_source": f"{source}:{market_type}",
                    "overround": devigged.overround,
                }
            )
    return normalized


def _build_row_from_inputs(
    feature: Mapping[str, object],
    label: Mapping[str, object],
    *,
    snapshot: Mapping[str, object] | None,
    odds: Mapping[str, object] | None,
    top_n: int,
    scheduled_start: datetime,
) -> GolfPreRoundRow:
    market_bid = _snapshot_price(snapshot, "yes_bid") if snapshot is not None else None
    market_ask = _snapshot_price(snapshot, "yes_ask") if snapshot is not None else None
    odds_probability = _optional_float(odds.get("odds_probability")) if odds is not None else None
    source = str(odds.get("reference_price_source") or odds.get("source") or "") if odds is not None else ""
    numeric = {feature_name: _optional_float(feature.get(feature_name)) or 0.0 for feature_name in MODEL_FEATURES}
    if market_bid is not None and market_ask is not None:
        numeric["market_mid"] = (market_bid + market_ask) / 2.0
        numeric["spread"] = market_ask - market_bid
    if snapshot is not None:
        liquidity = _first_optional_float(snapshot, ("liquidity", "open_interest", "volume_24h", "volume"))
        if liquidity is not None:
            numeric["liquidity"] = liquidity
    if "time_to_start_hours" not in feature or str(feature.get("time_to_start_hours") or "").strip() == "":
        feature_as_of = _parse_datetime(_required(feature, "feature_as_of"))
        numeric["time_to_start_hours"] = max(0.0, (scheduled_start - feature_as_of).total_seconds() / 3600.0)
    return GolfPreRoundRow(
        event_date=_parse_date(_required(feature, "event_date")),
        tournament_id=_required(feature, "tournament_id"),
        player_id=_required(feature, "player_id"),
        player_name=str(feature.get("player_name") or feature.get("player_id") or ""),
        target=_target_from_label(label, top_n=top_n),
        top_n=top_n,
        field_size=int(_optional_float(feature.get("field_size")) or 0.0),
        course_archetype=str(feature.get("course_archetype") or "unknown"),
        tee_wave=str(feature.get("tee_wave") or "unknown"),
        round_number=int(_optional_float(feature.get("round_number")) or 1.0),
        numeric=numeric,
        market_bid=market_bid,
        market_ask=market_ask,
        odds_probability=odds_probability,
        reference_price_source=source or None,
    )


def _labels_by_key(
    rows: Sequence[Mapping[str, object]],
    *,
    top_n: int,
) -> dict[tuple[str, str, int], Mapping[str, object]]:
    labels: dict[tuple[str, str, int], Mapping[str, object]] = {}
    for row in rows:
        _require_columns(row, LABEL_REQUIRED_COLUMNS, "label row")
        labels[_row_key(row, top_n=top_n)] = row
    return labels


def _latest_snapshots_by_key(
    rows: Sequence[Mapping[str, object]],
) -> dict[tuple[str, str, int], tuple[Mapping[str, object], ...]]:
    grouped: defaultdict[tuple[str, str, int], list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        key = _row_key(row, top_n=int(_optional_float(row.get("top_n")) or 20.0))
        grouped[key].append(row)
    return {
        key: tuple(sorted(items, key=lambda item: _parse_datetime(_required(item, "captured_at"))))
        for key, items in grouped.items()
    }


def _odds_probabilities_by_key(
    rows: Sequence[Mapping[str, object]],
) -> dict[tuple[str, str, int], tuple[Mapping[str, object], ...]]:
    normalized = normalize_decimal_odds_rows(rows)
    grouped: defaultdict[tuple[str, str, int], list[Mapping[str, object]]] = defaultdict(list)
    for row in normalized:
        key = _row_key(row, top_n=int(_optional_float(row.get("top_n")) or 20.0))
        grouped[key].append(row)
    return {
        key: tuple(sorted(items, key=lambda item: _parse_datetime(_required(item, "odds_as_of"))))
        for key, items in grouped.items()
    }


def _latest_before(
    rows: Sequence[Mapping[str, object]],
    cutoff: datetime,
    *,
    timestamp_field: str,
) -> Mapping[str, object] | None:
    latest: Mapping[str, object] | None = None
    latest_ts: datetime | None = None
    for row in rows:
        ts = _parse_datetime(_required(row, timestamp_field))
        if ts > cutoff:
            raise ValueError(f"{timestamp_field} for {_row_key(row, top_n=20)} is after scheduled_start")
        if latest_ts is None or ts > latest_ts:
            latest = row
            latest_ts = ts
    return latest


def _target_from_label(label: Mapping[str, object], *, top_n: int) -> int:
    made = _optional_float(label.get("made_top_n"))
    if made is None:
        made = _optional_float(label.get("target"))
    if made is not None:
        return 1 if made >= 0.5 else 0
    position = _optional_float(label.get("final_position") or label.get("position"))
    if position is None:
        raise ValueError("label row needs made_top_n/target or final_position")
    return 1 if position <= top_n else 0


def _row_key(row: Mapping[str, object], *, top_n: int) -> tuple[str, str, int]:
    row_top_n = int(_optional_float(row.get("top_n")) or float(top_n))
    return (_required(row, "tournament_id"), _required(row, "player_id"), row_top_n)


def _snapshot_price(row: Mapping[str, object], field: str) -> float | None:
    alternatives = (field, f"{field}_dollars", field.replace("yes_", "market_"))
    return _first_optional_float(row, alternatives)


def _first_optional_float(row: Mapping[str, object], fields: Sequence[str]) -> float | None:
    for field in fields:
        parsed = _optional_float(row.get(field))
        if parsed is not None:
            return parsed
    return None


def _write_dicts(path: Path, columns: Sequence[str], rows: Sequence[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def _require_columns(row: Mapping[str, object], columns: Sequence[str], kind: str) -> None:
    for column in columns:
        if str(row.get(column) or "").strip() == "":
            raise ValueError(f"{kind} missing required column {column!r}")


def _required(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if value is None or str(value).strip() == "":
        raise ValueError(f"missing required field {field!r}")
    return str(value).strip()


def _optional_float(value: object) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    parsed = float(str(value))
    if not math.isfinite(parsed):
        raise ValueError("numeric value must be finite")
    return parsed


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_date(value: str) -> date:
    return _parse_datetime(value).date()
