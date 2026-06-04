"""In-play tennis win-probability engine (Markov) + backtest on slam point-by-point.

The "adaptive layer" for mid-match data: tennis scoring is a Markov chain, so
given each player's per-point serve-win probability and the CURRENT score
(sets/games/points/server), there's an exact recursion for P(match win). The
pre-match model is only the PRIOR that sets the serve probabilities; the live
score does the adapting.

This script:
  * implements the analytic engine (game w/ deuce, tiebreak serve-rotation, set
    w/ tiebreak-at-6-6, best-of-3/5 match), startable from any in-play state;
  * `--validate`: cross-checks the analytic engine against Monte-Carlo
    simulation (catches recursion bugs before any backtest is trusted);
  * backtests on Sackmann slam point-by-point: walks each match, computes the
    live win prob at every point, and reports (a) point-level CALIBRATION vs the
    actual match winner — does the engine produce honest probabilities — and
    (b) SWING magnitude — how far fair value jumps on a single point (sizes the
    in-play trading opportunity the market must track).

Two serve-prior variants:
  * score-only  : fixed 0.64 serve-win for both (leak-free; pure score signal)
  * match-serve : each player's serve-win from THIS match's points (mildly leaky;
    validates the engine WITH player strength + sizes realistic swings)

Honest scope: this validates the ENGINE and sizes SWINGS. It does NOT prove a
market edge — that needs live in-play Kalshi quotes (which we don't have) and a
latency assessment.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import math
import random
import statistics
import sys
from pathlib import Path

import polars as pl

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PBP = ROOT / "data" / "tennis" / "tennis_slam_pointbypoint"
_SCORE_MAP = {"0": 0, "15": 1, "30": 2, "40": 3, "AD": 4, "": 0}


class InPlay:
    """Analytic P1-perspective win-probability engine; caches per (sp1, sp2)."""

    def __init__(self, sp1: float, sp2: float, best_of: int = 5) -> None:
        self.sp1 = sp1
        self.sp2 = sp2
        self.sets_to_win = best_of // 2 + 1
        self._g: dict = {}
        self._tb: dict = {}
        self._s: dict = {}
        self._m: dict = {}

    # server wins a game from point state (a=server pts, b=returner pts)
    def gwp(self, p: float, a: int, b: int) -> float:
        if a >= 4 and a - b >= 2:
            return 1.0
        if b >= 4 and b - a >= 2:
            return 0.0
        key = (p, a, b)
        c = self._g.get(key)
        if c is not None:
            return c
        if a >= 3 and b >= 3:
            q = 1 - p
            d = p * p / (p * p + q * q)
            r = d if a == b else (p + q * d if a == b + 1 else p * d)
        else:
            r = p * self.gwp(p, a + 1, b) + (1 - p) * self.gwp(p, a, b + 1)
        self._g[key] = r
        return r

    def game_p1(self, server: int, a: int, b: int) -> float:
        return self.gwp(self.sp1, a, b) if server == 1 else 1.0 - self.gwp(self.sp2, a, b)

    # P1 wins tiebreak; a/b tb points, first = who served point 1 of the tiebreak
    def tb_p1(self, a: int, b: int, first: int) -> float:
        if a >= 7 and a - b >= 2:
            return 1.0
        if b >= 7 and b - a >= 2:
            return 0.0
        if a >= 6 and b >= 6 and a == b:
            # tiebreak deuce: win-by-2 from level; over a 2-point block each player
            # serves once, so P1 wins = x/(x+y). Collapses the infinite alternation.
            x = self.sp1 * (1 - self.sp2)
            y = (1 - self.sp1) * self.sp2
            return x / (x + y) if (x + y) > 0 else 0.5
        key = (a, b, first)
        c = self._tb.get(key)
        if c is not None:
            return c
        i = a + b
        server = first if ((i + 1) // 2) % 2 == 0 else 3 - first
        pw1 = self.sp1 if server == 1 else 1.0 - self.sp2
        r = pw1 * self.tb_p1(a + 1, b, first) + (1 - pw1) * self.tb_p1(a, b + 1, first)
        self._tb[key] = r
        return r

    # P1 wins the set from (g1,g2 games, server of current game, point state a,b)
    def set_p1(self, g1: int, g2: int, server: int, a: int, b: int) -> float:
        if g1 >= 6 and g1 - g2 >= 2:
            return 1.0
        if g2 >= 6 and g2 - g1 >= 2:
            return 0.0
        if g1 == 6 and g2 == 6:
            return self.tb_p1(0, 0, server)
        key = (g1, g2, server, a, b)
        c = self._s.get(key)
        if c is not None:
            return c
        gw = self.game_p1(server, a, b)
        r = gw * self.set_p1(g1 + 1, g2, 3 - server, 0, 0) + (1 - gw) * self.set_p1(g1, g2 + 1, 3 - server, 0, 0)
        self._s[key] = r
        return r

    def _match_from_setstart(self, s1: int, s2: int, first: int) -> float:
        if s1 == self.sets_to_win:
            return 1.0
        if s2 == self.sets_to_win:
            return 0.0
        key = (s1, s2, first)
        c = self._m.get(key)
        if c is not None:
            return c
        ws = self.set_p1(0, 0, first, 0, 0)
        r = ws * self._match_from_setstart(s1 + 1, s2, 3 - first) + (1 - ws) * self._match_from_setstart(
            s1, s2 + 1, 3 - first
        )
        self._m[key] = r
        return r

    def match_p1(self, s1: int, s2: int, g1: int, g2: int, server: int, a: int, b: int) -> float:
        """P1 win prob from a full in-play state."""
        if s1 == self.sets_to_win:
            return 1.0
        if s2 == self.sets_to_win:
            return 0.0
        ws = self.set_p1(g1, g2, server, a, b)
        nxt = 3 - server  # approx: first server of next set alternates
        return ws * self._match_from_setstart(s1 + 1, s2, nxt) + (1 - ws) * self._match_from_setstart(
            s1, s2 + 1, nxt
        )


def _simulate(sp1: float, sp2: float, best_of: int, rng: random.Random) -> int:
    """Monte-Carlo a full match from 0-0; return 1 if P1 wins (validation only)."""
    stw = best_of // 2 + 1
    s1 = s2 = 0
    server = 1
    while s1 < stw and s2 < stw:
        g1 = g2 = 0
        while not ((g1 >= 6 or g2 >= 6) and abs(g1 - g2) >= 2) and not (g1 == 6 and g2 == 6):
            # play one game
            a = b = 0
            sp = sp1 if server == 1 else sp2
            while True:
                if rng.random() < sp:
                    a += 1
                else:
                    b += 1
                if a >= 4 and a - b >= 2:
                    win = server
                    break
                if b >= 4 and b - a >= 2:
                    win = 3 - server
                    break
            if win == 1:
                g1 += 1
            else:
                g2 += 1
            server = 3 - server
        if g1 == 6 and g2 == 6:
            # tiebreak
            a = b = 0
            first = server
            while not (max(a, b) >= 7 and abs(a - b) >= 2):
                i = a + b
                srv = first if ((i + 1) // 2) % 2 == 0 else 3 - first
                pw1 = sp1 if srv == 1 else (1 - sp2)  # P(P1 wins point), returner-correct
                if rng.random() < pw1:
                    a += 1
                else:
                    b += 1
            if a > b:
                g1 += 1
            else:
                g2 += 1
            server = 3 - first
        if g1 > g2:
            s1 += 1
        else:
            s2 += 1
    return 1 if s1 == stw else 0


def validate() -> int:
    rng = random.Random(7)
    print("=== ENGINE VALIDATION: analytic vs Monte-Carlo (match start 0-0) ===")
    ok = True
    for sp1, sp2, bo in [(0.64, 0.64, 3), (0.70, 0.60, 3), (0.64, 0.64, 5), (0.68, 0.62, 5), (0.60, 0.66, 5)]:
        eng = InPlay(sp1, sp2, bo)
        analytic = eng.match_p1(0, 0, 0, 0, 1, 0, 0)
        n = 40000
        mc = sum(_simulate(sp1, sp2, bo, rng) for _ in range(n)) / n
        se = (mc * (1 - mc) / n) ** 0.5
        diff = abs(analytic - mc)
        flag = "OK" if diff < 4 * se + 0.005 else "** MISMATCH **"
        if "MISMATCH" in flag:
            ok = False
        print(f"  sp1={sp1} sp2={sp2} Bo{bo}: analytic={analytic:.4f} mc={mc:.4f} (±{se:.4f}) diff={diff:.4f} {flag}")
    print("VALIDATION:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ---------------- backtest ---------------- #
def _winner_of_set(g1: int, g2: int) -> int | None:
    if (g1 >= 6 or g2 >= 6) and abs(g1 - g2) >= 2:
        return 1 if g1 > g2 else 2
    if g1 == 7 or g2 == 7:
        return 1 if g1 > g2 else 2
    return None


def _match_outcome(rows: list[dict]) -> tuple[int, int] | None:
    """Derive (match_winner, best_of) from the points alone (the matches file's
    winner/event_name are blank in this dataset). Men take 3 sets, women 2 — the
    winner's set count also gives best_of. Returns None for incomplete/retired."""
    set_winner: dict[int, int] = {}
    max_set = 1
    for r in rows:
        try:
            sn = int(r["SetNo"])
            sw = int(r["SetWinner"])
        except (TypeError, ValueError, KeyError):
            continue
        max_set = max(max_set, sn)
        if sw in (1, 2):
            set_winner[sn] = sw
    s1 = sum(1 for w in set_winner.values() if w == 1)
    s2 = sum(1 for w in set_winner.values() if w == 2)
    best_of = 5 if (max(s1, s2) >= 3 or max_set >= 4) else 3
    stw = best_of // 2 + 1
    if max(s1, s2) < stw:  # retired / incomplete
        return None
    return (1 if s1 > s2 else 2), best_of


