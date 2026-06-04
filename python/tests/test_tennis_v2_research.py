"""Tennis v2 research pipeline tests.

Covers the schema/contract, stateless feature arithmetic, the antisymmetric
inference guarantee (P(p1)+P(p2)=1 by construction), no-leakage in the stateful
frame builder, the odds name-matcher, the odds→match merge, and the loader's
skip-unreadable-file robustness. xgboost-dependent tests importorskip so the
pure-Python coverage still runs in a minimal environment.
"""

from __future__ import annotations

import json
import struct
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from eventcontracts.contracts import load_json_contract, validate_contract
from eventcontracts.research import tennis_odds as odds
from eventcontracts.research import tennis_v2 as v2
from tests.conftest import REPO_ROOT

# polars is a research dependency; skip the whole module (rather than abort
# collection of the entire suite) when it is not installed.
pl = pytest.importorskip("polars")


# --------------------------------------------------------------------------
# schema + contract
# --------------------------------------------------------------------------
def test_v2_schema_validates_against_feature_contract() -> None:
    schema = v2.feature_schema_document()
    contract = load_json_contract(REPO_ROOT / "contracts/schemas/feature_schema.schema.json")
    validate_contract(schema, contract)
    assert schema["schema_version"] == "2"
    assert [f["name"] for f in schema["features"]] == list(v2.TENNIS_V2_FEATURE_NAMES)


def test_v2_feature_names_unique_and_monotone_aligned() -> None:
    names = v2.TENNIS_V2_FEATURE_NAMES
    assert len(names) == len(set(names))
    constraints = v2.monotone_constraints()
    assert len(constraints) == len(names)
    assert set(constraints) <= {-1, 0, 1}
    # Strength/market features must push p1 the right way (non-negative).
    by_name = dict(zip(names, constraints, strict=True))
    for must_be_increasing in ("elo_diff", "elo_blend_diff", "p1_implied_prob", "implied_prob_diff"):
        assert by_name[must_be_increasing] == 1


# --------------------------------------------------------------------------
# stateless feature arithmetic
# --------------------------------------------------------------------------
def _snap(**kw: object) -> v2.TennisV2Snapshot:
    base: dict[str, object] = dict(
        match_id="m-1", match_date=date(2026, 1, 1), p1_id="a", p2_id="b"
    )
    base.update(kw)
    return v2.TennisV2Snapshot(**base)  # type: ignore[arg-type]


def test_feature_row_covers_exactly_the_schema_and_is_deterministic() -> None:
    row = v2.feature_row_v2(_snap())
    assert set(row) == set(v2.TENNIS_V2_FEATURE_NAMES)
    assert v2.feature_row_v2(_snap()) == row
    assert v2.feature_vector_v2(_snap()) == tuple(row[n] for n in v2.TENNIS_V2_FEATURE_NAMES)


def test_feature_arithmetic_key_fields() -> None:
    row = v2.feature_row_v2(
        _snap(
            p1_elo=1600.0,
            p2_elo=1500.0,
            surface="Clay",
            best_of=5,
            tourney_level="G",
            round="QF",
            p1_hand="L",
            p2_hand="R",
            p1_days_since_match=100,  # clamped to 30
            p2_days_since_match=3,
        )
    )
    assert row["elo_diff"] == 100.0
    assert row["surface_clay"] == 1.0 and row["surface_hard"] == 0.0
    assert row["best_of_5"] == 1.0 and row["is_grand_slam"] == 1.0
    assert row["hand_matchup"] == 1.0
    assert row["round_ordinal"] == pytest.approx(5.0 / 7.0)
    assert row["days_rest_diff"] == pytest.approx(30.0 - 3.0)


def test_absent_odds_yield_neutral_market_block() -> None:
    row = v2.feature_row_v2(_snap())  # no decimal odds
    assert row["p1_implied_prob"] == 0.5
    assert row["implied_prob_diff"] == 0.0
    assert row["odds_present"] == 0.0


def test_present_odds_normalize_and_flag() -> None:
    # Realistic vigged book: 1/1.4 + 1/2.5 = 0.714 + 0.4 = 1.114 overround.
    row = v2.feature_row_v2(_snap(p1_decimal_odds=1.4, p2_decimal_odds=2.5))
    assert row["odds_present"] == 1.0
    assert row["p1_implied_prob"] > 0.5  # favorite after overround-normalization
    assert row["implied_prob_diff"] > 0.0
    assert row["odds_overround"] > 1.0  # bookmaker margin present


