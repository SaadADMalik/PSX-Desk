"""The broker's own limits: pure functions over a portfolio, no I/O.

These run twice: when a trade is proposed (so the human sees which checks pass)
and again at the moment of the fill (prices and cash may have moved since). The
proposer's view is never trusted: the broker fetches its own price and computes
its own portfolio.
"""

from dataclasses import dataclass

from contracts import Check, OrderSide, Portfolio, Position


@dataclass(frozen=True)
class BrokerPolicy:
    max_position_pct: float = 20.0  # one stock, % of equity, after the trade
    max_order_pct: float = 10.0  # one order's value, % of equity
    fee_bps: float = 15.0  # commission, paper: 0.15% of value
    min_fee: float = 25.0  # PKR
    slippage_bps: float = 10.0  # fills 0.1% worse than the quote
    max_price_drift_pct: float = 3.0  # quote moved more than this since proposal: refuse
    expiry_hours: float = 24.0  # an unanswered proposal goes stale


def fee(value: float, policy: BrokerPolicy) -> float:
    return round(max(value * policy.fee_bps / 10_000, policy.min_fee), 2)


def fill_price(quote: float, side: OrderSide, policy: BrokerPolicy) -> float:
    adj = policy.slippage_bps / 10_000
    return round(quote * (1 + adj if side is OrderSide.buy else 1 - adj), 4)


def build_portfolio(cash: float, fills: list[dict], marks: dict[str, float | None]) -> Portfolio:
    """Average-cost positions from the fill history; marked at the latest quote, or at
    cost when no quote is available (and then listed as stale)."""
    book: dict[str, tuple[int, float]] = {}
    for f in fills:
        qty, avg = book.get(f["symbol"], (0, 0.0))
        if f["side"] == "buy":
            new = qty + f["quantity"]
            avg = (qty * avg + f["quantity"] * float(f["price"])) / new
            qty = new
        else:
            qty -= f["quantity"]
            if qty == 0:
                avg = 0.0
        book[f["symbol"]] = (qty, avg)

    positions, stale = [], []
    for sym, (qty, avg) in sorted(book.items()):
        if qty == 0:
            continue
        mark = marks.get(sym)
        if mark is None:
            stale.append(sym)
        positions.append(Position(symbol=sym, quantity=qty, avg_cost=round(avg, 4), mark=mark,
                                  market_value=round(qty * (mark or avg), 2), weight_pct=0))
    equity = round(cash + sum(p.market_value for p in positions), 2)
    for p in positions:
        p.weight_pct = round(100 * p.market_value / equity, 2) if equity else 0
    return Portfolio(cash=round(cash, 2), equity=equity, positions=positions, marks_stale=stale)


def pre_trade_checks(symbol: str, side: OrderSide, quantity: int, price: float,
                     pf: Portfolio, policy: BrokerPolicy) -> list[Check]:
    value = quantity * price
    cost = fee(value, policy)
    held = next((p for p in pf.positions if p.symbol == symbol), None)
    held_qty = held.quantity if held else 0
    equity = pf.equity or 1.0
    checks = []
    if side is OrderSide.buy:
        checks.append(Check(rule="cash", ok=pf.cash >= value + cost,
                            detail=f"needs {value + cost:,.0f} incl. fees; cash {pf.cash:,.0f}"))
        after = 100 * (held_qty * price + value) / equity
        checks.append(Check(rule="max_position", ok=after <= policy.max_position_pct,
                            detail=f"{symbol} would be {after:.1f}% of equity "
                                   f"(limit {policy.max_position_pct:g}%)"))
    else:
        checks.append(Check(rule="no_short", ok=quantity <= held_qty,
                            detail=f"selling {quantity}, holding {held_qty}"))
    order_pct = 100 * value / equity
    checks.append(Check(rule="max_order", ok=order_pct <= policy.max_order_pct,
                        detail=f"order is {order_pct:.1f}% of equity "
                               f"(limit {policy.max_order_pct:g}%)"))
    return checks


def failed(checks: list[Check]) -> list[str]:
    return [f"{c.rule}: {c.detail}" for c in checks if not c.ok]