def _load_match_players(points_path: Path) -> dict[str, tuple[str, str]]:
    mp = Path(str(points_path).replace("-points.csv", "-matches.csv"))
    if not mp.exists():
        return {}
    df = pl.read_csv(mp, infer_schema_length=0)
    out: dict[str, tuple[str, str]] = {}
    for r in df.iter_rows(named=True):
        p1, p2 = r.get("player1"), r.get("player2")
        if r.get("match_id") and p1 and p2:
            out[r["match_id"]] = (p1, p2)
    return out


def _accumulate_priors(files: list[Path]) -> dict[str, tuple[float, float]]:
    """Leak-free, opponent-adjusted pre-match serve prior per match. Processes
    files chronologically, accumulating each player's career serve- and
    return-points-won rate from PRIOR matches only; the prior for player X serving
    to Y is clamp(serve_X - (return_Y - avg_return)) (the standard additive
    serve/return combination). Falls back to tour average until enough history."""
    from collections import defaultdict

    sw: dict = defaultdict(float)
    sn: dict = defaultdict(float)
    rw: dict = defaultdict(float)
    rn: dict = defaultdict(float)
    avg, avg_q, mins = 0.64, 0.36, 150.0
    priors: dict[str, tuple[float, float]] = {}

    def prior(srv: str, ret: str) -> float:
        if sn[srv] >= mins and rn[ret] >= mins:
            return _clamp_sp(sw[srv] / sn[srv] - (rw[ret] / rn[ret] - avg_q))
        return avg

    for pf in files:
        players = _load_match_players(pf)
        try:
            pts = pl.read_csv(pf, infer_schema_length=0)
        except Exception:  # noqa: BLE001
            continue
        for mid, group in _group_by_match(pts):
            names = players.get(mid)
            if not names:
                continue
            a, b = names
            priors[mid] = (prior(a, b), prior(b, a))  # BEFORE updating with this match
            for r in group:
                sv, pw = r.get("PointServer"), r.get("PointWinner")
                if sv == "1" and pw in ("1", "2"):
                    sn[a] += 1
                    sw[a] += 1 if pw == "1" else 0
                    rn[b] += 1
                    rw[b] += 1 if pw == "2" else 0
                elif sv == "2" and pw in ("1", "2"):
                    sn[b] += 1
                    sw[b] += 1 if pw == "2" else 0
                    rn[a] += 1
                    rw[a] += 1 if pw == "1" else 0
    return priors