def test_write_v2_feature_schema_and_confidence_gate_metrics(tmp_path: Path) -> None:
    schema_path = v2.write_feature_schema(tmp_path / "feature_schema.json")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    gates = v2.confidence_gate_metrics([1, 0, 1, 0], [0.8, 0.6, 0.55, 0.49], cutoffs=(0.55, 0.7))

    assert schema["schema_version"] == "2"
    assert [f["name"] for f in schema["features"]] == list(v2.TENNIS_V2_FEATURE_NAMES)
    assert gates[0]["cutoff"] == 0.55
    assert gates[0]["samples"] == 3
    assert gates[0]["accuracy"] == pytest.approx(2 / 3)
    assert gates[1]["samples"] == 1


# --------------------------------------------------------------------------
# odds name-matcher
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sackmann,tennis_data,expected",
    [
        ("Marcos Giron", "Giron M.", "giron|m"),
        ("Richard Gasquet", "Gasquet R.", "gasquet|r"),
        ("Felix Auger-Aliassime", "Auger Aliassime F.", "augeraliassime|f"),
        ("Christopher O'Connell", "O Connell C.", "oconnell|c"),
    ],
)
def test_name_keys_agree_across_sources(sackmann: str, tennis_data: str, expected: str) -> None:
    assert odds.sackmann_name_key(sackmann) == expected
    assert odds.tennis_data_name_key(tennis_data) == expected


def test_name_keys_reject_single_token() -> None:
    assert odds.sackmann_name_key("Madonna") is None
    assert odds.tennis_data_name_key("Pele") is None


# --------------------------------------------------------------------------
# odds merge
# --------------------------------------------------------------------------
def test_merge_attaches_odds_within_window_and_nulls_outside() -> None:
    matches = pl.DataFrame(
        {
            "winner_id": ["1", "2"],
            "loser_id": ["2", "1"],
            "winner_name": ["Marcos Giron", "Richard Gasquet"],
            "loser_name": ["Richard Gasquet", "Marcos Giron"],
            "tourney_date": [20230102, 20230601],
        }
    )
    odds_frame = pl.DataFrame(
        {
            "winner_key": ["giron|m"],
            "loser_key": ["gasquet|r"],
            "match_date": [date(2023, 1, 3)],  # +1 day from tourney start -> in window
            "winner_decimal_odds": [1.5],
            "loser_decimal_odds": [2.5],
        }
    )
    merged = odds.merge_odds_into_matches(matches, odds_frame)
    got = merged.sort("tourney_date")
    assert got["winner_decimal_odds"].to_list()[0] == pytest.approx(1.5)
    # second match (June) shares no odds row -> null
    assert got["winner_decimal_odds"].to_list()[1] is None
    assert odds.odds_match_rate(merged) == pytest.approx(0.5)


def test_merge_respects_forward_window() -> None:
    matches = pl.DataFrame(
        {
            "winner_id": ["1"],
            "loser_id": ["2"],
            "winner_name": ["Marcos Giron"],
            "loser_name": ["Richard Gasquet"],
            "tourney_date": [20230101],
        }
    )
    odds_frame = pl.DataFrame(
        {
            "winner_key": ["giron|m"],
            "loser_key": ["gasquet|r"],
            "match_date": [date(2023, 3, 1)],  # ~59 days later, outside 21-day window
            "winner_decimal_odds": [1.5],
            "loser_decimal_odds": [2.5],
        }
    )
    merged = odds.merge_odds_into_matches(matches, odds_frame)
    assert merged["winner_decimal_odds"].to_list()[0] is None


# --------------------------------------------------------------------------
# loader robustness (the .xls-disguised-as-.xlsx skip fix)
# --------------------------------------------------------------------------
def test_looks_like_xlsx_distinguishes_zip_from_ole2(tmp_path: Path) -> None:
    good = tmp_path / "good.xlsx"
    good.write_bytes(b"PK\x03\x04rest-of-zip")
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(struct.pack("<Q", 0xE011CFD0A1B11AE1))  # OLE2 magic
    assert odds._looks_like_xlsx(good) is True
    assert odds._looks_like_xlsx(bad) is False


