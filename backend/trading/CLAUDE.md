# Agent 4: Trading Engine

## Your Mission

Build the trading decision engine: EV calculation, risk management, cooldown logic, trade queue (for manual approval mode), and trade execution orchestration. You are the gatekeeper — you decide what trades happen and enforce all safety limits.

## What You Build

```
backend/trading/
├── __init__.py
├── ev_calculator.py   -> Expected value calculation + Kelly-sized signals for each bracket + guardrails (divergence cap, probability blending, YES market floor) + split YES/NO EV thresholds + fee estimate modes (conservative/realistic)
├── kelly.py           -> Kelly Criterion position sizing (fractional Kelly, fee-adjusted, safety caps)
├── risk_manager.py    -> Position limits, daily loss, exposure tracking
├── cooldown.py        -> Cooldown timer logic (per-loss with enable toggle, and consecutive loss with enable toggle)
├── trade_queue.py     -> Trade approval queue for manual mode
├── executor.py        -> Trade execution orchestrator (auto + manual modes). Orders placed with 14-min expiration_ts. Unfilled → RESTING status, filled → OPEN status. No more immediate cancel.
├── postmortem.py      -> Generate full trade post-mortem after settlement
├── sync.py            -> Kalshi portfolio sync (reconciles app Trade records with actual Kalshi filled orders)
├── scheduler.py       -> Celery tasks for trading cycle (passes Kelly params, GuardrailSettings, auto-sync, post-settlement retraining trigger, bracket cap via _get_open_bracket_qty). `_sync_resting_orders()` syncs resting trades at start of each cycle. `_fetch_market_prices()` tries Redis cache → REST yes_ask → orderbook fallback. `best_yes_price_from_orderbook()` extracts best YES ask from orderbook (or derives from NO bids).
├── notifications.py   -> Web push notifications via VAPID
└── exceptions.py      -> Trading-specific exceptions (or import from common)
```

## Dependencies

- **Input from Agent 3 (Prediction):** `BracketPrediction` objects (probability per bracket)
- **Input from Agent 2 (Kalshi):** `KalshiMarket` objects (current prices), `KalshiClient` (for placing orders)
- **User Settings:** Trading mode, max trade size, risk limits, cooldown config
- **Shared Imports:**
  ```python
  from backend.common.schemas import (
      BracketProbability,
      BracketPrediction,
      TradeSignal,
      TradeRecord,
      UserSettings,
      WeatherData,
      PendingTrade,
  )
  from backend.common.logging import get_logger
  from backend.common.config import settings
  from backend.common.database import async_session
  from backend.common.models import Trade, Settlement, DailyRiskState, Prediction
  from backend.common.exceptions import (
      BozBaseException,
      RiskLimitError,
      CooldownActiveError,
      InvalidOrderError,
  )
  ```

---

## CRITICAL: All Kalshi Prices Are in CENTS

The Kalshi API uses **cents** (integers 1-99), not dollars. A market price of `22` means $0.22. A payout is always `100` cents ($1.00). Every function in this module must be explicit about units:

- Function parameters: use `price_cents: int` (not `price: float`)
- Internal EV math: convert to dollars only for final results
- Database storage: store `price_cents` as integer
- API calls: pass cents directly to Kalshi
- Logs: always include the unit — `{"price_cents": 22}` not `{"price": 0.22}`

**If you see a float where cents should be, that is a bug.**

---

## Kalshi Fee Calculation

Kalshi charges fees per contract, based on contract count and probability. There are two fee estimation modes:

### Fee Estimate Mode (`fee_estimate_mode` user setting)

- **`conservative`** (default): Uses the legacy formula `15% of profit` (overestimates fees, safer for users)
- **`realistic`**: Uses Kalshi's actual formula `ceil(0.07 * C * P * (1-P))` where C = number of contracts, P = probability (price/100)

The `fee_estimate_mode` is a per-user setting stored on the User model.

### Kalshi's Actual Fee Formula (Realistic Mode)

```
fee_cents = ceil(0.07 * contracts * price_cents * (100 - price_cents) / 100)
fee_cents = max(fee_cents, 1)  # minimum 1 cent per contract
```

This formula charges based on the variance of the contract outcome, not on profit. It results in lower fees for contracts near the extremes (very high or very low probability) and higher fees near 50%.

### Conservative Fee Formula (Legacy)

```
profit_if_win = (100 - price_cents) for YES, price_cents for NO
fee_cents = max(1, int(profit_if_win * 0.15))
```

### Settlement Fee Handling

At settlement time, `postmortem.py` uses the actual `taker_fees` value from the Kalshi API (stored on the Trade model at sync/execution time), NOT recalculated fees. This ensures P&L accuracy regardless of which estimation mode was used for EV calculation.

### Split EV Thresholds

The EV calculator supports separate thresholds for YES and NO sides:

- `min_ev_threshold_yes`: Minimum EV to trigger a YES trade (default: 0.05)
- `min_ev_threshold_no`: Minimum EV to trigger a NO trade (default: 0.05)

This allows users to set asymmetric thresholds (e.g., higher threshold for NO trades which have different risk profiles). Both fields are stored on the User model and exposed via the settings API. For backward compatibility, the legacy `min_ev_threshold` field maps to `min_ev_threshold_yes`.

### Implementation (ev_calculator.py)

```python
# backend/trading/ev_calculator.py
from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from backend.common.logging import get_logger
from backend.common.schemas import BracketProbability, BracketPrediction, TradeSignal

logger = get_logger("TRADING")
ET = ZoneInfo("America/New_York")


def estimate_fees(price_cents: int, side: str, *, mode: str = "conservative") -> float:
    """Estimate Kalshi fees for EV calculation.

    Args:
        price_cents: Market price in cents (1-99).
        side: "yes" or "no".
        mode: "conservative" (15% of profit) or "realistic" (Kalshi formula).

    Returns:
        Estimated fee in DOLLARS (float).
    """
    if not (1 <= price_cents <= 99):
        raise ValueError(f"price_cents must be 1-99, got {price_cents}")
    if side not in ("yes", "no"):
        raise ValueError(f"side must be 'yes' or 'no', got {side!r}")

    if mode == "realistic":
        p = price_cents / 100
        fee_cents = max(1, math.ceil(0.07 * 1 * price_cents * (100 - price_cents) / 100))
    else:  # conservative
        if side == "yes":
            profit_if_win = 100 - price_cents
        else:
            profit_if_win = price_cents
        fee_cents = max(1, int(profit_if_win * 0.15))

    return fee_cents / 100  # return in dollars
```

---

## EV Calculation

For each bracket in each city, calculate expected value for **both** YES and NO sides. The formula accounts for Kalshi fees.

### The Math

```
EV = (probability_of_winning * $1.00 payout) - cost_in_dollars - estimated_fees

For YES side:
  cost = price_cents / 100
  prob_win = model_probability
  payout = $1.00

For NO side:
  cost = (100 - price_cents) / 100
  prob_win = 1.0 - model_probability
  payout = $1.00
```

### Worked Example

```
Model says 28% chance for bracket "53-54F"
Market YES price: 22 cents ($0.22)

YES side:
  cost = $0.22
  prob_win = 0.28
  fee = estimate_fees(22, "yes") = max(1, int(78 * 0.15)) / 100 = 11/100 = $0.11
  EV = (0.28 * 1.00) - 0.22 - 0.11 = +$0.05 → POTENTIAL TRADE (if > threshold)

  But wait — we charge fees only on wins, so more precisely:
  EV = (0.28 * (1.00 - 0.11)) - 0.22 = (0.28 * 0.89) - 0.22 = 0.2492 - 0.22 = +$0.0292

  SIMPLIFIED APPROACH (recommended for v1): charge fee unconditionally.
  This is conservative — we overestimate fees, which is SAFER for the user.
  EV = (0.28 * 1.00) - 0.22 - 0.11 = -$0.05 → NO TRADE

NO side:
  cost = (100 - 22) / 100 = $0.78
  prob_win = 1.0 - 0.28 = 0.72
  fee = estimate_fees(22, "no") = max(1, int(22 * 0.15)) / 100 = 3/100 = $0.03
  EV = (0.72 * 1.00) - 0.78 - 0.03 = -$0.09 → NO TRADE
```

### Implementation

