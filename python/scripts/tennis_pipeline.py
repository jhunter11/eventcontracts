"""Plane A (research/data) producer: auto-build a tennis snapshot bundle.

ONE-SHOT CLI (run from cron / a scheduler / a RunPod job). It:

  1. DISCOVERS open Kalshi ATP match markets (KXATPMATCH) and groups the two
     legs of each match by event, reading player names from ``yes_sub_title``.
  2. CALCS ODDS via a pluggable provider (The Odds API sharp line by default, or
     a manual ``player,decimal_odds`` CSV) and, for The Odds API, the match
     commence_time so already-started matches are skipped (the model is
     pre-match).
  3. RESOLVES each player to Sackmann history and BUILDS the v2 snapshot
     (`tennis_v2.build_upcoming_snapshot`) with odds attached.
  4. WRITES a self-contained bundle dir:
        <out>/snapshots.jsonl   one row per tradeable market_id (YES = favorite leg)
        <out>/manifest.json     provenance + the --tickers list + freshness data
     The execution plane (the Rust live-runner, possibly on separate compute)
     consumes exactly these two files; see tennis_run_from_bundle.py.

Plane separation: this plane needs only the Sackmann CSVs + an odds source — NO
model/ONNX (scoring happens in the Rust runner on the execution plane). So it
runs fine on cheap/ephemeral compute (RunPod); sync the bundle dir to the
execution host (object store / runpodctl / rsync). The manifest's per-match
commence_time + generated_at let the execution side refuse stale snapshots.

ATP only: the promoted model is ATP-trained and there is no WTA history in the
repo, so KXWTAMATCH is intentionally NOT produced.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))
DEFAULT_HISTORY = ROOT / "data" / "tennis" / "tennis_atp" / "tennis_atp-master"
DEFAULT_BUNDLE = "sports_tennis_xgboost__live-candidate-20260530"
KALSHI_HOST = "https://api.elections.kalshi.com/trade-api/v2"
ODDS_API = "https://api.the-odds-api.com/v4"

from eventcontracts.research import tennis_roster as roster  # noqa: E402
from eventcontracts.research import tennis_v2 as t2  # noqa: E402


def _norm(name: str) -> str:
    return " ".join(str(name).strip().lower().split())


def _surname(name: str) -> str:
    n = _norm(name)
    return n.split(" ")[-1] if n else ""


def _get(url: str, headers: dict | None = None, timeout: int = 30, tries: int = 3) -> tuple[object, dict]:
    """GET JSON with light retry/backoff so a transient blip doesn't kill an
    unattended (cron) cycle. Retries network errors + HTTP 429/5xx."""
    h = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    if headers:
        h.update(headers)
    last: Exception | None = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read()), dict(r.headers)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            if attempt == tries - 1:
                raise
        time.sleep(2 * (attempt + 1))
    raise last  # type: ignore[misc]


_ROUND_PATTERNS = [
    ("round of 128", "R128"),
    ("round of 64", "R64"),
    ("round of 32", "R32"),
    ("round of 16", "R16"),
    ("quarterfinal", "QF"),
    ("semifinal", "SF"),
    ("final", "F"),
]


def _round_from_title(title: str, fallback: str) -> str:
    """Map a Kalshi match title ('... : Round Of 16 match?') to a Sackmann round
    code so the bundle stays correct as a tournament advances (R16 -> QF -> SF ->
    F) without re-editing the cron flags. Falls back to ``--round`` if unknown."""
    low = (title or "").lower()
    for needle, code in _ROUND_PATTERNS:
        if needle in low:
            return code
    return fallback


# --------------------------------------------------------------------------- #
# 1. Kalshi discovery
# --------------------------------------------------------------------------- #
@dataclass
class Match:
    event_ticker: str
    legs: dict[str, str] = field(default_factory=dict)  # player_name -> leg ticker
    asks: dict[str, float] = field(default_factory=dict)  # player_name -> yes_ask
    bids: dict[str, float] = field(default_factory=dict)
    target_date: str = ""  # YYYY-MM-DD from the event ticker
    title: str = ""  # a leg title, for round auto-detection

    @property
    def players(self) -> list[str]:
        return list(self.legs)


def _f(x: object) -> float:
    try:
        return float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _date_from_event(event_ticker: str) -> str:
    # KXATPMATCH-26JUN01TIAARN -> 26JUN01 -> 2026-06-01
    try:
        seg = event_ticker.split("-")[1]
        return datetime.strptime(seg[:7], "%y%b%d").date().isoformat()
    except (IndexError, ValueError):
        return ""


def discover_matches(series: str, host: str) -> list[Match]:
    matches: dict[str, Match] = {}
    cursor: str | None = None
    for _ in range(10):
        params = {"series_ticker": series, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        payload, _hdr = _get(f"{host}/markets?{urllib.parse.urlencode(params)}")
        markets = payload.get("markets", []) if isinstance(payload, dict) else []
        for m in markets:
            if not isinstance(m, dict) or m.get("status") != "active":
                continue
            ev = m.get("event_ticker")
            name = m.get("yes_sub_title")
            ticker = m.get("ticker")
            if not (ev and name and ticker):
                continue
            match = matches.setdefault(ev, Match(event_ticker=ev, target_date=_date_from_event(ev)))
            match.legs[name] = ticker
            match.asks[name] = _f(m.get("yes_ask_dollars"))
            match.bids[name] = _f(m.get("yes_bid_dollars"))
            if not match.title:
                match.title = str(m.get("title") or "")
        cursor = payload.get("cursor") if isinstance(payload, dict) else None
        if not cursor:
            break
    # keep only well-formed two-leg matches
    return [m for m in matches.values() if len(m.legs) == 2 and m.target_date]


# --------------------------------------------------------------------------- #
# 2. Odds providers (pluggable)
# --------------------------------------------------------------------------- #
@dataclass
class MatchOdds:
    odds_by_norm_name: dict[str, float]
    commence_time: str | None = None

    def odds_for(self, player: str) -> float | None:
        return self.odds_by_norm_name.get(_norm(player)) or self.odds_by_norm_name.get(_surname(player))


class OddsProvider:
    name = "base"

    def odds_for_match(self, match: Match) -> MatchOdds | None:  # pragma: no cover - interface
        raise NotImplementedError


class TheOddsApiProvider(OddsProvider):
    name = "the_odds_api"

    def __init__(self, api_key: str, sport_key: str, regions: str = "eu,uk,us", book: str = "pinnacle"):
        self.api_key = api_key
        self.sport_key = sport_key
        self.regions = regions
        self.book = book
        self._events: list[dict] | None = None

    def _load(self) -> list[dict]:
        if self._events is None:
            url = f"{ODDS_API}/sports/{self.sport_key}/odds?" + urllib.parse.urlencode(
                {"apiKey": self.api_key, "regions": self.regions, "markets": "h2h", "oddsFormat": "decimal"}
            )
            data, _hdr = _get(url)
            self._events = data if isinstance(data, list) else []
        return self._events

    def _extract(self, event: dict) -> dict[str, float]:
        prices: dict[str, list[float]] = {}
        preferred: dict[str, float] = {}
        for bk in event.get("bookmakers", []):
            is_pref = bk.get("key") == self.book
            for mkt in bk.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                for oc in mkt.get("outcomes", []):
                    nm, pr = oc.get("name"), oc.get("price")
                    if nm is None or pr is None:
                        continue
                    prices.setdefault(_norm(nm), []).append(float(pr))
                    if is_pref:
                        preferred[_norm(nm)] = float(pr)
        return {nm: preferred.get(nm) or sum(pl_) / len(pl_) for nm, pl_ in prices.items()}

    def odds_for_match(self, match: Match) -> MatchOdds | None:
        want = {_surname(p) for p in match.players}
        for ev in self._load():
            names = [ev.get("home_team", ""), ev.get("away_team", "")]
            if {_surname(n) for n in names if n} != want:
                continue
            priced = self._extract(ev)
            # re-key by surname too so match.players (Kalshi spellings) resolve
            by_name: dict[str, float] = {}
            for nm, od in priced.items():
                by_name[nm] = od
                by_name[_surname(nm)] = od
            return MatchOdds(odds_by_norm_name=by_name, commence_time=ev.get("commence_time"))
        return None


class ManualCsvProvider(OddsProvider):
    name = "manual_csv"

    def __init__(self, csv_path: Path):
        self.by_name: dict[str, float] = {}
        with csv_path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                nm = row.get("player")
                od = row.get("decimal_odds")
                if nm and od:
                    self.by_name[_norm(nm)] = float(od)
                    self.by_name[_surname(nm)] = float(od)

    def odds_for_match(self, match: Match) -> MatchOdds | None:
        got = {_norm(p): self.by_name.get(_norm(p)) or self.by_name.get(_surname(p)) for p in match.players}
        if all(v for v in got.values()):
            return MatchOdds(odds_by_norm_name={k: v for k, v in got.items() if v})
        return None


# --------------------------------------------------------------------------- #
# 3 + 4. Build snapshots + write bundle
# --------------------------------------------------------------------------- #
def _opt_float(v: object) -> float | None:
    try:
        return float(v) if v is not None else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _opt_int(v: object) -> int | None:
    f = _opt_float(v)
    return None if f is None else int(f)


def build_bundle(args: argparse.Namespace) -> int:
    now = datetime.now(UTC)
    matches = discover_matches(args.series, args.kalshi_host)
    print(f"discovered {len(matches)} open {args.series} matches")
    if not matches:
        print("no open matches; nothing to do")
        return 0

    if args.odds_provider == "the-odds-api":
        if not args.api_key:
            print("ERROR: --odds-provider the-odds-api needs THE_ODDS_API_KEY or --api-key")
            return 2
        provider: OddsProvider = TheOddsApiProvider(args.api_key, args.sport_key, book=args.book)
    else:
        if not args.odds_csv or not args.odds_csv.exists():
            print("ERROR: --odds-provider manual needs an existing --odds-csv")
            return 2
        provider = ManualCsvProvider(args.odds_csv)
    print(f"odds provider: {provider.name}")

    try:
        hist = roster.load_history(args.history_dir)
    except FileNotFoundError:
        print(
            f"ERROR: no Sackmann ATP history under {args.history_dir}.\n"
            "  This plane needs the JeffSackmann/tennis_atp CSVs. On a fresh host:\n"
            "    git clone --depth 1 https://github.com/JeffSackmann/tennis_atp \\\n"
            f"      {args.history_dir}\n"
            "  (or transfer your local data dir), then re-run. Override location with --history-dir."
        )
        return 2
    table = roster.build_player_table(hist)

    rows: list[dict] = []
    manifest_matches: list[dict] = []
    skipped: list[str] = []
    for match in matches:
        mo = provider.odds_for_match(match)
        if mo is None:
            skipped.append(f"{match.event_ticker} (no odds)")
            continue
        if mo.commence_time and not args.include_started:
            try:
                ct = datetime.fromisoformat(mo.commence_time.replace("Z", "+00:00"))
                if ct <= now:
                    skipped.append(f"{match.event_ticker} (already started {mo.commence_time})")
                    continue
            except ValueError:
                pass
        players = match.players
        odds = {p: mo.odds_for(p) for p in players}
        if not all(odds.values()):
            skipped.append(f"{match.event_ticker} (incomplete odds)")
            continue
        # YES side = the favorite (lower decimal odds), deterministic.
        p1 = min(players, key=lambda p: odds[p])  # favorite
        p2 = next(p for p in players if p != p1)
        market_id = match.legs[p1]
        try:
            r1 = roster.resolve_player(table, p1)
            r2 = roster.resolve_player(table, p2)
        except roster.PlayerNotFound as exc:
            skipped.append(f"{match.event_ticker} ({exc})")
            continue

        rnd = _round_from_title(match.title, args.round)
        snap = t2.build_upcoming_snapshot(
            hist,
            p1_id=str(r1["pid"]),
            p2_id=str(r2["pid"]),
            match_date=datetime.strptime(match.target_date, "%Y-%m-%d").date(),
            surface=args.surface,
            best_of=args.best_of,
            round=rnd,
            tourney_level=args.tourney_level,
            p1_rank=_opt_int(r1.get("rank")),
            p2_rank=_opt_int(r2.get("rank")),
            p1_rank_points=_opt_float(r1.get("rank_points")),
            p2_rank_points=_opt_float(r2.get("rank_points")),
            p1_age=_opt_float(r1.get("age")),
            p2_age=_opt_float(r2.get("age")),
            p1_height_cm=_opt_float(r1.get("ht")),
            p2_height_cm=_opt_float(r2.get("ht")),
            p1_hand=str(r1.get("hand") or "U"),
            p2_hand=str(r2.get("hand") or "U"),
            p1_decimal_odds=odds[p1],
            p2_decimal_odds=odds[p2],
            match_id=market_id,
        )
        payload = dataclasses.asdict(snap)
        payload["match_date"] = snap.match_date.isoformat()
        payload["p1_name"] = r1["name"]
        payload["p2_name"] = r2["name"]
        rows.append({"market_id": market_id, "source": "tennis_xgboost_onnx", **payload})
        manifest_matches.append(
            {
                "market_id": market_id,
                "event": match.event_ticker,
                "round": rnd,
                "p1": r1["name"],
                "p2": r2["name"],
                "p1_odds": odds[p1],
                "p2_odds": odds[p2],
                "commence_time": mo.commence_time,
                "kalshi_yes_bid": match.bids.get(p1),
                "kalshi_yes_ask": match.asks.get(p1),
            }
        )
        print(f"  built {market_id} [{rnd}]: {r1['name']} (fav {odds[p1]}) vs {r2['name']} ({odds[p2]})")

    if not rows:
        print(f"\nno tradeable snapshots built. skipped: {skipped}")
        return 1

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    snap_path = out / "snapshots.jsonl"
    with snap_path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    commences = [m["commence_time"] for m in manifest_matches if m["commence_time"]]
    manifest = {
        "schema": "tennis-pipeline-bundle/v1",
        "generated_at": now.isoformat(),
        "series": args.series,
        "tournament": {
            "surface": args.surface,
            "tourney_level": args.tourney_level,
            "best_of": args.best_of,
            "round": args.round,
        },
        "model": {"expect_tennis_schema_version": "2", "bundle": args.bundle_name},
        "odds": {"provider": provider.name, "sport_key": args.sport_key, "book": args.book},
        "matches": manifest_matches,
        "tickers": [m["market_id"] for m in manifest_matches],
        "earliest_commence": min(commences) if commences else None,
        "skipped": skipped,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"\nwrote bundle -> {out}")
    print(f"  snapshots.jsonl: {len(rows)} match(es)")
    print(f"  tickers: {' '.join(manifest['tickers'])}")
    if skipped:
        print(f"  skipped: {len(skipped)} -> {skipped}")
    print("\nExecution plane: sync this dir to the live host, then:")
    print(f"  python python/scripts/tennis_run_from_bundle.py --bundle {out}  # add --live-submit to trade")
    return 0


def _selftest() -> int:
    """Offline checks for the pure logic (no network)."""
    assert _date_from_event("KXATPMATCH-26JUN01TIAARN") == "2026-06-01"
    assert _date_from_event("KXATPMATCH-26MAY31DEJZVE") == "2026-05-31"
    assert _surname("Felix Auger-Aliassime") == "auger-aliassime"
    assert _round_from_title("Will X win the A vs B: Round Of 16 match?", "R32") == "R16"
    assert _round_from_title("Will X win the A vs B: Quarterfinal match?", "R32") == "QF"
    assert _round_from_title("no round here", "R32") == "R32"  # fallback
    m = Match(event_ticker="E", legs={"Frances Tiafoe": "T-TIA", "Matteo Arnaldi": "T-ARN"}, target_date="2026-06-01")
    prov = ManualCsvProvider.__new__(ManualCsvProvider)
    prov.by_name = {"frances tiafoe": 1.5, "tiafoe": 1.5, "matteo arnaldi": 2.6, "arnaldi": 2.6}
    mo = prov.odds_for_match(m)
    assert mo is not None and mo.odds_for("Frances Tiafoe") == 1.5
    print("selftest OK")
    return 0


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", default="KXATPMATCH")
    ap.add_argument("--surface", default="Clay", choices=["Hard", "Clay", "Grass", "Carpet"])
    ap.add_argument("--tourney-level", default="G", help="A/M/G/F (G = Grand Slam).")
    ap.add_argument("--best-of", type=int, default=5, choices=[3, 5])
    ap.add_argument("--round", default="R16")
    ap.add_argument("--odds-provider", default="the-odds-api", choices=["the-odds-api", "manual"])
    ap.add_argument("--api-key", default=os.environ.get("THE_ODDS_API_KEY"))
    ap.add_argument("--sport-key", default="tennis_atp_french_open")
    ap.add_argument("--book", default="pinnacle")
    ap.add_argument("--odds-csv", type=Path, default=None, help="manual provider: player,decimal_odds rows")
    ap.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY)
    ap.add_argument("--bundle-name", default=DEFAULT_BUNDLE, help="model bundle name recorded in the manifest")
    ap.add_argument("--kalshi-host", default=KALSHI_HOST)
    ap.add_argument("--include-started", action="store_true", help="do NOT skip matches past commence_time")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "tennis-live" / "bundle")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    return build_bundle(args)


if __name__ == "__main__":
    raise SystemExit(main())