def test_loader_raises_when_no_readable_files(tmp_path: Path) -> None:
    (tmp_path / "2012.xlsx").write_bytes(b"\xd0\xcf\x11\xe0not-a-zip")
    # Skips the unreadable file (with a warning) and, finding nothing readable,
    # raises rather than returning an empty frame.
    with pytest.warns(UserWarning, match="skipped unreadable"), pytest.raises(FileNotFoundError):
        odds.load_tennis_data_odds(tmp_path)


def test_loader_missing_source_raises() -> None:
    with pytest.raises(FileNotFoundError):
        odds.load_tennis_data_odds(REPO_ROOT / "does/not/exist")


# --------------------------------------------------------------------------
# stateful frame builder — no leakage
# --------------------------------------------------------------------------
def _synthetic_matches() -> pl.DataFrame:
    # Three chronological matches among three players, with serve stats + score.
    return pl.DataFrame(
        {
            "winner_id": ["A", "A", "C"],
            "loser_id": ["B", "C", "B"],
            "winner_name": ["Al Pha", "Al Pha", "Cy Gamma"],
            "loser_name": ["Be Ta", "Cy Gamma", "Be Ta"],
            "tourney_date": [20230102, 20230116, 20230130],
            "surface": ["Hard", "Hard", "Clay"],
            "best_of": [3, 3, 3],
            "score": ["6-4 6-4", "7-6 6-7 6-3", "6-2 6-2"],
            "w_ace": [5, 8, 3],
            "w_df": [2, 1, 4],
            "w_svpt": [70, 90, 60],
            "w_1stWon": [40, 55, 35],
            "w_2ndWon": [15, 18, 12],
            "w_bpSaved": [3, 5, 1],
            "w_bpFaced": [4, 6, 2],
            "l_ace": [4, 3, 2],
            "l_df": [3, 5, 6],
            "l_svpt": [68, 85, 58],
            "l_1stWon": [38, 48, 30],
            "l_2ndWon": [12, 14, 10],
            "l_bpSaved": [2, 3, 1],
            "l_bpFaced": [5, 7, 5],
        }
    )


def test_frame_builder_emits_features_and_no_self_leakage() -> None:
    frame = v2.build_v2_training_frame(_synthetic_matches(), include_mirrored=True)
    # mirrored doubling
    assert frame.height == 6
    for name in v2.TENNIS_V2_FEATURE_NAMES:
        assert name in frame.columns
        assert frame[name].null_count() == 0
    assert "label" in frame.columns and "match_date" in frame.columns
    # mirroring keeps the label set balanced.
    assert frame["label"].sum() == 3
    # No-leakage: the very first match (chronologically) sees both players at the
    # 1500 Elo prior, so elo_diff/blend must be 0 — the result is not baked in.
    first = frame.sort("match_date").row(0, named=True)
    assert first["elo_diff"] == 0.0
    assert first["elo_blend_diff"] == 0.0


def test_frame_builder_requires_core_columns() -> None:
    with pytest.raises(ValueError, match="missing required"):
        v2.build_v2_training_frame(pl.DataFrame({"winner_id": ["A"]}))


# --------------------------------------------------------------------------
# dynamic-K Elo: experience decay + layoff boost
# --------------------------------------------------------------------------
def test_dynamic_k_shrinks_with_experience_and_grows_after_layoff() -> None:
    base_k = 250.0
    rookie = v2._dynamic_k(0, None, base_k=base_k, layoff_boost=0.5)
    veteran = v2._dynamic_k(200, None, base_k=base_k, layoff_boost=0.5)
    assert rookie == pytest.approx(base_k / (5**0.4))
    assert veteran < rookie  # experience stabilises the rating

    fresh = v2._dynamic_k(50, 10, base_k=base_k, layoff_boost=0.5)
    at_grace = v2._dynamic_k(50, v2._ELO_LAYOFF_GRACE_DAYS, base_k=base_k, layoff_boost=0.5)
    full = v2._dynamic_k(50, v2._ELO_LAYOFF_CAP_DAYS, base_k=base_k, layoff_boost=0.5)
    assert fresh == at_grace  # inside the grace window → no boost
    assert full == pytest.approx(fresh * 1.5)  # at the cap → (1 + boost)x
    # clamped past the cap, and disabled by layoff_boost=0 or an unknown last match.
    assert v2._dynamic_k(50, 10_000, base_k=base_k, layoff_boost=0.5) == full
    assert v2._dynamic_k(50, v2._ELO_LAYOFF_CAP_DAYS, base_k=base_k, layoff_boost=0.0) == fresh
    assert v2._dynamic_k(50, None, base_k=base_k, layoff_boost=0.5) == fresh


