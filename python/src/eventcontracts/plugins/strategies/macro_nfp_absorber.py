"""Non-farm payrolls shock-absorber market-maker.

Hypothesis (per docs/strategy-specs.md #3): market makers pull liquidity
pre-NFP, creating artificially wide spreads. Earning that spread outweighs
directional gap risk when sized conservatively.

Trigger: `TimerEvent` at known release boundaries. At `T-5min` the strategy
widens its quotes to a configured volatility cone; at `T+1min` it tightens
again. The strategy itself does not place fresh orders here — it replaces
already-resting maker quotes via `ReplaceOrder`. The set of resting orders
to manage is read from the spec's `parameters.managed_orders` mapping
(``label -> client_order_id``) so the operator decides which makers this
strategy controls.

Implementation note: the spec lists a GARCH volatility model. The model
pipeline is still scaffolded; this module runs in **rules mode** — widen/
tighten widths come from the spec's `wide_bps` and `tight_bps` parameters.
Swap for a real GARCH-driven cone once the model runner exists.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from eventcontracts.domain.decisions import (
    NoAction,
    ReplaceOrder,
    StrategyDecision,
)
from eventcontracts.domain.events import NormalizedEvent, TimerEvent
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.latency import ExecutionPriority, LatencyTier
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.registry import register

WIDEN_LABEL = "nfp_widen"
TIGHTEN_LABEL = "nfp_tighten"


class MacroNfpAbsorberStrategy(StrategyBase):
    """Widen/tighten resting quotes around the NFP release."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        # Operator-supplied string: ``bid_yes:<client_order_id>,ask_yes:<client_order_id>``.
        self.managed_orders = _parse_managed_orders(
            str(spec.parameters.get("managed_orders", ""))
        )
        self.wide_bps = Decimal(str(spec.parameters.get("wide_bps", "300")))
        self.tight_bps = Decimal(str(spec.parameters.get("tight_bps", "50")))
        self.reference_mid = Decimal(str(spec.parameters.get("reference_mid", "0.5")))

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if not isinstance(event, TimerEvent):
            return (NoAction(reason="ignored:not_timer"),)
        if not self.managed_orders:
            return (NoAction(reason="no_managed_orders_configured"),)

        if event.label == WIDEN_LABEL:
            return self._replace_all(self.wide_bps, "widen_for_nfp", LatencyTier.STANDARD)
        if event.label == TIGHTEN_LABEL:
            return self._replace_all(
                self.tight_bps, "tighten_post_nfp", LatencyTier.RELAXED
            )
        return (NoAction(reason=f"timer_label_unknown:{event.label}"),)

    def _replace_all(
        self, half_width_bps: Decimal, reason: str, tier: LatencyTier
    ) -> Sequence[StrategyDecision]:
        half_width = half_width_bps / Decimal("10000")
        bid_price = max(self.reference_mid - half_width, Decimal("0.01"))
        ask_price = min(self.reference_mid + half_width, Decimal("0.99"))

        decisions: list[StrategyDecision] = []
        for label, coid in sorted(self.managed_orders.items()):
            new_price = bid_price if "bid" in label.lower() else ask_price
            decisions.append(
                ReplaceOrder(
                    client_order_id=coid,
                    new_price=new_price,
                    reason=f"{reason}:{label}",
                    priority=ExecutionPriority(tier=tier),
                )
            )
        return tuple(decisions)


@register("macro_nfp_absorber")
def factory(spec: StrategySpec) -> MacroNfpAbsorberStrategy:
    return MacroNfpAbsorberStrategy(spec)


def _parse_managed_orders(raw: str) -> dict[str, ClientOrderId]:
    orders: dict[str, ClientOrderId] = {}
    for item in raw.split(","):
        if not item.strip():
            continue
        label, sep, client_order_id = item.partition(":")
        if not sep or not label.strip() or not client_order_id.strip():
            raise ValueError(
                "managed_orders must be comma-separated label:client_order_id pairs"
            )
        orders[label.strip()] = ClientOrderId(client_order_id.strip())
    return orders