```python
def calculate_ev(
    model_prob: float,
    market_price_cents: int,
    side: str,
) -> float:
    """Calculate expected value for a potential trade.

    Uses the conservative approach: fees are subtracted unconditionally
    (not only on wins). This slightly underestimates true EV, which is
    safer — we'd rather miss a marginal trade than take a bad one.

    Args:
        model_prob: Our model's probability (0.0 to 1.0).
        market_price_cents: Kalshi market price in CENTS (1-99).
        side: "yes" or "no".

    Returns:
        Expected value in dollars (positive = profitable).
    """
    if not (0.0 <= model_prob <= 1.0):
        raise ValueError(f"model_prob must be 0.0-1.0, got {model_prob}")

    if side == "yes":
        payout_if_win = 1.00  # $1.00 payout
        prob_win = model_prob
        cost_dollars = market_price_cents / 100
    elif side == "no":
        payout_if_win = 1.00
        prob_win = 1.0 - model_prob
        cost_dollars = (100 - market_price_cents) / 100
    else:
        raise ValueError(f"side must be 'yes' or 'no', got {side!r}")

    fees = estimate_fees(market_price_cents, side)

    ev = (prob_win * payout_if_win) - cost_dollars - fees
    return round(ev, 4)
```

---

## Scanning Both Sides

For every bracket, the agent must calculate EV for **BOTH** YES and NO. Pick the better side if both are positive. This is the core scanning loop.

### Implementation

```python
def scan_bracket(
    bracket: BracketProbability,
    market_price_cents: int,
    min_ev_threshold: float,
    city: str,
    prediction_date: str,
    confidence: str,
) -> TradeSignal | None:
    """Scan a single bracket for trading opportunities on both sides.

    Args:
        bracket: Model's probability for this bracket.
        market_price_cents: Current Kalshi YES price in cents.
        min_ev_threshold: Minimum EV in dollars to trigger a trade.
        city: City code (e.g., "NYC").
        prediction_date: Date string for the event.
        confidence: Model confidence level ("HIGH", "MEDIUM", "LOW").

    Returns:
        TradeSignal if a +EV opportunity exists, None otherwise.
    """
    # Calculate EV for both sides
    ev_yes = calculate_ev(bracket.probability, market_price_cents, "yes")
    ev_no = calculate_ev(bracket.probability, market_price_cents, "no")

    logger.debug(
        "Bracket scan",
        extra={
            "data": {
                "city": city,
                "bracket": bracket.bracket_label,
                "model_prob": bracket.probability,
                "market_cents": market_price_cents,
                "ev_yes": ev_yes,
                "ev_no": ev_no,
            }
        },
    )

    # Pick the better side if both are positive
    best_side = None
    best_ev = 0.0

    if ev_yes >= ev_no and ev_yes >= min_ev_threshold:
        best_side = "yes"
        best_ev = ev_yes
    elif ev_no > ev_yes and ev_no >= min_ev_threshold:
        best_side = "no"
        best_ev = ev_no

    if best_side is None:
        return None  # No trade

    market_prob = (
        market_price_cents / 100 if best_side == "yes" else (100 - market_price_cents) / 100
    )

    return TradeSignal(
        city=city,
        bracket=bracket,
        side=best_side,
        market_price=market_price_cents,  # store in cents
        model_probability=bracket.probability,
        ev=best_ev,
        confidence=confidence,
        reasoning=_generate_signal_reasoning(bracket, market_price_cents, best_side, best_ev),
    )


def scan_all_brackets(
    prediction: BracketPrediction,
    market_prices: dict[str, int],  # bracket_label -> price_cents
    min_ev_threshold: float,
) -> list[TradeSignal]:
    """Scan all brackets for a city and return all +EV trade signals.

    Args:
        prediction: Full bracket prediction for one city.
        market_prices: Mapping of bracket label to current YES price in cents.
        min_ev_threshold: Minimum EV in dollars to trigger a trade.

    Returns:
        List of TradeSignal objects, sorted by EV descending.
    """
    signals = []
    for bracket in prediction.brackets:
        price = market_prices.get(bracket.bracket_label)
        if price is None:
            logger.warning(
                "No market price for bracket",
                extra={"data": {"city": prediction.city, "bracket": bracket.bracket_label}},
            )
            continue
        signal = scan_bracket(
            bracket=bracket,
            market_price_cents=price,
            min_ev_threshold=min_ev_threshold,
            city=prediction.city,
            prediction_date=str(prediction.date),
            confidence=prediction.confidence,
        )
        if signal is not None:
            signals.append(signal)

    # Sort by EV descending — best opportunity first
    signals.sort(key=lambda s: s.ev, reverse=True)

    logger.info(
        "Bracket scan complete",
        extra={
            "data": {
                "city": prediction.city,
                "total_brackets": len(prediction.brackets),
                "signals_found": len(signals),
            }
        },
    )
    return signals


def _generate_signal_reasoning(
    bracket: BracketProbability,
    market_price_cents: int,
    side: str,
    ev: float,
) -> str:
    """Generate human-readable reasoning for a trade signal."""
    model_pct = bracket.probability * 100
    market_pct = market_price_cents if side == "yes" else (100 - market_price_cents)
    edge = model_pct - market_pct
    return (
        f"Model: {model_pct:.1f}% vs Market: {market_pct}% "
        f"({'+' if edge > 0 else ''}{edge:.1f}% edge). "
        f"EV: ${ev:+.4f} per contract on {side.upper()} side."
    )
```

---

## Trading Engine Guardrails (ev_calculator.py)

Three guardrails are applied inside `scan_bracket()` and `scan_all_brackets()` BEFORE EV calculation:

### 1. Divergence Cap
If the absolute difference between model probability and market-implied probability exceeds `max_model_market_divergence`, the bracket is blocked. This prevents the bot from trading on extreme model-market disagreements that may indicate a model error.

### 2. Probability Blending
Instead of using raw model probability, the EV calculation uses a blended probability: `blended = model_weight * model_prob + (1 - model_weight) * market_prob`. This anchors the model to market consensus and reduces overconfidence. The `blended_probability` is stored on both `TradeSignal` and the `Trade` database record.

### 3. YES Market Floor
For YES-side trades, if the market-implied probability (price/100) is below `min_market_prob_for_yes`, the trade is blocked. This prevents buying YES on brackets the market considers extremely unlikely.

### Implementation

```python
@dataclass(frozen=True)
class GuardrailSettings:
    model_weight: float = 0.7
    max_model_market_divergence: float = 0.30
    min_market_prob_for_yes: float = 0.05


def apply_guardrails(
    model_prob: float,
    market_price_cents: int,
    side: str,
    gs: GuardrailSettings,
) -> tuple[float, bool, str]:
    """Apply guardrails and return (blended_prob, blocked, reason)."""
```

The `GuardrailSettings` is constructed from User columns in `scheduler.py` and passed through `scan_bracket()` / `scan_all_brackets()`. Blocked trades increment the `GUARDRAIL_BLOCKED_TOTAL` Prometheus counter.

---

## Input Validation (Defensive Programming)

The trading engine must **NEVER** trust upstream data blindly. Validate before every trading cycle.

### Implementation

```python
# In ev_calculator.py or a separate validation.py

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def validate_predictions(predictions: list[BracketPrediction]) -> bool:
    """Validate prediction data before trading on it.

    Returns True if ALL predictions are valid. Logs specific errors.
    If any prediction is invalid, returns False — halt trading for this cycle.
    """
    for pred in predictions:
        # Probabilities must sum to ~1.0 (allow small floating point error)
        total = sum(b.probability for b in pred.brackets)
        if not (0.95 <= total <= 1.05):
            logger.error(
                "Bracket probabilities do not sum to 1.0",
                extra={"data": {"city": pred.city, "total": total}},
            )
            return False

        # No NaN or negative probabilities
        for b in pred.brackets:
            if math.isnan(b.probability) or b.probability < 0:
                logger.error(
                    "Invalid probability value",
                    extra={
                        "data": {
                            "city": pred.city,
                            "bracket": b.bracket_label,
                            "probability": b.probability,
                        }
                    },
                )
                return False

        # Must have exactly 6 brackets
        if len(pred.brackets) != 6:
            logger.error(
                "Expected 6 brackets",
                extra={"data": {"city": pred.city, "count": len(pred.brackets)}},
            )
            return False

        # Data freshness check — predictions older than 2 hours are stale
        age = datetime.now(ET) - pred.generated_at
        if age > timedelta(hours=2):
            logger.warning(
                "Stale predictions detected",
                extra={
                    "data": {
                        "city": pred.city,
                        "age_hours": round(age.total_seconds() / 3600, 2),
                    }
                },
            )
            return False

    return True


def validate_market_prices(prices: dict[str, int]) -> bool:
    """Validate market prices from Kalshi before using them."""
    for label, price in prices.items():
        if not isinstance(price, int):
            logger.error(
                "Market price is not an integer", extra={"data": {"bracket": label, "price": price}}
            )
            return False
        if not (1 <= price <= 99):
            logger.error(
                "Market price out of range",
                extra={"data": {"bracket": label, "price_cents": price}},
            )
            return False
    return True
```

---

