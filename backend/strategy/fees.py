"""Exact Kalshi trading fees (per ORDER, rounded up to the cent).

Kalshi fee schedule (KXHIGH* series: ``fee_type: quadratic``, multiplier 1):

    taker fee = roundup_to_cent(0.07   x C x P x (1 - P))
    maker fee = roundup_to_cent(0.0175 x C x P x (1 - P))   (charged on fill only)

with C = contract count and P = price in dollars. Rounding is per order, which
is what makes tiny orders on cheap/expensive contracts so costly (1 contract at
97c pays a full 1c on a 0.2c "true" fee).

Unlike v1's ``estimate_fees`` there is no "realistic x0.3" discount here — v2
always charges the exact schedule.
"""

from __future__ import annotations

TAKER_RATE_X4 = 28  # 0.07 expressed in quarters of a cent-percent: 0.07 = 28 / 400
MAKER_RATE_X4 = 7  # 0.0175 = 7 / 400


def kalshi_fee_cents(price_cents: int, count: int, maker: bool = False) -> int:
    """Return the exact Kalshi fee in cents for one order.

    Integer arithmetic only (no float rounding error at the ceiling).

    Args:
        price_cents: Price paid per contract, 1-99 cents (YES price for a YES
            buy, NO price for a NO buy — P(1-P) is symmetric anyway).
        count: Number of contracts in the order (>= 1).
        maker: Use the maker rate instead of the taker rate.

    Returns:
        Fee in whole cents for the whole order.

    Raises:
        ValueError: On a price outside 1-99 or a non-positive count.

    Examples:
        >>> kalshi_fee_cents(97, 1)
        1
        >>> kalshi_fee_cents(50, 10)
        18
    """
    if not 1 <= price_cents <= 99:
        msg = f"price_cents must be 1-99, got {price_cents}"
        raise ValueError(msg)
    if count < 1:
        msg = f"count must be >= 1, got {count}"
        raise ValueError(msg)
    rate_x4 = MAKER_RATE_X4 if maker else TAKER_RATE_X4
    # fee_cents = rate * C * p(100-p) / 100^2 * 100  =  rate_x4 * C * p(100-p) / 40000
    numerator = rate_x4 * count * price_cents * (100 - price_cents)
    return -(-numerator // 40_000)