def _points_files(pbp_dir: Path, years: list[str]) -> list[Path]:
    return sorted(f for f in pbp_dir.glob("*-points.csv") if any(f.name.startswith(y) for y in years))


def _trajectory_for(
    group: list[dict], variant: str, kappa: float, priors: dict[str, tuple[float, float]]
) -> tuple[list[float], int] | None:
    """Build one match's per-point P(P1 win) trajectory, dispatching on variant.
    Returns (trajectory, p1_won) or None for unusable matches. Shared by backtest()
    and analyze_persistence() so the variant/prior logic can't drift between them."""
    outcome = _match_outcome(group)
    if outcome is None:
        return None
    actual, best_of = outcome
    p1_won = 1 if actual == 1 else 0
    if variant == "bayes":
        traj = _match_trajectory(group, best_of, kappa=kappa)
    elif variant == "best":
        mid = group[0]["match_id"] if group else None
        traj = _match_trajectory(group, best_of, kappa=kappa, prior_mu=priors.get(mid, (0.64, 0.64)))
    else:
        sp1, sp2 = _serve_priors(group, variant)
        if sp1 is None:
            return None
        traj = _match_trajectory(group, best_of, fixed_sp=(sp1, sp2))
    return (traj, p1_won) if traj else None


def backtest(pbp_dir: Path, years: list[str], variant: str, max_matches: int | None, kappa: float) -> int:
    import time

    files = _points_files(pbp_dir, years)
    if not files:
        print(f"no -points.csv for years {years} under {pbp_dir}")
        return 2
    priors = _accumulate_priors(files) if variant == "best" else {}
    preds: list[float] = []
    obs: list[int] = []
    swings: list[float] = []
    n_matches = 0
    t_eval = 0.0
    for pf in files:
        try:
            pts = pl.read_csv(pf, infer_schema_length=0)
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {pf.name}: {exc}")
            continue
        for _mid, group in _group_by_match(pts):
            t0 = time.perf_counter()
            res = _trajectory_for(group, variant, kappa, priors)
            t_eval += time.perf_counter() - t0
            if res is None:
                continue
            traj, p1_won = res
            preds.extend(traj)
            obs.extend([p1_won] * len(traj))
            swings.extend(abs(traj[i] - traj[i - 1]) for i in range(1, len(traj)))
            n_matches += 1
            if max_matches and n_matches >= max_matches:
                break
        if max_matches and n_matches >= max_matches:
            break

    if not preds:
        print("no points scored")
        return 1
    tag = f"{variant} kappa={kappa:g}" if variant == "bayes" else variant
    print(f"\n=== IN-PLAY BACKTEST ({tag}) — {n_matches} matches, {len(preds):,} point-states ===")
    _report_calibration(preds, obs)
    _report_swings(swings)
    us = t_eval / max(1, len(preds)) * 1e6
    print(f"\n  inference: {us:.1f} µs/state ({len(preds) / max(1e-9, t_eval):,.0f} states/s, warm cache)")
    return 0