## Risk Management

All limits are user-configurable with safe defaults:

| Risk Control | Default | Range | Description |
|-------------|---------|-------|-------------|
| Max trade size | $1.00 | $0.01 - $100 | Maximum cost per individual trade |
| Daily loss limit | $10.00 | $1 - $1000 | Stop trading after this much loss in one day |
| Max daily exposure | $25.00 | $1 - $5000 | Total capital at risk across all open positions |
| Min EV threshold | 5% ($0.05) | 1% - 50% | Minimum expected value to trigger a trade |
| Cooldown per loss | 60 min | 0 (off) - 1440 min (24h) | Pause after each losing trade |
| Consecutive loss limit | 3 | 0 (off) - 10 | Pause for rest of day after N losses in a row |
| Consecutive loss toggle | on | on/off | Enable/disable rest-of-day cooldown (counter still increments, per-loss still fires) |
| Max contracts per bracket | 5 | 1 - 20 | Hard cap on open contracts per bracket per market |
| Model weight (guardrail) | 0.7 | 0.0 - 1.0 | Weight given to model probability vs market in blended probability |
| Max model-market divergence (guardrail) | 0.30 | 0.05 - 1.0 | Block trades where model and market disagree by more than this |
| Min market prob for YES (guardrail) | 0.05 | 0.0 - 0.50 | Block YES trades where market probability is below this floor |

**Risk checks happen BEFORE every trade, no exceptions:**
1. Is cooldown active? -> BLOCK
2. Would this trade exceed max trade size? -> BLOCK
3. Would this trade push daily exposure over limit? -> BLOCK
4. Has daily loss limit been hit? -> BLOCK
5. Is the EV above minimum threshold? -> If no, SKIP
6. Would this trade exceed per-bracket position cap? -> BLOCK (via `_get_open_bracket_qty()` in scheduler.py)
7. All checks pass -> PROCEED

### RiskManager Implementation (risk_manager.py)

```python
# backend/trading/risk_manager.py
from __future__ import annotations

from datetime import datetime, date
from zoneinfo import ZoneInfo

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.logging import get_logger
from backend.common.schemas import TradeSignal, TradeRecord, UserSettings
from backend.common.models import Trade, DailyRiskState, TradeStatus
from backend.common.exceptions import RiskLimitError, CooldownActiveError

logger = get_logger("RISK")
ET = ZoneInfo("America/New_York")


def get_trading_day() -> date:
    """Get current trading day in ET."""
    return datetime.now(ET).date()


def is_new_trading_day(last_trading_day: date) -> bool:
    """Check if we've crossed into a new trading day."""
    return get_trading_day() > last_trading_day


class RiskManager:
    """Enforces all trading risk limits. Uses database-level locking for concurrency safety."""

    def __init__(self, settings: UserSettings, db: AsyncSession):
        self.settings = settings
        self.db = db

    async def check_trade(self, signal: TradeSignal) -> tuple[bool, str]:
        """Run ALL risk checks. Returns (allowed, reason).

        Checks run IN ORDER — first failure short-circuits:
        1. Cooldown active?
        2. Trade size within max?
        3. Daily exposure limit?
        4. Daily loss limit?
        5. EV above threshold?
        """
        # 1. Cooldown check
        cooldown_active, cooldown_reason = await self._check_cooldown()
        if cooldown_active:
            logger.info("Trade blocked: cooldown", extra={"data": {"reason": cooldown_reason}})
            return False, f"Cooldown active: {cooldown_reason}"

        # 2. Trade size check
        trade_cost = signal.market_price / 100  # cents to dollars
        if signal.side == "no":
            trade_cost = (100 - signal.market_price) / 100
        if trade_cost > self.settings.max_trade_size:
            logger.info(
                "Trade blocked: exceeds max trade size",
                extra={"data": {"cost": trade_cost, "max": self.settings.max_trade_size}},
            )
            return (
                False,
                f"Trade cost ${trade_cost:.2f} exceeds max ${self.settings.max_trade_size:.2f}",
            )

        # 3. Daily exposure check
        current_exposure = await self.get_open_exposure()
        if current_exposure + trade_cost > self.settings.max_daily_exposure:
            logger.info(
                "Trade blocked: daily exposure limit",
                extra={
                    "data": {
                        "current_exposure": current_exposure,
                        "trade_cost": trade_cost,
                        "limit": self.settings.max_daily_exposure,
                    }
                },
            )
            return (
                False,
                f"Would exceed daily exposure (${current_exposure:.2f} + ${trade_cost:.2f} > ${self.settings.max_daily_exposure:.2f})",
            )

        # 4. Daily loss check
        daily_pnl = await self.get_daily_pnl()
        if daily_pnl <= -self.settings.daily_loss_limit:
            logger.info(
                "Trade blocked: daily loss limit",
                extra={"data": {"daily_pnl": daily_pnl, "limit": self.settings.daily_loss_limit}},
            )
            return (
                False,
                f"Daily loss limit reached (P&L: ${daily_pnl:.2f}, limit: -${self.settings.daily_loss_limit:.2f})",
            )

        # 5. EV threshold check
        if signal.ev < self.settings.min_ev_threshold:
            return (
                False,
                f"EV ${signal.ev:.4f} below threshold ${self.settings.min_ev_threshold:.4f}",
            )

        logger.info(
            "Trade approved by risk manager",
            extra={
                "data": {
                    "city": signal.city,
                    "bracket": signal.bracket.bracket_label,
                    "side": signal.side,
                    "ev": signal.ev,
                    "cost": trade_cost,
                }
            },
        )
        return True, "All checks passed"

    async def get_daily_pnl(self) -> float:
        """Sum today's realized P&L from settled trades."""
        trading_day = get_trading_day()
        result = await self.db.execute(
            select(func.coalesce(func.sum(Trade.pnl), 0.0)).where(
                Trade.settled_at.isnot(None),
                func.date(Trade.trade_date) == trading_day,
            )
        )
        return float(result.scalar())

    async def get_open_exposure(self) -> float:
        """Sum cost of all unsettled open positions."""
        result = await self.db.execute(
            select(func.coalesce(func.sum(Trade.entry_price * Trade.quantity), 0.0)).where(
                Trade.status.in_([TradeStatus.EXECUTED, TradeStatus.PENDING, TradeStatus.APPROVED])
            )
        )
        return float(result.scalar())

    async def record_trade(self, trade: TradeRecord) -> None:
        """Update risk tracking after a trade executes."""
        logger.info(
            "Recording trade for risk tracking",
            extra={
                "data": {
                    "trade_id": trade.id,
                    "city": trade.city,
                    "side": trade.side,
                    "cost": trade.entry_price,
                }
            },
        )

    async def _check_cooldown(self) -> tuple[bool, str]:
        """Check if any cooldown is currently active.

        Returns (is_active, reason).
        """
        # Delegate to CooldownManager — see cooldown.py section below
        from backend.trading.cooldown import CooldownManager

        cm = CooldownManager(self.settings, self.db)
        return await cm.is_cooldown_active()

    async def handle_daily_reset(self) -> None:
        """Reset daily counters when a new trading day starts.

        Call this at the start of every trading cycle.
        """
        trading_day = get_trading_day()
        state = await self._get_or_create_daily_state(trading_day)
        if state.is_reset:
            return  # already reset for today

        state.total_pnl = 0.0
        state.total_exposure = 0.0
        state.consecutive_losses = 0
        state.is_reset = True
        await self.db.commit()

        logger.info(
            "Daily limits reset",
            extra={"data": {"new_day": str(trading_day)}},
        )

    async def _get_or_create_daily_state(self, trading_day: date) -> DailyRiskState:
        """Get or create the DailyRiskState row for the given trading day."""
        result = await self.db.execute(
            select(DailyRiskState).where(DailyRiskState.trading_day == trading_day)
        )
        state = result.scalar_one_or_none()
        if state is None:
            state = DailyRiskState(trading_day=trading_day)
            self.db.add(state)
            await self.db.flush()
        return state
```

---

## Concurrency Safety

Risk checks **MUST** use database-level locking to prevent race conditions. Two concurrent Celery workers must not both approve trades that together exceed the daily exposure limit.

### Pattern: SELECT FOR UPDATE

