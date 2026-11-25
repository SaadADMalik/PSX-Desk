"""Price alerts: when a rule fires, when it re-arms, when it ends. Pure functions.

A rule watches for a fall below a reference price:

  * ``pct``: the price is ``threshold`` percent or more below the reference;
  * ``pkr``: the position has lost ``threshold`` rupees or more against the reference
    (shares held now x fall per share), so it needs a position.

The reference is the position's average cost when the stock is held, otherwise the
price when the rule was set.

It fires on a CROSSING, not on every check below the line:

    armed ──(breach)──► triggered ──(recovers past REARM_AT of the line)──► armed

A price that hovers around the line would otherwise alert at every check. The re-arm
level is 80% of the threshold: a -5% rule fires at -5% and re-arms above -4%.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

REARM_AT = 0.8
Verdict = Literal["trigger", "rearm", "expire", "end", "hold"]


@dataclass(frozen=True)
class Reading:
    drop_pct: float          # how far below the reference, in % (negative: above it)
    loss_pkr: float | None   # rupees lost on the shares held now; None if none held


def reading(ref_price: float, price: float, held: int) -> Reading:
    drop = (ref_price - price) / ref_price * 100
    loss = round((ref_price - price) * held, 2) if held > 0 else None
    return Reading(round(drop, 2), loss)


def measure(kind: str, r: Reading) -> float | None:
    """The quantity the threshold is compared with."""
    return r.drop_pct if kind == "pct" else r.loss_pkr


def verdict(*, kind: str, threshold: float, state: str, ends_at: datetime | None,
            now: datetime, reading: Reading | None) -> Verdict:
    """What one check does to one active rule. `reading` is None when there is no
    fresh price: the rule is left alone rather than judged on stale data."""
    if ends_at is not None and now >= ends_at:
        return "expire"
    if reading is None:
        return "hold"
    value = measure(kind, reading)
    if value is None:  # a rupee rule on a position that has been sold
        return "end"
    if state == "armed" and value >= threshold:
        return "trigger"
    if state == "triggered" and value <= threshold * REARM_AT:
        return "rearm"
    return "hold"