def _repeated_pairing(dates: list[int]) -> pl.DataFrame:
    n = len(dates)
    return pl.DataFrame(
        {
            "winner_id": ["A"] * n,
            "loser_id": ["B"] * n,
            "tourney_date": dates,
            "surface": ["Hard"] * n,
            "best_of": [3] * n,
            "score": ["6-4 6-4"] * n,
        }
    )


def test_layoff_boost_is_inert_at_normal_cadence_but_engages_after_a_gap() -> None:
    # Normal 14-day cadence: every gap is inside the grace window, so the layoff
    # term changes nothing — a model trained without it stays consistent.
    normal = _repeated_pairing([20230101, 20230115, 20230129])
    assert (
        v2.build_v2_training_frame(normal, include_mirrored=False, elo_layoff_boost=0.0)["elo_diff"].to_list()
        == v2.build_v2_training_frame(normal, include_mirrored=False, elo_layoff_boost=0.5)["elo_diff"].to_list()
    )
    # A ~200-day gap before match 2 means match 2's update is layoff-boosted, so by
    # match 3 (which sees that update) the winner A is rated further above B.
    gapped = _repeated_pairing([20230101, 20230720, 20230725])
    e0 = v2.build_v2_training_frame(gapped, include_mirrored=False, elo_layoff_boost=0.0).sort("match_date")["elo_diff"]
    e5 = v2.build_v2_training_frame(gapped, include_mirrored=False, elo_layoff_boost=0.5).sort("match_date")["elo_diff"]
    assert e0[0] == e5[0] == 0.0  # first meeting: shared 1500 prior
    assert e0[1] == pytest.approx(e5[1])  # match 2 update was the players' first → no layoff yet
    assert e5[2] > e0[2]  # boosted match-2 update widens the gap seen at match 3


# --------------------------------------------------------------------------
# training + antisymmetric inference (xgboost-dependent)
# --------------------------------------------------------------------------
def _trained_model_and_test() -> tuple[Any, pl.DataFrame]:
    pytest.importorskip("xgboost")
    frame = v2.build_v2_training_frame(_synthetic_matches(), include_mirrored=True)
    # Tiny, fast: no validation/early-stopping, few rounds, monotone on.
    model = v2.train_v2(frame, None, num_boost_round=20, early_stopping_rounds=0)
    return model, frame


def test_train_and_predict_in_unit_interval() -> None:
    model, frame = _trained_model_and_test()
    preds = v2.predict_v2(model, frame)
    assert len(preds) == frame.height
    assert all(0.0 <= p <= 1.0 for p in preds)


def test_committed_parity_fixture_is_current() -> None:
    """The committed Python<->Rust fixture must equal a fresh generation.

    If a v2 feature changes without regenerating the fixture, this fails — and
    the Rust `feature_vector_matches_python_fixture` test would then be checking
    against stale numbers. The two tests together pin both sides.
    """
    fixture_path = REPO_ROOT / v2.PARITY_FIXTURE_RELPATH
    committed = json.loads(fixture_path.read_text())
    fresh = json.loads(json.dumps(v2.feature_parity_fixture()))
    assert committed == fresh, (
        f"{v2.PARITY_FIXTURE_RELPATH} is stale; regenerate from "
        "feature_parity_fixture() after changing v2 features"
    )
    assert committed["feature_names"] == list(v2.TENNIS_V2_FEATURE_NAMES)


def test_antisymmetric_inference_gives_coherent_two_sided_prices() -> None:
    """P(x) + P(swap(x)) must equal 1 exactly, for any model."""
    model, frame = _trained_model_and_test()
    forward = frame.select(v2.TENNIS_V2_FEATURE_NAMES)
    swapped_matrix = v2._swap_features(forward.to_numpy())
    swapped = pl.DataFrame(swapped_matrix, schema=list(v2.TENNIS_V2_FEATURE_NAMES))

    p_fwd = v2.predict_v2_antisymmetric(model, forward)
    p_swp = v2.predict_v2_antisymmetric(model, swapped)
    for a, b in zip(p_fwd, p_swp, strict=True):
        assert a + b == pytest.approx(1.0, abs=1e-9)