```python
async def check_and_reserve_exposure(self, amount: float) -> bool:
    """Atomically check exposure limit and reserve if allowed.

    Uses SELECT FOR UPDATE to prevent race conditions when
    multiple trading cycles run concurrently.
    """
    async with self.db.begin():
        result = await self.db.execute(
            select(DailyRiskState)
            .where(DailyRiskState.trading_day == get_trading_day())
            .with_for_update()  # Lock the row
        )
        state = result.scalar_one_or_none()
        if state is None:
            state = DailyRiskState(trading_day=get_trading_day())
            self.db.add(state)

        if state.total_exposure + amount > self.settings.max_daily_exposure:
            logger.info(
                "Exposure reservation denied",
                extra={
                    "data": {
                        "requested": amount,
                        "current": state.total_exposure,
                        "limit": self.settings.max_daily_exposure,
                    }
                },
            )
            return False

        state.total_exposure += amount
        logger.info(
            "Exposure reserved",
            extra={
                "data": {
                    "amount": amount,
                    "new_total": state.total_exposure,
                }
            },
        )
        return True
```

**Important:** Always use `check_and_reserve_exposure()` instead of reading exposure and then writing separately. The read-then-write pattern has a TOCTOU race condition.

---

## Daily Reset Timing

- Daily loss limit and daily exposure reset at **midnight ET (Eastern Time)**
- The "trading day" runs from 00:00 ET to 23:59 ET
- We use a single daily reset for all cities (not per-city)

### Implementation

```python
from datetime import datetime, date
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def get_trading_day() -> date:
    """Get current trading day in ET."""
    return datetime.now(ET).date()


def is_new_trading_day(last_trading_day: date) -> bool:
    """Check if we've crossed into a new trading day."""
    return get_trading_day() > last_trading_day
```

### When `is_new_trading_day()` Returns True

Execute these resets at the start of the first trading cycle of the new day:

1. Reset daily P&L counter to 0
2. Reset daily exposure counter to 0
3. Reset consecutive loss counter to 0
4. Clear any active per-loss cooldowns
5. Log: `RISK`, `"Daily limits reset"`, `{"new_day": trading_day}`

This logic lives in `RiskManager.handle_daily_reset()` (shown above).

---

## Cooldown Logic

```python
# backend/trading/cooldown.py
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.logging import get_logger
from backend.common.schemas import UserSettings
from backend.common.models import DailyRiskState

logger = get_logger("COOLDOWN")
ET = ZoneInfo("America/New_York")


class CooldownManager:
    """Manages per-loss and consecutive-loss cooldown timers."""

    def __init__(self, settings: UserSettings, db: AsyncSession):
        self.settings = settings
        self.db = db

    async def is_cooldown_active(self) -> tuple[bool, str]:
        """Check if any cooldown is currently active.

        Returns (is_active, reason_string).
        """
        state = await self._get_daily_state()
        if state is None:
            return False, ""

        now = datetime.now(ET)

        # Check per-loss cooldown
        if state.cooldown_until and now < state.cooldown_until:
            remaining = (state.cooldown_until - now).total_seconds() / 60
            reason = f"Per-loss cooldown: {remaining:.0f} min remaining"
            logger.info(
                "Cooldown active", extra={"data": {"type": "per_loss", "remaining_min": remaining}}
            )
            return True, reason

        # Check consecutive-loss cooldown (rest of day)
        if state.rest_of_day_cooldown:
            reason = "Consecutive loss limit hit — paused for rest of trading day"
            logger.info("Cooldown active", extra={"data": {"type": "consecutive_loss"}})
            return True, reason

        return False, ""

    async def on_trade_loss(self) -> None:
        """Called when a trade settles as a loss. Updates cooldown state."""
        state = await self._get_or_create_daily_state()
        now = datetime.now(ET)

        # Per-loss cooldown
        if self.settings.cooldown_per_loss_minutes > 0:
            state.cooldown_until = now + timedelta(minutes=self.settings.cooldown_per_loss_minutes)
            logger.info(
                "Per-loss cooldown activated",
                extra={"data": {"until": str(state.cooldown_until)}},
            )

        # Consecutive loss tracking
        state.consecutive_losses += 1
        if (
            self.settings.consecutive_loss_limit > 0
            and state.consecutive_losses >= self.settings.consecutive_loss_limit
        ):
            state.rest_of_day_cooldown = True
            logger.warning(
                "Consecutive loss limit hit",
                extra={
                    "data": {
                        "count": state.consecutive_losses,
                        "limit": self.settings.consecutive_loss_limit,
                    }
                },
            )

        await self.db.commit()

    async def on_trade_win(self) -> None:
        """Called when a trade settles as a win. Resets consecutive loss counter."""
        state = await self._get_or_create_daily_state()
        state.consecutive_losses = 0
        await self.db.commit()
        logger.info("Consecutive loss counter reset (win)", extra={"data": {}})

    async def _get_daily_state(self) -> DailyRiskState | None:
        """Get today's risk state, or None if not yet created."""
        from backend.trading.risk_manager import get_trading_day

        result = await self.db.execute(
            select(DailyRiskState).where(DailyRiskState.trading_day == get_trading_day())
        )
        return result.scalar_one_or_none()

    async def _get_or_create_daily_state(self) -> DailyRiskState:
        """Get or create today's risk state."""
        from backend.trading.risk_manager import get_trading_day

        state = await self._get_daily_state()
        if state is None:
            state = DailyRiskState(trading_day=get_trading_day())
            self.db.add(state)
            await self.db.flush()
        return state
```

### Cooldown State Transitions

```
Trade Loss:
  └── cooldown_per_loss > 0?
      └── YES: Set cooldown_until = now + cooldown_per_loss_minutes
  └── Increment consecutive_losses
      └── enable_consecutive_loss_limit AND >= consecutive_loss_limit?
          └── YES: Set rest_of_day_cooldown = True (paused until midnight ET)

Trade Win:
  └── Reset consecutive_losses = 0
  └── (per-loss cooldown timer is NOT cleared by a win — it expires naturally)

New Trading Day:
  └── Reset consecutive_losses = 0
  └── Clear cooldown_until
  └── Clear rest_of_day_cooldown
```

---

## Trade Queue (Manual Approval Mode)

When trading mode is "manual":
1. Bot identifies +EV trade
2. Create `PendingTrade` record in database (status: PENDING)
3. Send push notification to user with trade details
4. User sees trade in PWA dashboard trade queue
5. User taps Approve -> bot executes via Kalshi client
6. User taps Reject -> trade marked REJECTED, logged
7. Trade expires after configurable timeout (default: 30 min) -> marked EXPIRED, logged

### Implementation (trade_queue.py)

```python
# backend/trading/trade_queue.py
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.logging import get_logger
from backend.common.schemas import TradeSignal, PendingTrade
from backend.common.models import Trade, TradeStatus

logger = get_logger("TRADING")
ET = ZoneInfo("America/New_York")

PENDING_TRADE_TTL_MINUTES = 30


async def queue_trade(
    signal: TradeSignal,
    db: AsyncSession,
    notification_service: "NotificationService",
) -> PendingTrade:
    """Queue a trade for manual user approval.

    Creates a PendingTrade in the database and sends a push notification.
    The trade will auto-expire after PENDING_TRADE_TTL_MINUTES.
    """
    now = datetime.now(ET)
    pending = PendingTrade(
        id=str(uuid4()),
        city=signal.city,
        bracket=signal.bracket.bracket_label,
        side=signal.side,
        price=signal.market_price,  # stored in CENTS
        quantity=1,  # default; could be configurable
        model_probability=signal.model_probability,
        market_probability=signal.market_price / 100,
        ev=signal.ev,
        confidence=signal.confidence,
        reasoning=signal.reasoning,
        status="PENDING",
        created_at=now,
        expires_at=now + timedelta(minutes=PENDING_TRADE_TTL_MINUTES),
        acted_at=None,
    )

    # Save to DB (use the Trade model with PENDING status)
    trade_model = Trade(
        id=pending.id,
        city=signal.city,
        bracket_label=signal.bracket.bracket_label,
        side=signal.side,
        entry_price=signal.market_price / 100,  # store dollars in DB
        quantity=pending.quantity,
        model_probability=signal.model_probability,
        market_probability=signal.market_price / 100,
        ev_at_entry=signal.ev,
        confidence=signal.confidence,
        status=TradeStatus.PENDING,
        created_at=now,
        trade_date=now,
    )
    db.add(trade_model)
    await db.commit()

    # Send push notification
    await notification_service.send(
        title=f"+EV Trade: {signal.city} {signal.bracket.bracket_label}",
        body=f"EV: +${signal.ev:.2f} | {signal.confidence} confidence | {signal.side.upper()} @ {signal.market_price}c",
        data={"trade_id": pending.id},
    )

    logger.info(
        "Trade queued for approval",
        extra={
            "data": {
                "trade_id": pending.id,
                "city": signal.city,
                "bracket": signal.bracket.bracket_label,
                "side": signal.side,
                "ev": signal.ev,
                "expires_at": str(pending.expires_at),
            }
        },
    )

    return pending


async def approve_trade(trade_id: str, db: AsyncSession) -> Trade:
    """Approve a pending trade for execution.

    Returns the updated Trade model. Caller is responsible for
    actually executing the trade via executor.py.
    """
    result = await db.execute(select(Trade).where(Trade.id == trade_id))
    trade = result.scalar_one_or_none()

    if trade is None:
        raise ValueError(f"Trade {trade_id} not found")
    if trade.status != TradeStatus.PENDING:
        raise ValueError(f"Trade {trade_id} is {trade.status.value}, not PENDING")
    if datetime.now(ET) > trade.created_at + timedelta(minutes=PENDING_TRADE_TTL_MINUTES):
        trade.status = TradeStatus.EXPIRED
        await db.commit()
        raise ValueError(f"Trade {trade_id} has expired")

    trade.status = TradeStatus.APPROVED
    await db.commit()

    logger.info("Trade approved", extra={"data": {"trade_id": trade_id}})
    return trade


async def reject_trade(trade_id: str, db: AsyncSession) -> Trade:
    """Reject a pending trade."""
    result = await db.execute(select(Trade).where(Trade.id == trade_id))
    trade = result.scalar_one_or_none()

    if trade is None:
        raise ValueError(f"Trade {trade_id} not found")
    if trade.status != TradeStatus.PENDING:
        raise ValueError(f"Trade {trade_id} is {trade.status.value}, not PENDING")

    trade.status = TradeStatus.REJECTED
    await db.commit()

    logger.info("Trade rejected", extra={"data": {"trade_id": trade_id}})
    return trade


async def expire_stale_trades(db: AsyncSession) -> int:
    """Expire all pending trades past their TTL. Returns count expired."""
    now = datetime.now(ET)
    cutoff = now - timedelta(minutes=PENDING_TRADE_TTL_MINUTES)

    result = await db.execute(
        update(Trade)
        .where(
            Trade.status == TradeStatus.PENDING,
            Trade.created_at < cutoff,
        )
        .values(status=TradeStatus.EXPIRED)
    )
    await db.commit()

    count = result.rowcount
    if count > 0:
        logger.info("Expired stale pending trades", extra={"data": {"count": count}})
    return count
```

