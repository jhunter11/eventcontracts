"""Minimal Deribit REST client for live ATM IV.

Pulls the at-the-money implied volatility for the next-expiring BTC
or ETH option. Used to seed the synthetic scenario generator with a
real-world vol level so the ensemble runs against a mix of real
external data and synthetic Kalshi quotes.

This module deliberately does not depend on the rest of the framework
beyond ``rust_decimal``-style ``Decimal`` types. It is safe to import
in scripts and notebooks without setting up a strategy runner.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

DERIBIT_BASE = "https://www.deribit.com/api/v2"


def _ssl_context() -> ssl.SSLContext | None:
    """Return an SSL context that tolerates skewed clocks in research VMs.

    Set ``EVENTCONTRACTS_INSECURE_TLS=1`` to disable certificate
    validation — useful when the host clock is far enough in the
    future that Deribit's currently-valid TLS cert appears
    ``not yet valid``. Never enable this on a host that handles live
    capital.
    """

    if os.environ.get("EVENTCONTRACTS_INSECURE_TLS") == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


@dataclass(frozen=True)
class DeribitSnapshot:
    """One ATM IV reading from Deribit."""

    underlying: str           # "BTC" or "ETH"
    spot: Decimal             # index price
    atm_iv: Decimal           # annualized IV at the closest strike
    expiry_at: datetime       # UTC expiry of the matched instrument
    instrument_name: str      # e.g. BTC-25NOV24-100000-C


def _http_get(path: str, params: dict[str, str], *, timeout: float = 10.0) -> dict:
    url = f"{DERIBIT_BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "eventcontracts/0.1"})
    ctx = _ssl_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        body = resp.read().decode("utf-8")
    payload = json.loads(body)
    if "result" not in payload:
        raise RuntimeError(f"deribit error: {payload}")
    return payload["result"]


def fetch_index_price(underlying: str = "BTC") -> Decimal:
    """Return the current Deribit index price for ``BTC`` or ``ETH``."""

    key = f"{underlying.lower()}_usd"
    data = _http_get("/public/get_index_price", {"index_name": key})
    return Decimal(str(data["index_price"]))


def fetch_atm_snapshot(
    underlying: str = "BTC",
    *,
    max_expiries: int = 6,
) -> DeribitSnapshot:
    """Pick the soonest BTC/ETH option expiry and return its ATM IV.

    The function walks ``max_expiries`` future option expiries on
    Deribit, finds the call closest to the current index price, and
    returns its mark IV. The result is a point-in-time snapshot —
    callers that want a longer feed should poll periodically.
    """

    spot = fetch_index_price(underlying)
    instruments = _http_get(
        "/public/get_instruments",
        {"currency": underlying, "kind": "option", "expired": "false"},
    )
    # Group instruments by expiry; pick the soonest ``max_expiries``.
    by_expiry: dict[int, list[dict]] = {}
    for instr in instruments:
        if instr.get("option_type") != "call":
            continue
        ts_ms = instr.get("expiration_timestamp")
        if ts_ms is None:
            continue
        by_expiry.setdefault(int(ts_ms), []).append(instr)
    if not by_expiry:
        raise RuntimeError(f"no live {underlying} options on Deribit")
    soonest_expiries = sorted(by_expiry.keys())[:max_expiries]

    best: dict | None = None
    best_distance: Decimal | None = None
    for ts in soonest_expiries:
        for instr in by_expiry[ts]:
            strike = Decimal(str(instr["strike"]))
            distance = abs(strike - spot)
            if best_distance is None or distance < best_distance:
                best = instr
                best_distance = distance
        # Stop at the first expiry that has an ATM-ish strike.
        if best is not None and best.get("expiration_timestamp") == ts:
            break
    if best is None:
        raise RuntimeError("could not select a Deribit ATM strike")

    ticker = _http_get("/public/ticker", {"instrument_name": best["instrument_name"]})
    mark_iv = Decimal(str(ticker.get("mark_iv", "0"))) / Decimal("100")  # Deribit returns %
    expiry_at = datetime.fromtimestamp(
        int(best["expiration_timestamp"]) / 1000.0, tz=UTC
    )
    return DeribitSnapshot(
        underlying=underlying.upper(),
        spot=spot,
        atm_iv=mark_iv,
        expiry_at=expiry_at,
        instrument_name=best["instrument_name"],
    )
