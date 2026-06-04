"""THE money experiment: is sharp-vs-soft Closing-Line Value real & tradeable?

We proved the ATP match-winner market is efficient: no model beats the sharp
closing line. So the edge is NOT prediction -- it is Closing-Line Value (CLV):
the SHARP book's fair value vs a SOFTER book's price. Kalshi is a soft,
retail-heavy book, so this edge transfers directly.

This test uses tennis-data.co.uk only (it carries multiple books per match):
  * Pinnacle  (PSW/PSL)   = the sharp book   -> our FAIR VALUE
  * Bet365    (B365W/B365L)= a soft book      -> proxy for the Kalshi price
  * Average   (AvgW/AvgL)  = market average    -> alt soft proxy

Per match we de-vig each book to vig-free probabilities, randomly assign which
competitor is "player 1" (kills the winner-listed-first label leak), then run a
walk-forward, post-fee betting sim: back the side where the sharp fair value
exceeds the soft price by >= edge, transact at the soft price, settle at truth,
charge the real Kalshi taker fee 0.07*P*(1-P). ROI is on capital deployed.

Premise check first: sharp log-loss must be < soft log-loss (sharp beats soft).
If the premise holds and post-fee ROI > 0 -- especially on favourites, where the
fee is smallest and variance lowest -- that is a simple, reliable money-maker.
"""

from __future__ import annotations

import io
import json
import sys
import warnings
from pathlib import Path

import numpy as np

warnings.filterwarnings("ignore")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
ODDS = ROOT / "data" / "tennis" / "tennis_data_odds"
FEE_RATE = 0.07
RNG = np.random.default_rng(7)


def _devig(w_odds: np.ndarray, l_odds: np.ndarray):
    """Proportional de-vig of a two-way book -> fair P(winner), P(loser)."""
    rw, rl = 1.0 / w_odds, 1.0 / l_odds
    s = rw + rl
    return rw / s, rl / s


def _log_loss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _fee(price: np.ndarray) -> np.ndarray:
    return FEE_RATE * price * (1.0 - price)


def load() -> dict:
    import openpyxl

    rows = []
    for f in sorted(ODDS.glob("*.xlsx")):
        if not (f.stem.isdigit() and int(f.stem) >= 2013):
            continue
        wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        hdr = list(next(it))
        idx = {name: i for i, name in enumerate(hdr) if name is not None and name not in {}}
        # first occurrence wins for duplicated headers
        col = {}
        for i, name in enumerate(hdr):
            if name not in col and name is not None:
                col[name] = i
        need = ("PSW", "PSL", "B365W", "B365L", "AvgW", "AvgL", "Date", "WRank", "LRank")
        if not all(k in col for k in ("PSW", "PSL", "B365W", "B365L")):
            wb.close()
            continue
        year = int(f.stem)
        for r in it:
            try:
                psw, psl = r[col["PSW"]], r[col["PSL"]]
                b3w, b3l = r[col["B365W"]], r[col["B365L"]]
                avw = r[col["AvgW"]] if "AvgW" in col else None
                avl = r[col["AvgL"]] if "AvgL" in col else None
            except (IndexError, KeyError):
                continue
            if None in (psw, psl, b3w, b3l):
                continue
            try:
                psw, psl, b3w, b3l = float(psw), float(psl), float(b3w), float(b3l)
            except (TypeError, ValueError):
                continue
            if min(psw, psl, b3w, b3l) <= 1.0:
                continue
            wr = r[col["WRank"]] if "WRank" in col else None
            lr = r[col["LRank"]] if "LRank" in col else None
            rows.append((year, psw, psl, b3w, b3l, avw, avl, wr, lr))
        wb.close()

    year = np.array([x[0] for x in rows])
    psw = np.array([x[1] for x in rows], float)
    psl = np.array([x[2] for x in rows], float)
    b3w = np.array([x[3] for x in rows], float)
    b3l = np.array([x[4] for x in rows], float)

    # Randomly orient each match: is the listed winner "player 1"?
    p1_is_winner = RNG.random(len(rows)) < 0.5
    y = p1_is_winner.astype(int)  # did player 1 win?

    def orient(w_side, l_side):
        # value for player1 / player2 given a (winner_side, loser_side) pair
        p1 = np.where(p1_is_winner, w_side, l_side)
        p2 = np.where(p1_is_winner, l_side, w_side)
        return p1, p2

    ps_p1, ps_p2 = orient(psw, psl)
    b3_p1, b3_p2 = orient(b3w, b3l)

    sharp_p1, _ = _devig(ps_p1, ps_p2)
    soft_p1, _ = _devig(b3_p1, b3_p2)
    return {"year": year, "y": y, "sharp": sharp_p1, "soft": soft_p1}