### Pending Trade State Machine

```
                     ┌─────────────┐
                     │   PENDING   │
                     └──────┬──────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │ APPROVED │ │ REJECTED │ │ EXPIRED  │
        └─────┬────┘ └──────────┘ └──────────┘
              │
              ▼
        ┌──────────┐
        │ EXECUTED │ (order placed on Kalshi)
        └─────┬────┘
              │
        ┌─────┴─────┐
        ▼           ▼
   ┌────────┐  ┌────────┐
   │  WON   │  │  LOST  │
   └────────┘  └────────┘
```

---

## Trade Execution Flow (Auto Mode)

### Implementation (executor.py)

```python
# backend/trading/executor.py
from __future__ import annotations

from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.logging import get_logger
from backend.common.schemas import TradeSignal, TradeRecord, UserSettings, BracketPrediction
from backend.common.models import Trade, TradeStatus
from backend.common.exceptions import InvalidOrderError

logger = get_logger("ORDER")
ET = ZoneInfo("America/New_York")


async def execute_trade(
    signal: TradeSignal,
    kalshi_client: "KalshiClient",
    db: AsyncSession,
) -> TradeRecord:
    """Execute a trade on Kalshi and record it.

    Steps:
    1. Build order params (market ticker, side, quantity, price)
    2. Place order via Kalshi API
    3. Handle response: FILLED, PARTIAL, REJECTED
    4. Record in database
    5. Update risk manager
    6. Log everything

    Raises:
        InvalidOrderError: If order parameters are invalid.
        KalshiAPIError: If the Kalshi API rejects the order.
    """
    # Build the order
    order_params = {
        "ticker": signal.market_ticker,  # e.g., "KXHIGHNY-26FEB17-B3"
        "action": "buy",
        "side": signal.side,  # "yes" or "no"
        "type": "limit",
        "count": signal.quantity,
        "yes_price": signal.market_price if signal.side == "yes" else None,
        "no_price": signal.market_price if signal.side == "no" else None,
    }

    logger.info(
        "Placing order",
        extra={
            "data": {
                "ticker": signal.market_ticker,
                "side": signal.side,
                "price_cents": signal.market_price,
                "quantity": signal.quantity,
            }
        },
    )

    try:
        response = await kalshi_client.create_order(order_params)
    except Exception as e:
        logger.error(
            "Order placement failed",
            extra={
                "data": {
                    "ticker": signal.market_ticker,
                    "error": str(e),
                    "side": signal.side,
                    "price_cents": signal.market_price,
                }
            },
        )
        raise

    # Handle partial fills
    order_data = response.get("order", {})
    filled_count = order_data.get("count", signal.quantity)
    remaining = order_data.get("remaining_count", 0)

    if remaining > 0:
        logger.info(
            "Partial fill",
            extra={
                "data": {
                    "ticker": signal.market_ticker,
                    "filled": filled_count,
                    "remaining": remaining,
                }
            },
        )

    # Verify order status after placement
    order_id = order_data.get("order_id")
    if order_id:
        order_status = await kalshi_client.get_order(order_id)
        if order_status.get("status") == "canceled":
            logger.warning(
                "Order was canceled by exchange",
                extra={"data": {"order_id": order_id}},
            )
            raise InvalidOrderError(
                "Order canceled by exchange",
                context={"order_id": order_id, "ticker": signal.market_ticker},
            )

    # Record the trade
    trade_id = str(uuid4())
    cost_dollars = signal.market_price / 100
    if signal.side == "no":
        cost_dollars = (100 - signal.market_price) / 100

    trade = Trade(
        id=trade_id,
        kalshi_order_id=order_id,
        city=signal.city,
        bracket_label=signal.bracket.bracket_label,
        side=signal.side,
        entry_price=cost_dollars,
        quantity=filled_count,
        model_probability=signal.model_probability,
        market_probability=signal.market_price / 100,
        ev_at_entry=signal.ev,
        confidence=signal.confidence,
        status=TradeStatus.EXECUTED,
        trade_date=datetime.now(ET),
        created_at=datetime.now(ET),
    )

    db.add(trade)
    await db.commit()

    logger.info(
        "Trade executed and recorded",
        extra={
            "data": {
                "trade_id": trade_id,
                "order_id": order_id,
                "city": signal.city,
                "bracket": signal.bracket.bracket_label,
                "side": signal.side,
                "price_cents": signal.market_price,
                "quantity": filled_count,
                "ev": signal.ev,
            }
        },
    )

    return TradeRecord(
        id=trade_id,
        city=signal.city,
        date=datetime.now(ET).date(),
        bracket_label=signal.bracket.bracket_label,
        side=signal.side,
        entry_price=cost_dollars,
        quantity=filled_count,
        model_probability=signal.model_probability,
        market_probability=signal.market_price / 100,
        ev_at_entry=signal.ev,
        confidence=signal.confidence,
        weather_forecasts=[],
        prediction=None,
        status="OPEN",
        settlement_temp_f=None,
        settlement_source=None,
        pnl=None,
        postmortem=None,
        created_at=datetime.now(ET),
        settled_at=None,
    )
```

### Order Rejection & Partial Fill Handling

- If Kalshi **rejects** an order: log it, do **NOT** retry (market conditions changed)
- If order is **partially filled**: record only the filled quantity, update risk limits for partial amount
- If order **times out**: check order status via API, handle accordingly
- Always verify order status after placement (see `execute_trade` above)

```python
# Post-placement verification pattern
order_status = await kalshi_client.get_order(order_id)
if order_status["status"] == "canceled":
    logger.warning("ORDER", "Order was canceled by exchange", {...})
elif order_status["remaining_count"] > 0:
    logger.info("ORDER", "Partial fill", {"filled": filled, "remaining": remaining})
```

---

## Trade Post-Mortem Generation

After settlement, generate a full post-mortem for each trade (see PRD Section 3.6):
- Pull the weather forecasts that were active at time of trade
- Pull the actual NWS CLI settlement data
- Compare model prediction vs. actual outcome
- Determine which weather models were most/least accurate
- Calculate final P&L after fees
- Generate human-readable narrative explaining why the trade won/lost
- Store in database, linked to the trade record

### Implementation (postmortem.py)