def analyze_persistence(
    pbp_dir: Path, years: list[str], variant: str, max_matches: int | None, kappa: float, threshold: float = 0.05
) -> int:
    """Beyond swing MAGNITUDE — does a big single-point fair-value jump PERSIST or
    REVERT over the next few points? This is the OFFLINE half of the latency
    question: if a +10c jump on a break of serve is gone within a point (an
    immediate re-break), the signal is intrinsically fragile and a slow trader is
    hurt before market microstructure even enters; if it holds for several points,
    the timing is forgiving and chasing a faster feed is worthwhile.

    For each qualifying jump (|Δ| ≥ threshold, from a CONTESTABLE state 0.15–0.85
    so we exclude trivially-persistent near-decided points), we track the RETAINED
    FRACTION = (FV h points later − FV before jump) / Δ. So 1.0 = fully persistent,
    0 = fully reverted, >1 = the move continued (incl. running to an absorbing win)."""
    files = _points_files(pbp_dir, years)
    if not files:
        print(f"no -points.csv for years {years} under {pbp_dir}")
        return 2
    priors = _accumulate_priors(files) if variant == "best" else {}
    horizons = [1, 2, 4, 8]
    retained: dict[int, list[float]] = {h: [] for h in horizons}
    half_lives: list[int] = []
    n_durable = 0  # jumps that never decayed to ≤50% before the match ended
    n_jumps = 0
    all_abs: list[float] = []
    n_matches = 0
    for pf in files:
        try:
            pts = pl.read_csv(pf, infer_schema_length=0)
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {pf.name}: {exc}")
            continue
        for _mid, group in _group_by_match(pts):
            res = _trajectory_for(group, variant, kappa, priors)
            if res is None:
                continue
            traj, _ = res
            all_abs.extend(abs(traj[i] - traj[i - 1]) for i in range(1, len(traj)))
            for i in range(1, len(traj)):
                d = traj[i] - traj[i - 1]
                base = traj[i - 1]
                if abs(d) < threshold or not (0.15 <= base <= 0.85):
                    continue
                n_jumps += 1
                for h in horizons:
                    if i + h < len(traj):
                        retained[h].append((traj[i + h] - base) / d)
                decayed = False
                for h in range(1, len(traj) - i):
                    if (traj[i + h] - base) / d <= 0.5:
                        half_lives.append(h)
                        decayed = True
                        break
                if not decayed:
                    n_durable += 1
            n_matches += 1
            if max_matches and n_matches >= max_matches:
                break
        if max_matches and n_matches >= max_matches:
            break

    if n_jumps == 0:
        print("no qualifying jumps")
        return 1
    print(f"\n=== FAIR-VALUE SWING PERSISTENCE ({variant}) — {n_matches} matches, "
          f"{n_jumps:,} jumps ≥{threshold:.0%} from contestable (0.15–0.85) states ===")
    print("  retained fraction of the jump, h points later (1.0=fully persists, 0=fully reverts):")
    for h in horizons:
        vals = retained[h]
        if not vals:
            continue
        med = statistics.median(vals)
        kept = sum(1 for v in vals if v >= 0.5) / len(vals)
        grew = sum(1 for v in vals if v >= 1.0) / len(vals)
        rev = sum(1 for v in vals if v <= 0.0) / len(vals)
        print(f"    +{h:>2}pt: median={med:+.2f}  ≥50% kept={kept:.0%}  grew(≥100%)={grew:.0%}  "
              f"reverted(≤0)={rev:.0%}  n={len(vals):,}")
    if half_lives:
        hl = sorted(half_lives)
        p90 = hl[int(0.9 * len(hl))]
        print(f"\n  decay: {n_durable / n_jumps:.0%} of jumps never lose half their value before match end; "
              f"of those that decay, median half-life={statistics.median(hl):.0f} pts (p90={p90} pts)")
    s = sorted(all_abs, reverse=True)
    total = sum(s)
    if total:
        top1 = sum(s[: max(1, len(s) // 100)]) / total
        top5 = sum(s[: max(1, len(s) // 20)]) / total
        print(f"  concentration: top 1% of points carry {top1:.0%} of all fair-value movement, "
              f"top 5% carry {top5:.0%}")
    return 0


def analyze_thresholds(
    pbp_dir: Path, years: list[str], variant: str, max_matches: int | None, kappa: float,
    thresholds: tuple[float, ...] = (0.70, 0.75, 0.80, 0.85, 0.90, 0.95),
) -> int:
    """The 'is it worth buying the in-play favourite?' question, answered empirically.

    For each match, find the FIRST point where the leader's fair value crosses a
    threshold T, and record whether that leader actually went on to win. This gives
    the real COMEBACK rate at each confidence level and tests whether trusting a
    high in-play probability is a tradeable edge or the favourite-longshot wash.

    Trade framing: a contract bought at price p pays 1 if it wins, so the return on
    a win is (1-p)/p — buy at 0.80 → +25% if it holds. But you also lose p when it
    busts, so EV/contract = realized_win_rate − p, and the gross 25% only matters if
    the win rate exceeds the price. We use the engine's fair value at the crossing as
    the entry price (the closest offline proxy for a market quote — no historical
    in-play book exists), net of Kalshi's 0.07·p·(1−p) taker fee. realized_win_rate
    is GROUND TRUTH, so win_rate vs price also reveals the engine's tail calibration."""
    files = _points_files(pbp_dir, years)
    if not files:
        print(f"no -points.csv for years {years} under {pbp_dir}")
        return 2
    priors = _accumulate_priors(files) if variant == "best" else {}
    prices: dict[float, list[float]] = {t: [] for t in thresholds}
    wins: dict[float, list[int]] = {t: [] for t in thresholds}
    pre_match: dict[float, int] = {t: 0 for t in thresholds}
    n_matches = 0
    for pf in files:
        try:
            pts = pl.read_csv(pf, infer_schema_length=0)
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {pf.name}: {exc}")
            continue
        for _mid, group in _group_by_match(pts):
            res = _trajectory_for(group, variant, kappa, priors)
            if res is None:
                continue
            traj, p1_won = res
            n_matches += 1
            for t in thresholds:
                idx = next((i for i, fv in enumerate(traj) if fv >= t or fv <= 1.0 - t), None)
                if idx is None:
                    continue
                fv = traj[idx]
                leader_p1 = fv >= 0.5
                prices[t].append(fv if leader_p1 else 1.0 - fv)
                wins[t].append(1 if (leader_p1 == (p1_won == 1)) else 0)
                if idx == 0:
                    pre_match[t] += 1
            if max_matches and n_matches >= max_matches:
                break
        if max_matches and n_matches >= max_matches:
            break

    if not n_matches:
        print("no matches")
        return 1
    print(f"\n=== IN-PLAY THRESHOLD / COMEBACK ANALYSIS ({variant}) — {n_matches} matches ===")
    print("  'reached' = matches where the leader's fair value first hit T (incl. pre-match favourites).")
    print("  trade = buy the leader at the fair value on first crossing, hold to settlement; fee=0.07·p·(1−p).")
    for t in thresholds:
        n = len(wins[t])
        if not n:
            continue
        wr = sum(wins[t]) / n
        mp = sum(prices[t]) / n
        ret_win = (1.0 - mp) / mp
        ev = wr - mp
        fee = 0.07 * mp * (1.0 - mp)
        roi, roi_net = ev / mp, (ev - fee) / mp
        print(f"  ≥{t:.0%}: reached {n:4d}/{n_matches} ({n / n_matches:5.1%}) [{pre_match[t]:4d} pre-match]; "
              f"avg entry {mp:.3f}; leader wins {wr:6.1%}  comeback {1 - wr:5.1%}; "
              f"ret-if-win +{ret_win:4.0%}; EV/contract {ev:+.3f}; ROI {roi:+.1%} net {roi_net:+.1%}")
    print("  (win_rate ≈ entry → market-efficient wash; the gross 'ret-if-win' is offset by the comeback rate.)")
    return 0


def _group_by_match(pts: pl.DataFrame):
    cur = None
    buf: list[dict] = []
    for r in pts.iter_rows(named=True):
        mid = r["match_id"]
        if mid != cur:
            if buf:
                yield cur, buf
            cur, buf = mid, []
        buf.append(r)
    if buf:
        yield cur, buf


def _serve_priors(rows: list[dict], variant: str) -> tuple[float | None, float | None]:
    if variant == "score-only":
        return 0.64, 0.64
    s1w = s1 = s2w = s2 = 0
    for r in rows:
        sv = r.get("PointServer")
        pw = r.get("PointWinner")
        if sv == "1" and pw in ("1", "2"):
            s1 += 1
            s1w += 1 if pw == "1" else 0
        elif sv == "2" and pw in ("1", "2"):
            s2 += 1
            s2w += 1 if pw == "2" else 0
    if s1 < 20 or s2 < 20:
        return None, None
    return max(0.5, min(0.85, s1w / s1)), max(0.5, min(0.85, s2w / s2))


_ENGINE_CACHE: dict[tuple, InPlay] = {}


def _engine_for(sp1: float, sp2: float, best_of: int) -> InPlay:
    """Cache engines by rounded serve probs so the Bayesian path (sp nudges every
    point) reuses recursion caches — keeps per-state inference at microseconds."""
    key = (round(sp1, 3), round(sp2, 3), best_of)
    eng = _ENGINE_CACHE.get(key)
    if eng is None:
        eng = InPlay(key[0], key[1], best_of)
        _ENGINE_CACHE[key] = eng
    return eng


def _clamp_sp(x: float) -> float:
    return max(0.5, min(0.85, x))


def _match_trajectory(
    rows: list[dict],
    best_of: int,
    *,
    fixed_sp: tuple[float, float] | None = None,
    kappa: float | None = None,
    prior_mu: float | tuple[float, float] = 0.64,
) -> list[float]:
    """Walk a match; at each point return P(P1 wins) from the live state.

    fixed_sp: constant serve probs (score-only / match-serve variants).
    kappa:    Bayesian Beta-Binomial in-match update — sp = (prior_mu*kappa +
              serve_pts_won_so_far) / (kappa + serve_pts_played_so_far), computed
              from points STRICTLY BEFORE the current one (leak-free + adaptive)."""
    stw = best_of // 2 + 1
    out: list[float] = []
    sets1 = sets2 = 0
    cur_set = None
    last_g1 = last_g2 = 0
    w1 = n1 = w2 = n2 = 0  # serve points won/played per player, so far
    for r in rows:
        try:
            set_no = int(r["SetNo"])
            g1 = int(r["P1GamesWon"])
            g2 = int(r["P2GamesWon"])
            server = int(r["PointServer"])
            pw = r["PointWinner"]
        except (TypeError, ValueError, KeyError):
            continue
        if server not in (1, 2) or pw not in ("1", "2"):
            continue
        if cur_set is None:
            cur_set = set_no
        if set_no != cur_set:
            wset = _winner_of_set(last_g1, last_g2)
            if wset == 1:
                sets1 += 1
            elif wset == 2:
                sets2 += 1
            cur_set = set_no
        last_g1, last_g2 = g1, g2
        if sets1 >= stw or sets2 >= stw:
            break
        if fixed_sp is not None:
            sp1, sp2 = fixed_sp
        else:  # Bayesian posterior mean from points so far, per-player prior
            pm1, pm2 = prior_mu if isinstance(prior_mu, tuple) else (prior_mu, prior_mu)
            sp1 = _clamp_sp((pm1 * kappa + w1) / (kappa + n1)) if n1 else pm1
            sp2 = _clamp_sp((pm2 * kappa + w2) / (kappa + n2)) if n2 else pm2
        a = _SCORE_MAP.get(r.get("P1Score", "0"), 0)
        b = _SCORE_MAP.get(r.get("P2Score", "0"), 0)
        sa, sb = (a, b) if server == 1 else (b, a)
        with contextlib.suppress(RecursionError):
            out.append(_engine_for(sp1, sp2, best_of).match_p1(sets1, sets2, g1, g2, server, sa, sb))
        # update serve counts AFTER predicting (strictly-prior → leak-free)
        if server == 1:
            n1 += 1
            w1 += 1 if pw == "1" else 0
        else:
            n2 += 1
            w2 += 1 if pw == "2" else 0
    return out


def _report_calibration(pred: list[float], obs: list[int]) -> None:
    brier = statistics.fmean((p - o) ** 2 for p, o in zip(pred, obs, strict=True))
    eps = 1e-6
    ll = statistics.fmean(
        -(o * math.log(max(eps, p)) + (1 - o) * math.log(max(eps, 1 - p))) for p, o in zip(pred, obs, strict=True)
    )
    print(f"  point-level calibration: Brier={brier:.4f}  logloss={ll:.4f}")
    print("  reliability (pred bucket -> realized p1-win rate):")
    for i in range(10):
        lo, hi = i / 10, (i + 1) / 10
        sel = [(p, o) for p, o in zip(pred, obs, strict=True) if lo <= p < hi or (i == 9 and p == 1.0)]
        if not sel:
            continue
        mp = statistics.fmean(p for p, _ in sel)
        mo = statistics.fmean(o for _, o in sel)
        print(f"    [{lo:.1f},{hi:.1f}) n={len(sel):6d} pred={mp:.3f} obs={mo:.3f}")


def _report_swings(swings: list[float]) -> None:
    if not swings:
        return
    s = sorted(swings)
    mean = statistics.fmean(s)
    p95 = s[int(0.95 * len(s))]
    p99 = s[int(0.99 * len(s))]
    big5 = sum(1 for x in s if x >= 0.05) / len(s)
    big10 = sum(1 for x in s if x >= 0.10) / len(s)
    print("\n  per-point fair-value SWINGS (the in-play opportunity the market must track):")
    print(f"    mean |Δ|={mean:.4f}  p95={p95:.4f}  p99={p99:.4f}  "
          f"share≥5c={big5:.2%}  share≥10c={big10:.2%}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--validate", action="store_true", help="cross-check analytic engine vs Monte-Carlo, then exit")
    ap.add_argument("--pbp-dir", type=Path, default=DEFAULT_PBP)
    ap.add_argument("--years", default="2023,2024")
    ap.add_argument("--variant", choices=("score-only", "match-serve", "bayes", "best"), default="best")
    ap.add_argument("--analyze", choices=("calibration", "persistence", "thresholds"), default="calibration",
                    help="calibration: Brier/swings (default); persistence: post-jump decay; "
                         "thresholds: in-play comeback rates + trade EV by confidence level")
    ap.add_argument("--kappa", type=float, default=60.0, help="Bayesian prior strength (pseudo serve-points)")
    ap.add_argument("--max-matches", type=int, default=None)
    args = ap.parse_args()
    if args.validate:
        return validate()
    years = [y.strip() for y in args.years.split(",") if y.strip()]
    if args.analyze == "persistence":
        return analyze_persistence(args.pbp_dir, years, args.variant, args.max_matches, args.kappa)
    if args.analyze == "thresholds":
        return analyze_thresholds(args.pbp_dir, years, args.variant, args.max_matches, args.kappa)
    return backtest(args.pbp_dir, years, args.variant, args.max_matches, args.kappa)


if __name__ == "__main__":
    raise SystemExit(main())