def main() -> int:
    d = load()
    year, y, sharp, soft = d["year"], d["y"], d["sharp"], d["soft"]
    print(f"matches with Pinnacle+Bet365 = {y.size}")

    print("\n=== PREMISE: does the sharp book beat the soft book? (log-loss, lower=better) ===")
    print(
        json.dumps(
            {
                "pinnacle_sharp_logloss": round(_log_loss(y, sharp), 5),
                "bet365_soft_logloss": round(_log_loss(y, soft), 5),
                "sharp_beats_soft": bool(_log_loss(y, sharp) < _log_loss(y, soft)),
                "mean_abs_price_gap": round(float(np.mean(np.abs(sharp - soft))), 4),
            },
            indent=2,
        )
    )

    # Walk-forward CLV betting: train years are irrelevant (no fit needed), but we
    # report per-season to show consistency, then pooled with a favourites slice.
    print("\n=== CLV BETTING: back side where sharp_fair - soft_price >= edge, pay soft price ===")

    def simulate(mask_extra=None, edges=(50, 100, 150, 200, 300)):
        out = []
        for e in edges:
            t = e / 10000.0
            profit = cost = bets = wins = 0.0
            bets = 0
            for i in range(y.size):
                if mask_extra is not None and not mask_extra[i]:
                    continue
                # back player 1?
                if sharp[i] - soft[i] >= t and soft[i] > 0.02:
                    price, won = float(soft[i]), (y[i] == 1)
                # back player 2? sharp_p2 - soft_p2 = (1-sharp)-(1-soft) = soft-sharp
                elif soft[i] - sharp[i] >= t and (1 - soft[i]) > 0.02:
                    price, won = float(1 - soft[i]), (y[i] == 0)
                else:
                    continue
                profit += ((1.0 - price) if won else -price) - FEE_RATE * price * (1 - price)
                cost += price
                bets += 1
                wins += int(won)
            roi = profit / cost if cost else float("nan")
            out.append((e, bets, wins / bets if bets else float("nan"), roi))
        return out

    print("\n-- ALL matches --")
    for e, n, wr, roi in simulate():
        print(f"edge>={e:>3}bps: roi={roi:+.4f} win_rate={wr:.3f} n={n}")

    # The 'simple, reliable' slice: FAVOURITES (soft price for the backed side high).
    # Equivalent: only bet when the backed side is the favourite (price>0.60).
    print("\n-- FAVOURITES only (backed-side soft price > 0.60; lowest fee + variance) --")

    def simulate_fav(edges=(50, 100, 150, 200, 300), fav=0.60):
        out = []
        for e in edges:
            t = e / 10000.0
            profit = cost = 0.0
            bets = wins = 0
            for i in range(y.size):
                if sharp[i] - soft[i] >= t and soft[i] > fav:
                    price, won = float(soft[i]), (y[i] == 1)
                elif soft[i] - sharp[i] >= t and (1 - soft[i]) > fav:
                    price, won = float(1 - soft[i]), (y[i] == 0)
                else:
                    continue
                profit += ((1.0 - price) if won else -price) - FEE_RATE * price * (1 - price)
                cost += price
                bets += 1
                wins += int(won)
            roi = profit / cost if cost else float("nan")
            out.append((e, bets, wins / bets if bets else float("nan"), roi))
        return out

    for e, n, wr, roi in simulate_fav():
        print(f"edge>={e:>3}bps: roi={roi:+.4f} win_rate={wr:.3f} n={n}")

    # Per-season consistency at a sensible operating point (edge>=100bps, all).
    print("\n=== PER-SEASON @ edge>=100bps (all matches) ===")
    for yr in sorted(set(year.tolist())):
        m = year == yr
        prof = cost = 0.0
        bets = wins = 0
        for i in np.where(m)[0]:
            if sharp[i] - soft[i] >= 0.01 and soft[i] > 0.02:
                price, won = float(soft[i]), (y[i] == 1)
            elif soft[i] - sharp[i] >= 0.01 and (1 - soft[i]) > 0.02:
                price, won = float(1 - soft[i]), (y[i] == 0)
            else:
                continue
            prof += ((1.0 - price) if won else -price) - FEE_RATE * price * (1 - price)
            cost += price
            bets += 1
            wins += int(won)
        roi = prof / cost if cost else float("nan")
        print(f"{yr}: roi={roi:+.4f} n={bets} wr={wins / bets if bets else float('nan'):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