```python
# backend/trading/postmortem.py
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.logging import get_logger
from backend.common.schemas import TradeRecord, WeatherData
from backend.common.models import Trade, Settlement, WeatherForecast, TradeStatus

logger = get_logger("POSTMORTEM")
ET = ZoneInfo("America/New_York")


def generate_postmortem_narrative(
    trade: Trade,
    settlement: Settlement,
    forecasts: list[WeatherForecast],
) -> str:
    """Generate human-readable post-mortem explanation.

    Example output:
    "LOSS (-$0.22): Bought YES on NYC 53-54F at 22c. Actual high was 56F
    (bracket 55-56F). Our model predicted 28% probability -- the actual outcome
    fell 2F above our target bracket. NWS forecast was closest at 55F, while
    ECMWF predicted 54F. The warm bias likely came from unexpectedly clear
    afternoon skies increasing solar heating."
    """
    actual_temp = settlement.actual_high_f
    bracket = trade.bracket_label
    side = trade.side.upper()
    price_cents = int(trade.entry_price * 100)
    won = trade.status == TradeStatus.WON

    # Determine outcome
    if won:
        outcome_str = f"WIN (+${trade.pnl:.2f})"
    else:
        outcome_str = f"LOSS (-${abs(trade.pnl or 0):.2f})"

    # Build forecast comparison
    forecast_lines = []
    for fc in sorted(forecasts, key=lambda f: abs(f.forecast_high_f - actual_temp)):
        diff = fc.forecast_high_f - actual_temp
        forecast_lines.append(
            f"{fc.source}: {fc.forecast_high_f:.0f}F ({'+' if diff > 0 else ''}{diff:.0f}F off)"
        )

    forecast_summary = "; ".join(forecast_lines[:4])  # top 4 models

    narrative = (
        f"{outcome_str}: Bought {side} on {trade.city} {bracket} at {price_cents}c. "
        f"Actual high was {actual_temp:.0f}F. "
        f"Our model predicted {trade.model_probability:.0%} probability for this bracket. "
        f"Forecast accuracy: {forecast_summary}."
    )

    return narrative


async def settle_trade(
    trade: Trade,
    settlement: Settlement,
    db: AsyncSession,
) -> None:
    """Settle a trade after the actual temperature is known.

    Determines win/loss, calculates P&L (including fees), generates
    post-mortem narrative, and updates the trade record.
    """
    actual_temp = settlement.actual_high_f
    bracket = trade.bracket_label

    # Parse bracket bounds (e.g., "53-54F" -> 53, 54)
    won = _did_bracket_win(bracket, actual_temp, trade.side)

    # Calculate P&L
    cost = trade.entry_price * trade.quantity
    if won:
        payout = 1.00 * trade.quantity
        profit = payout - cost
        fee = max(0.01, profit * 0.15) * trade.quantity
        pnl = profit - fee
        trade.status = TradeStatus.WON
    else:
        pnl = -cost
        trade.status = TradeStatus.LOST

    trade.pnl = round(pnl, 4)
    trade.settlement_temp_f = actual_temp
    trade.settled_at = datetime.now(ET)

    # Fetch forecasts for post-mortem
    forecasts_result = await db.execute(
        select(WeatherForecast).where(
            WeatherForecast.city == trade.city,
            WeatherForecast.forecast_date == trade.trade_date,
        )
    )
    forecasts = list(forecasts_result.scalars().all())

    # Generate narrative
    trade.postmortem_narrative = generate_postmortem_narrative(trade, settlement, forecasts)

    await db.commit()

    logger.info(
        "Trade settled",
        extra={
            "data": {
                "trade_id": trade.id,
                "status": trade.status.value,
                "pnl": trade.pnl,
                "actual_temp_f": actual_temp,
                "bracket": bracket,
            }
        },
    )


def _did_bracket_win(bracket_label: str, actual_temp: float, side: str) -> bool:
    """Determine if a bracket/side combination won given the actual temperature.

    Bracket format: "53-54F" (2F range), or "<=52F" (bottom catch-all),
    or ">=57F" (top catch-all).
    """
    bracket_hit = False

    if bracket_label.startswith("<="):
        upper = float(bracket_label.replace("<=", "").replace("F", ""))
        bracket_hit = actual_temp <= upper
    elif bracket_label.startswith(">="):
        lower = float(bracket_label.replace(">=", "").replace("F", ""))
        bracket_hit = actual_temp >= lower
    else:
        # Standard bracket: "53-54F"
        parts = bracket_label.replace("F", "").split("-")
        lower = float(parts[0])
        upper = float(parts[1])
        bracket_hit = lower <= actual_temp <= upper

    if side == "yes":
        return bracket_hit
    else:  # "no"
        return not bracket_hit
```

---

## Push Notification Integration

Use Web Push API (VAPID keys) for real-time user notifications.

- Backend stores push subscription in database during onboarding
- Notifications sent for: trade queued (manual mode), trade executed, trade settled, daily summary, risk limit warnings

### Implementation

```python
# Can live in backend/trading/notifications.py or backend/common/notifications.py
from __future__ import annotations

import json

from pywebpush import webpush, WebPushException

from backend.common.logging import get_logger
from backend.common.config import settings

logger = get_logger("SYSTEM")


class NotificationService:
    """Sends web push notifications to the user."""

    def __init__(self, subscription: dict):
        """Initialize with the user's push subscription info.

        Args:
            subscription: Dict with keys "endpoint", "keys" (containing "p256dh" and "auth").
                          Stored in the user's record during PWA onboarding.
        """
        self.subscription = subscription

    async def send(self, title: str, body: str, data: dict | None = None) -> None:
        """Send a web push notification to the user.

        Args:
            title: Notification title (shown in notification banner).
            body: Notification body text.
            data: Optional JSON-serializable data for the PWA to process on tap.
        """
        payload = json.dumps(
            {
                "title": title,
                "body": body,
                "data": data or {},
            }
        )
        try:
            webpush(
                subscription_info=self.subscription,
                data=payload,
                vapid_private_key=settings.vapid_private_key,
                vapid_claims={"sub": f"mailto:{settings.vapid_email}"},
            )
            logger.info(
                "Push notification sent",
                extra={"data": {"title": title}},
            )
        except WebPushException as e:
            logger.error(
                "Push notification failed",
                extra={"data": {"error": str(e), "title": title}},
            )
```

### Notification Events

| Event | Title Format | Body Format |
|-------|-------------|-------------|
| Trade queued | `"+EV Trade: {city} {bracket}"` | `"EV: +${ev} \| {confidence} confidence"` |
| Trade executed | `"Trade Placed: {city} {bracket}"` | `"{side} @ {price}c — auto-executed"` |
| Trade settled (win) | `"Trade Won: {city} +${pnl}"` | `"Actual: {temp}F — bracket hit!"` |
| Trade settled (loss) | `"Trade Lost: {city} -${pnl}"` | `"Actual: {temp}F — outside bracket"` |
| Daily summary | `"Daily Summary: {date}"` | `"P&L: ${pnl} \| {wins}W-{losses}L"` |
| Risk limit warning | `"Risk Warning"` | `"Approaching daily loss limit (${current}/${limit})"` |

---

## Celery Task Integration

### Implementation (scheduler.py)

