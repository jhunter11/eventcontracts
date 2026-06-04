"""DECISIVE tradeability test: CLV edge at REAL (vigged) entry prices.

The prior CLV / anchored sweeps entered at DE-VIGGED prices, which is unattainable
and overstates ROI by ~the book margin. This corrects that: we BUY at the soft
book's RAW implied price (1/odds, vig included) -- the honest Kalshi-equivalent
entry -- using the SHARP (Pinnacle, de-vigged) probability as fair value, settle
at the true outcome, and charge the real Kalshi taker fee 0.07*P*(1-P).

This is exactly the live strategy: a sharp consensus is the model's fair value,
Kalshi is the soft venue, we lift its price when it deviates from fair by >= edge.
If this is positive on favourites at real prices, the winner-model-anchored-to-
sharp path is genuinely tradeable; if not, it is not.

Soft proxies tested: Bet365 (B365) and the market max (best available price,
the most favourable real entry a taker could get).
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


def _devig(w, l):
    rw, rl = 1.0 / w, 1.0 / l
    s = rw + rl
    return rw / s, rl / s


def _fee(p):
    return FEE_RATE * p * (1.0 - p)


def load():
    import openpyxl

    rows = []
    for f in sorted(ODDS.glob("*.xlsx")):
        if not (f.stem.isdigit() and int(f.stem) >= 2013):
            continue
        year = int(f.stem)
        wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        hdr = list(next(it))
        col = {}
        for i, n in enumerate(hdr):
            if n is not None and n not in col:
                col[n] = i
        if not all(k in col for k in ("PSW", "PSL", "B365W", "B365L")):
            wb.close()
            continue
        has_max = "MaxW" in col and "MaxL" in col
        for r in it:
            def g(name):
                i = col.get(name)
                return r[i] if (i is not None and i < len(r)) else None

            try:
                psw, psl = float(g("PSW")), float(g("PSL"))
                b3w, b3l = float(g("B365W")), float(g("B365L"))
            except (TypeError, ValueError):
                continue
            if min(psw, psl, b3w, b3l) <= 1.0:
                continue
            mxw = mxl = None
            if has_max:
                try:
                    mxw, mxl = float(g("MaxW")), float(g("MaxL"))
                    if min(mxw, mxl) <= 1.0:
                        mxw = mxl = None
                except (TypeError, ValueError):
                    mxw = mxl = None
            rows.append((year, psw, psl, b3w, b3l, mxw, mxl))
        wb.close()

    year = np.array([x[0] for x in rows])
    psw = np.array([x[1] for x in rows], float)
    psl = np.array([x[2] for x in rows], float)
    b3w = np.array([x[3] for x in rows], float)
    b3l = np.array([x[4] for x in rows], float)
    mxw = np.array([x[5] if x[5] else np.nan for x in rows], float)
    mxl = np.array([x[6] if x[6] else np.nan for x in rows], float)

    p1_is_w = RNG.random(len(rows)) < 0.5
    y = p1_is_w.astype(int)

    def orient(w, l):
        return np.where(p1_is_w, w, l), np.where(p1_is_w, l, w)

    ps1, ps2 = orient(psw, psl)
    b31, b32 = orient(b3w, b3l)
    mx1, mx2 = orient(mxw, mxl)
    sharp1, _ = _devig(ps1, ps2)  # fair value for player 1
    # RAW (vigged) soft entry prices: what a taker actually pays.
    b3_raw1, b3_raw2 = 1.0 / b31, 1.0 / b32
    mx_raw1, mx_raw2 = 1.0 / mx1, 1.0 / mx2
    return {
        "year": year, "y": y, "sharp1": sharp1,
        "b3_raw1": b3_raw1, "b3_raw2": b3_raw2,
        "mx_raw1": mx_raw1, "mx_raw2": mx_raw2,
    }


def simulate(y, sharp1, raw1, raw2, *, edge, fav_only=False, fav=0.60):
    """Buy player1 at raw1 if sharp1 - raw1 >= edge; buy player2 at raw2 if
    (1-sharp1) - raw2 >= edge. raw* are vigged entry prices (1/odds)."""
    t = edge / 10000.0
    profit = cost = 0.0
    bets = wins = 0
    for i in range(y.size):
        # player1 side
        if not np.isnan(raw1[i]) and (sharp1[i] - raw1[i]) >= t:
            price, won, side_fav = float(raw1[i]), (y[i] == 1), (raw1[i] >= fav)
        elif not np.isnan(raw2[i]) and ((1 - sharp1[i]) - raw2[i]) >= t:
            price, won, side_fav = float(raw2[i]), (y[i] == 0), (raw2[i] >= fav)
        else:
            continue
        if fav_only and not side_fav:
            continue
        if price <= 0.02 or price >= 0.98:
            continue
        profit += ((1.0 - price) if won else -price) - _fee(price)
        cost += price
        bets += 1
        wins += int(won)
    roi = profit / cost if cost else float("nan")
    return bets, (wins / bets if bets else float("nan")), roi


def main() -> int:
    d = load()
    y = d["y"]
    print(f"matches (Pinnacle+Bet365) = {y.size}")

    for label, raw1, raw2 in (
        ("Bet365 raw price (vig included)", d["b3_raw1"], d["b3_raw2"]),
        ("Market-MAX raw price (best available)", d["mx_raw1"], d["mx_raw2"]),
    ):
        print(f"\n=== ENTRY @ {label}; fair = Pinnacle de-vigged; real Kalshi fee ===")
        print("-- ALL matches --")
        for e in (0, 100, 200, 300, 500):
            n, wr, roi = simulate(y, d["sharp1"], raw1, raw2, edge=e)
            print(f"edge>={e:>3}bps: roi={roi:+.4f} wr={wr:.3f} n={n}")
        print("-- FAVOURITES (entry price > 0.60) --")
        for e in (0, 100, 200, 300, 500):
            n, wr, roi = simulate(y, d["sharp1"], raw1, raw2, edge=e, fav_only=True)
            print(f"edge>={e:>3}bps: roi={roi:+.4f} wr={wr:.3f} n={n}")

    # Per-season robustness at the most realistic operating point:
    # Market-MAX entry (best price a taker can get), favourites, edge>=200bps.
    print("\n=== PER-SEASON: MAX-price entry, favourites, edge>=200bps ===")
    pos = 0
    tot = 0
    for yr in sorted(set(d["year"].tolist())):
        m = d["year"] == yr
        n, wr, roi = simulate(
            y[m], d["sharp1"][m], d["mx_raw1"][m], d["mx_raw2"][m], edge=200, fav_only=True
        )
        if n >= 50:
            tot += 1
            pos += int(roi > 0)
            print(f"{yr}: roi={roi:+.4f} wr={wr:.3f} n={n}")
    print(f"\nseasons positive: {pos}/{tot}")
    print(
        "\nVERDICT: a real edge needs consistent positive ROI at MAX-price entry "
        "(the best a taker gets). Bet365-price positivity alone is not enough, "
        "since Kalshi's taker price may be worse than Bet365's best line."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