```python
# backend/trading/scheduler.py
from __future__ import annotations

import asyncio
from datetime import datetime

from celery import shared_task
from celery.schedules import crontab
from zoneinfo import ZoneInfo

from backend.common.logging import get_logger
from backend.common.database import async_session

logger = get_logger("TRADING")
ET = ZoneInfo("America/New_York")


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def trading_cycle(self):
    """Main trading loop — runs every 15 minutes via Celery Beat.

    This is the heartbeat of the trading engine.
    """
    try:
        asyncio.run(_run_trading_cycle())
    except Exception as exc:
        logger.error("Trading cycle failed", extra={"data": {"error": str(exc)}})
        raise self.retry(exc=exc)


async def _run_trading_cycle():
    """Async implementation of the trading cycle.

    Steps (in order):
    1. Check if we've entered a new trading day — reset daily limits if so
    2. Check if cooldown is active — abort if yes
    3. Check if markets are open (10 AM ET day before to ~11:59 PM ET event day)
    4. Fetch latest BracketPredictions from prediction engine (database)
    5. Validate predictions (sum to 1.0, no NaN, fresh enough)
    6. Fetch current market prices from Kalshi API
    7. Validate market prices (integers 1-99)
    8. Scan all brackets for +EV opportunities (both YES and NO sides)
    9. For each signal, run risk checks
    10. Execute (auto mode) or queue (manual mode) approved trades
    11. Log ALL decisions (including skipped trades and why)
    """
    from backend.trading.risk_manager import RiskManager, get_trading_day
    from backend.trading.ev_calculator import (
        scan_all_brackets,
        validate_predictions,
        validate_market_prices,
    )
    from backend.trading.executor import execute_trade
    from backend.trading.trade_queue import queue_trade
    from backend.trading.notifications import NotificationService

    async with async_session() as db:
        # Load user settings (single-user system for v1)
        user_settings = await _load_user_settings(db)
        risk_mgr = RiskManager(user_settings, db)

        # Step 1: Daily reset check
        await risk_mgr.handle_daily_reset()

        # Step 2: Cooldown check
        cooldown_active, reason = await risk_mgr._check_cooldown()
        if cooldown_active:
            logger.info("Trading cycle skipped: cooldown", extra={"data": {"reason": reason}})
            return

        # Step 3: Market hours check
        if not _are_markets_open():
            logger.debug("Trading cycle skipped: markets closed", extra={"data": {}})
            return

        # Step 4: Fetch predictions
        predictions = await _fetch_latest_predictions(db, user_settings.cities)
        if not predictions:
            logger.info("Trading cycle skipped: no predictions available", extra={"data": {}})
            return

        # Step 5: Validate predictions
        if not validate_predictions(predictions):
            logger.error("Trading cycle aborted: invalid predictions", extra={"data": {}})
            return

        # Step 6-7: Fetch and validate market prices
        kalshi_client = await _get_kalshi_client(db)
        for prediction in predictions:
            market_prices = await kalshi_client.get_market_prices(prediction.city, prediction.date)
            if not validate_market_prices(market_prices):
                logger.error(
                    "Skipping city: invalid market prices",
                    extra={"data": {"city": prediction.city}},
                )
                continue

            # Step 8: Scan for opportunities
            signals = scan_all_brackets(prediction, market_prices, user_settings.min_ev_threshold)
            if not signals:
                logger.debug(
                    "No +EV signals",
                    extra={"data": {"city": prediction.city}},
                )
                continue

            # Step 9-10: Risk check and execute/queue
            for signal in signals:
                allowed, risk_reason = await risk_mgr.check_trade(signal)
                if not allowed:
                    logger.info(
                        "Trade blocked by risk manager",
                        extra={
                            "data": {
                                "city": signal.city,
                                "bracket": signal.bracket.bracket_label,
                                "reason": risk_reason,
                            }
                        },
                    )
                    continue

                if user_settings.trading_mode == "auto":
                    await execute_trade(signal, kalshi_client, db)
                else:
                    notification_svc = await _get_notification_service(db)
                    await queue_trade(signal, db, notification_svc)

    logger.info(
        "Trading cycle complete",
        extra={"data": {"trading_day": str(get_trading_day())}},
    )


@shared_task
def check_pending_trades():
    """Expire stale pending trades in manual mode. Runs every 5 min."""
    asyncio.run(_expire_pending_trades())


async def _expire_pending_trades():
    """Expire pending trades past their TTL."""
    from backend.trading.trade_queue import expire_stale_trades

    async with async_session() as db:
        count = await expire_stale_trades(db)
        if count > 0:
            logger.info("Expired stale trades", extra={"data": {"count": count}})


@shared_task
def settle_trades():
    """Check for settled markets and generate post-mortems.

    Runs at 9 AM ET daily (after NWS CLI reports publish ~7-8 AM).
    """
    asyncio.run(_settle_and_postmortem())


async def _settle_and_postmortem():
    """Settle trades and generate post-mortem narratives."""
    from backend.trading.postmortem import settle_trade
    from backend.trading.cooldown import CooldownManager

    async with async_session() as db:
        # Find trades that need settlement
        from sqlalchemy import select
        from backend.common.models import Trade, Settlement, TradeStatus

        open_trades = await db.execute(select(Trade).where(Trade.status == TradeStatus.EXECUTED))

        for trade in open_trades.scalars().all():
            # Use market_date (from ticker) for settlement matching, NOT trade_date.
            # trade_date is when the order was placed (could be evening before),
            # market_date is the actual weather event date.
            settle_date = trade.market_date
            if settle_date is None:
                # Fallback: parse from ticker for pre-migration trades
                from backend.kalshi.markets import parse_market_date_from_ticker

                parsed = parse_market_date_from_ticker(trade.market_ticker)
                if parsed is not None:
                    settle_date = datetime.combine(parsed, datetime.min.time())
                else:
                    settle_date = trade.trade_date  # last resort

            settlement_result = await db.execute(
                select(Settlement).where(
                    Settlement.city == trade.city,
                    func.date(Settlement.settlement_date) == func.date(settle_date),
                )
            )
            settlement = settlement_result.scalar_one_or_none()
            if settlement is None:
                continue  # NWS CLI not published yet for this date

            await settle_trade(trade, settlement, db)

            # Update cooldown based on win/loss
            user_settings = await _load_user_settings(db)
            cm = CooldownManager(user_settings, db)
            if trade.status == TradeStatus.WON:
                await cm.on_trade_win()
            elif trade.status == TradeStatus.LOST:
                await cm.on_trade_loss()

    logger.info("Settlement cycle complete", extra={"data": {}})


def _are_markets_open() -> bool:
    """Check if Kalshi weather markets are currently tradeable.

    Markets open at 10:00 AM ET the day before the event and close
    around 11:59 PM ET on the event day. For simplicity, allow trading
    between 6:00 AM ET and 11:50 PM ET every day.
    """
    now = datetime.now(ET)
    hour = now.hour
    return 6 <= hour <= 23


# Placeholder helpers — implement based on your database/client setup
async def _load_user_settings(db) -> "UserSettings":
    """Load user settings from database."""
    ...


async def _get_kalshi_client(db) -> "KalshiClient":
    """Build authenticated Kalshi client."""
    ...


async def _get_notification_service(db) -> "NotificationService":
    """Build notification service with user's push subscription."""
    ...


# ─── Celery Beat Schedule ─────────────────────────────────────────────
# Add this to backend/common/celery_app.py

CELERY_BEAT_SCHEDULE = {
    "trading-cycle": {
        "task": "backend.trading.scheduler.trading_cycle",
        "schedule": crontab(minute="*/15"),  # Every 15 minutes
    },
    "expire-pending": {
        "task": "backend.trading.scheduler.check_pending_trades",
        "schedule": crontab(minute="*/5"),  # Every 5 minutes
    },
    "settle-trades": {
        "task": "backend.trading.scheduler.settle_trades",
        "schedule": crontab(hour=9, minute=0),  # 9 AM ET daily
    },
}
```

---

## Execution Orchestrator Summary

The main trading loop (runs as Celery task every 15 minutes):

```
every 15 minutes:
  1. Daily reset check → reset counters if new trading day
  2. Cooldown check → if active, skip entire cycle
  3. Market hours check → if closed, skip
  4. Fetch latest BracketPredictions from prediction engine
  5. Validate predictions (probabilities sum to 1.0, no NaN, fresh data)
  6. Fetch current market prices from Kalshi API
  7. Validate market prices (integers 1-99)
  8. For each bracket in each city:
     a. Calculate EV for YES and NO sides
     b. Run risk checks (size, exposure, loss limit, cooldown, EV threshold)
     c. If +EV and passes all risk checks:
        - AUTO mode: place order immediately via executor.py
        - MANUAL mode: queue trade for approval via trade_queue.py
  9. Log ALL decisions (including skipped trades and why)
```

---

## Exceptions (exceptions.py)

You can define trading-specific exceptions here, or import directly from `backend.common.exceptions`. If you add new exception types, make sure they inherit from `BozBaseException`.

```python
# backend/trading/exceptions.py
from backend.common.exceptions import BozBaseException


class TradingError(BozBaseException):
    """General trading engine error."""

    pass


class TradingHaltedError(BozBaseException):
    """Trading has been halted (e.g., invalid data, system issue)."""

    pass


# Also available from backend.common.exceptions:
# - RiskLimitError
# - CooldownActiveError
# - InvalidOrderError
# - InsufficientBalanceError
```

---

## Testing Requirements

Your tests go in `tests/trading/`:
- `test_ev_calculator.py` -- EV math correctness, both YES and NO sides, fee inclusion
- `test_risk_manager.py` -- all risk limits enforced correctly, edge cases at exact limits
- `test_cooldown.py` -- cooldown activates/deactivates correctly, consecutive loss counting, reset on win
- `test_trade_queue.py` -- state machine (PENDING->APPROVED->EXECUTED, PENDING->EXPIRED, etc.)
- `test_executor.py` -- full execution flow, auto vs manual mode routing
- `test_postmortem.py` -- post-mortem generation with correct data, narrative accuracy

### Test Patterns

```python
# All tests use pytest + pytest-asyncio
import pytest
from unittest.mock import AsyncMock, patch
from backend.trading.ev_calculator import calculate_ev, estimate_fees, scan_bracket


class TestEstimateFees:
    """Fee calculation must be exact — this is real money."""

    def test_yes_side_standard(self):
        # Buy YES at 22c, profit if win = 78c, fee = max(1, int(78*0.15)) = 11c
        assert estimate_fees(22, "yes") == 0.11

    def test_no_side_standard(self):
        # Buy NO where YES = 22c, profit if win = 22c, fee = max(1, int(22*0.15)) = 3c
        assert estimate_fees(22, "no") == 0.03

    def test_minimum_fee(self):
        # Buy YES at 95c, profit if win = 5c, fee = max(1, int(5*0.15)) = max(1, 0) = 1c
        assert estimate_fees(95, "yes") == 0.01

    def test_invalid_price_raises(self):
        with pytest.raises(ValueError):
            estimate_fees(0, "yes")
        with pytest.raises(ValueError):
            estimate_fees(100, "yes")

    def test_invalid_side_raises(self):
        with pytest.raises(ValueError):
            estimate_fees(50, "maybe")


class TestCalculateEV:
    """EV calculation is the core of all trading decisions."""

    def test_positive_ev_yes(self):
        # Model says 40%, market says 22c (22%)
        ev = calculate_ev(0.40, 22, "yes")
        # EV = (0.40 * 1.00) - 0.22 - 0.11 = +0.07
        assert ev == 0.07

    def test_negative_ev_yes(self):
        # Model says 20%, market says 22c
        ev = calculate_ev(0.20, 22, "yes")
        # EV = (0.20 * 1.00) - 0.22 - 0.11 = -0.13
        assert ev == -0.13

    def test_no_side_ev(self):
        # Model says 28% for bracket, market YES = 22c
        # NO side: prob_win = 0.72, cost = 0.78, fee = 0.03
        ev = calculate_ev(0.28, 22, "no")
        # EV = (0.72 * 1.00) - 0.78 - 0.03 = -0.09
        assert ev == -0.09

    def test_ev_symmetry(self):
        """If model agrees with market, both sides should be negative EV (due to fees)."""
        ev_yes = calculate_ev(0.50, 50, "yes")
        ev_no = calculate_ev(0.50, 50, "no")
        assert ev_yes < 0  # fees eat the edge
        assert ev_no < 0
```

### SAFETY TESTS (Critical -- in `tests/trading/test_safety.py`)

These are the most important tests in the entire project. They verify that the trading engine cannot lose more money than intended.

```python
# tests/trading/test_safety.py
"""
SAFETY TESTS — Paranoid edge case testing for the trading engine.

These tests verify that the trading engine cannot:
- Exceed position limits
- Trade during cooldown
- Trade past daily loss limits
- Send invalid orders to Kalshi
- Trade on garbage data
- Create race conditions on risk limits
"""

import pytest
import math
from unittest.mock import AsyncMock


class TestMaxPositionSafety:
    """Max position size CANNOT be exceeded under any circumstances."""

    @pytest.mark.asyncio
    async def test_trade_exactly_at_limit(self):
        """Trade at exactly max size should be allowed."""
        ...

    @pytest.mark.asyncio
    async def test_trade_one_cent_over_limit(self):
        """Trade 1 cent over max size must be blocked."""
        ...

    @pytest.mark.asyncio
    async def test_trade_with_zero_max_size(self):
        """If max_trade_size is 0, all trades must be blocked."""
        ...


class TestDailyLossLimitSafety:
    """Daily loss limit STOPS all trading when hit."""

    @pytest.mark.asyncio
    async def test_trading_halts_at_exact_limit(self):
        """When P&L == -daily_loss_limit, no more trades."""
        ...

    @pytest.mark.asyncio
    async def test_trading_halts_past_limit(self):
        """When P&L < -daily_loss_limit (rounding), no more trades."""
        ...


class TestCooldownSafety:
    """Cooldown BLOCKS trades during active cooldown."""

    @pytest.mark.asyncio
    async def test_cooldown_blocks_trade(self):
        """No trades during active cooldown period."""
        ...

    @pytest.mark.asyncio
    async def test_consecutive_loss_blocks_rest_of_day(self):
        """After N consecutive losses, trading halts until midnight ET."""
        ...


class TestInvalidOrderSafety:
    """Invalid orders NEVER reach Kalshi API."""

    @pytest.mark.asyncio
    async def test_nan_probability_halts_trading(self):
        """NaN probability must prevent all trading for the cycle."""
        ...

    @pytest.mark.asyncio
    async def test_negative_probability_halts_trading(self):
        """Negative probability must prevent all trading."""
        ...

    @pytest.mark.asyncio
    async def test_probabilities_not_summing_to_one(self):
        """If bracket probabilities sum to 0.5 or 1.5, halt trading."""
        ...

    @pytest.mark.asyncio
    async def test_stale_predictions_block_trading(self):
        """Predictions older than 2 hours must not be traded on."""
        ...

    @pytest.mark.asyncio
    async def test_zero_price_rejected(self):
        """Market price of 0 cents is invalid."""
        ...

    @pytest.mark.asyncio
    async def test_hundred_price_rejected(self):
        """Market price of 100 cents is invalid (contract already settled)."""
        ...


class TestConcurrencySafety:
    """Concurrent trade signals don't create race conditions on risk limits."""

    @pytest.mark.asyncio
    async def test_concurrent_exposure_check(self):
        """Two simultaneous trades must not both pass if their combined
        exposure exceeds the limit. Uses database-level locking."""
        ...


class TestKalshiClientFailureSafety:
    """If Kalshi client is unreachable, trades queue, don't crash."""

    @pytest.mark.asyncio
    async def test_kalshi_timeout_does_not_crash(self):
        """If Kalshi API times out, log error and continue."""
        ...

    @pytest.mark.asyncio
    async def test_kalshi_500_does_not_crash(self):
        """If Kalshi returns 500, log error and continue."""
        ...
```

---

## Build Checklist

Follow this order. Each step depends on the one before it.

1. **Create `exceptions.py`** -- `TradingError`, `TradingHaltedError` (or import `RiskLimitError`, `CooldownActiveError`, `InvalidOrderError` from `backend.common.exceptions`)

2. **Build `ev_calculator.py`** -- `estimate_fees()`, `calculate_ev()`, `scan_bracket()`, `scan_all_brackets()`, `validate_predictions()`, `validate_market_prices()`, `_generate_signal_reasoning()`

3. **Build `risk_manager.py`** -- `RiskManager` class with all checks, `get_trading_day()`, `is_new_trading_day()`, `check_and_reserve_exposure()`, daily reset logic

4. **Build `cooldown.py`** -- `CooldownManager` with `is_cooldown_active()`, `on_trade_loss()`, `on_trade_win()`, per-loss and consecutive-loss logic

5. **Build `trade_queue.py`** -- `queue_trade()`, `approve_trade()`, `reject_trade()`, `expire_stale_trades()`

6. **Build `executor.py`** -- `execute_trade()`, the main orchestrator connecting signals to Kalshi orders, partial fill handling, order verification

7. **Build `postmortem.py`** -- `generate_postmortem_narrative()`, `settle_trade()`, `_did_bracket_win()`

8. **Build `scheduler.py`** -- Celery tasks: `trading_cycle`, `check_pending_trades`, `settle_trades`, plus the `CELERY_BEAT_SCHEDULE` config

9. **Build `notifications.py`** -- `NotificationService` class with Web Push (VAPID) integration

10. **Write ALL tests in `tests/trading/`** -- `test_ev_calculator.py`, `test_risk_manager.py`, `test_cooldown.py`, `test_trade_queue.py`, `test_executor.py`, `test_postmortem.py`

11. **Write safety tests (`tests/trading/test_safety.py`)** -- Paranoid edge case testing: position limits, loss limits, cooldown enforcement, invalid data rejection, concurrency safety, API failure handling

---

## Rules for This Module

1. **All prices in cents.** The Kalshi API uses cents. Every variable holding a price must be named `*_cents` and be an `int`. Convert to dollars only for display, EV output, and P&L recording.

2. **Import from `backend.common`, not from other agents.** Use `backend.common.schemas`, `backend.common.logging`, `backend.common.config`, `backend.common.exceptions`.

3. **Async everywhere.** All database operations, HTTP calls, and Kalshi API calls use `async/await`. Use `httpx` for HTTP, SQLAlchemy 2.0 async sessions.

4. **Type everything.** All functions have type hints. All Pydantic models have field types. Use `from __future__ import annotations` at the top of every file.

5. **Log everything.** Every decision (trade, skip, block, error) gets a structured log entry. Use the correct module tags: `TRADING`, `ORDER`, `RISK`, `COOLDOWN`, `POSTMORTEM`.

6. **Never trust upstream data.** Validate predictions before trading. Validate market prices before scanning. Validate order responses before recording.

7. **Fail safe.** If in doubt, do NOT trade. A missed +EV opportunity costs nothing; a bad trade costs real money. Conservative is always better.

8. **Test real money paths paranoidly.** Every path that touches order placement or risk limit enforcement needs multiple edge-case tests.
