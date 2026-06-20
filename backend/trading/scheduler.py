"""Celery tasks for the trading engine scheduling.

These tasks are registered in the Celery beat schedule and orchestrate
the core trading cycle, pending trade expiration, and settlement.

Task schedule:
    - trading_cycle:        Every 15 minutes (scan + execute/queue trades)
    - check_pending_trades: Every 5 minutes (expire stale pending trades)
    - settle_trades:        9 AM ET daily (settle after NWS CLI published)

Uses asgiref.sync.async_to_sync for calling async functions from Celery
tasks, matching the pattern used in the weather scheduler.

Usage:
    These tasks are auto-discovered by Celery. Add the beat schedule to
    your Celery app configuration.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from asgiref.sync import async_to_sync
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded

from backend.common.database import get_task_session, reset_engine
from backend.common.logging import get_logger
from backend.common.metrics import (
    BRACKET_CAP_BLOCKED_TOTAL,
    RESTING_ORDER_SYNCED_TOTAL,
    TRADES_EXECUTED_TOTAL,
    TRADES_RISK_BLOCKED_TOTAL,
    TRADING_CYCLES_TOTAL,
)
from backend.websocket.events import publish_event_safe, publish_event_sync

logger = get_logger("TRADING")
ET = ZoneInfo("America/New_York")

# Resting orders that have been on Kalshi for longer than this are actively
# cancelled by _sync_resting_orders. Restores the auto-expiry behavior the
# old expiration_ts model provided before the v1.9.8 v2-endpoint migration
# (which uses time_in_force=good_till_canceled and so has no per-order
# expiry). 14 minutes matches the previous RESTING_EXPIRY_SECONDS in
# executor.py and stays under the 15-minute trading cycle interval so a
# stale order never overlaps with a new one for the same bracket.
RESTING_MAX_AGE_SECONDS = 14 * 60


# ─── Celery Tasks ───


@shared_task(
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    soft_time_limit=180,
    time_limit=240,
)
def trading_cycle(self) -> dict:
    """Main trading loop -- runs every 15 minutes via Celery Beat.

    This is the heartbeat of the trading engine. It scans for +EV
    opportunities and either executes (auto mode) or queues (manual mode)
    approved trades.

    Returns:
        Dict with task execution metadata.
    """
    start_time = datetime.now(UTC)

    logger.info(
        "Starting trading cycle",
        extra={"data": {}},
    )

    try:
        async_to_sync(_run_trading_cycle)()
    except SoftTimeLimitExceeded:
        elapsed = (datetime.now(UTC) - start_time).total_seconds()
        logger.warning(
            "Trading cycle hit soft time limit",
            extra={"data": {"elapsed_seconds": round(elapsed, 1)}},
        )
        TRADING_CYCLES_TOTAL.labels(outcome="timeout").inc()
        return {"status": "timeout", "elapsed_seconds": round(elapsed, 1)}
    except Exception as exc:
        logger.error(
            "Trading cycle failed, retrying",
            extra={"data": {"error": str(exc)}},
        )
        TRADING_CYCLES_TOTAL.labels(outcome="error").inc()
        raise self.retry(exc=exc) from exc

    elapsed = (datetime.now(UTC) - start_time).total_seconds()

    logger.info(
        "Trading cycle completed",
        extra={"data": {"elapsed_seconds": round(elapsed, 1)}},
    )

    TRADING_CYCLES_TOTAL.labels(outcome="completed").inc()

    return {
        "status": "completed",
        "elapsed_seconds": round(elapsed, 1),
    }


@shared_task(soft_time_limit=120, time_limit=180)
def check_pending_trades() -> dict:
    """Expire stale pending trades in manual mode.

    Runs every 5 minutes. Finds PendingTradeModel records past their
    TTL and marks them as EXPIRED.

    Returns:
        Dict with count of expired trades.
    """
    logger.info("Checking for stale pending trades", extra={"data": {}})

    try:
        count = async_to_sync(_expire_pending_trades)()
        if count > 0:
            publish_event_sync("trade.expired", {"count": count})
    except Exception as exc:
        logger.error(
            "Pending trade check failed",
            extra={"data": {"error": str(exc)}},
        )
        count = 0

    return {"status": "completed", "expired_count": count}


@shared_task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=300,
    time_limit=360,
)
def settle_trades(self) -> dict:
    """Check for settled markets and generate post-mortems.

    Runs at 9 AM ET daily (after NWS CLI reports publish ~7-8 AM).
    Finds open trades that have matching settlement data and resolves them.

    Returns:
        Dict with task execution metadata.
    """
    start_time = datetime.now(UTC)

    logger.info("Starting settlement cycle", extra={"data": {}})

    try:
        async_to_sync(_settle_and_postmortem)()
    except Exception as exc:
        logger.error(
            "Settlement cycle failed, retrying",
            extra={"data": {"error": str(exc)}},
        )
        raise self.retry(exc=exc) from exc

    elapsed = (datetime.now(UTC) - start_time).total_seconds()

    logger.info(
        "Settlement cycle completed",
        extra={"data": {"elapsed_seconds": round(elapsed, 1)}},
    )

    return {
        "status": "completed",
        "elapsed_seconds": round(elapsed, 1),
    }


# ─── Async Implementations ───


async def _run_trading_cycle() -> None:
    """Async implementation of the trading cycle.

    Steps (in order):
    1. Check if we've entered a new trading day -- reset daily limits if so
    2. Check if markets are open (6 AM - 11 PM ET)
    3. Load user settings for each active user
    4. Check cooldowns
    5. Fetch latest BracketPredictions from the database
    6. Validate predictions (sum to 1.0, no NaN, fresh enough)
    7. Fetch current market prices from Kalshi API
    8. Validate market prices (integers 1-99)
    9. Scan all brackets for +EV opportunities (both YES and NO sides)
    10. For each signal, run risk checks
    11. Execute (auto mode) or queue (manual mode) approved trades
    12. Log ALL decisions (including skipped trades and why)
    """
    from backend.trading.ev_calculator import (
        scan_all_brackets,
        validate_market_prices,
        validate_predictions,
    )
    from backend.trading.executor import execute_trade
    from backend.trading.risk_manager import RiskManager, get_trading_day
    from backend.trading.trade_queue import has_pending_duplicate, queue_trade

    # Reset the async engine so it is recreated in THIS event loop.
    # async_to_sync creates a fresh loop per Celery task invocation; the
    # singleton engine from a previous loop causes "Future attached to a
    # different loop" errors.
    reset_engine()

    # Step 0: Attempt settlement before trading. This ensures settled markets
    # are processed every 15 minutes (each cycle), not just at 9 AM ET.
    # Failures here must not block the trading cycle.
    try:
        await _settle_and_postmortem()
    except Exception as exc:
        logger.warning(
            "Settlement attempt during trading cycle failed (non-fatal)",
            extra={"data": {"error": str(exc)}},
        )

    # Always open a session for housekeeping tasks (resting sync, portfolio sync)
    # that must run even when markets are closed.
    session = await get_task_session()
    try:
        # Load user settings (placeholder -- single user for v1)
        user_settings = await _load_user_settings(session)
        if user_settings is None:
            logger.info(
                "Trading cycle skipped: no user configured",
                extra={"data": {}},
            )
            TRADING_CYCLES_TOTAL.labels(outcome="skipped").inc()
            return

        user_id = await _get_user_id(session)
        if user_id is None:
            logger.info(
                "Trading cycle skipped: no user found",
                extra={"data": {}},
            )
            TRADING_CYCLES_TOTAL.labels(outcome="skipped").inc()
            return

        # Build Kalshi client for housekeeping (sync, resting orders)
        kalshi_client = await _get_kalshi_client(session, user_id)

        # Sync resting orders BEFORE market hours check: orders placed in the
        # last cycle before market close must be synced even when markets are
        # closed.  This avoids stale RESTING trades sitting in the DB until
        # the next market-open cycle.
        if kalshi_client is not None:
            # Portfolio sync: reconcile with Kalshi positions
            try:
                from backend.trading.sync import sync_portfolio

                sync_result = await sync_portfolio(kalshi_client, session, user_id)
                if sync_result.synced_count > 0:
                    logger.info(
                        "Portfolio sync found new trades",
                        extra={
                            "data": {
                                "synced": sync_result.synced_count,
                                "skipped": sync_result.skipped_count,
                            }
                        },
                    )
                    await publish_event_safe(
                        "trade.synced",
                        {
                            "synced_count": sync_result.synced_count,
                        },
                    )
            except Exception as exc:
                logger.warning(
                    "Portfolio sync failed (non-fatal)",
                    extra={"data": {"error": str(exc)}},
                )

            # Sync resting orders: check if any RESTING trades have been filled
            # or expired on Kalshi since last cycle.
            try:
                synced = await _sync_resting_orders(session, kalshi_client, user_id)
                if synced > 0:
                    logger.info(
                        "Synced resting orders",
                        extra={"data": {"transitioned": synced}},
                    )
                    await session.commit()
            except Exception as exc:
                logger.warning(
                    "Resting order sync failed (non-fatal)",
                    extra={"data": {"error": str(exc)}},
                )

        # Step 2: Market hours check — skip trading (but housekeeping above
        # already ran).
        if not _are_markets_open():
            logger.debug(
                "Trading cycle skipped: markets closed",
                extra={"data": {}},
            )
            TRADING_CYCLES_TOTAL.labels(outcome="skipped").inc()
            return

        if kalshi_client is None:
            logger.info(
                "Trading cycle skipped: no Kalshi client available",
                extra={"data": {}},
            )
            TRADING_CYCLES_TOTAL.labels(outcome="skipped").inc()
            return

        risk_mgr = RiskManager(user_settings, session, user_id)

        # Step 1: Daily reset check
        await risk_mgr.handle_daily_reset()

        # Step 3-4: Cooldown check
        from backend.trading.cooldown import CooldownManager

        cm = CooldownManager(user_settings, session, user_id)
        cooldown_active, reason = await cm.is_cooldown_active()
        if cooldown_active:
            logger.info(
                "Trading cycle skipped: cooldown",
                extra={"data": {"reason": reason}},
            )
            TRADING_CYCLES_TOTAL.labels(outcome="skipped").inc()
            return

        # Fetch bankroll for Kelly sizing
        bankroll_cents = 0
        if user_settings.use_kelly_sizing:
            bankroll_cents = await _get_bankroll_cents(session, user_id, user_settings)

        # Fetch predictions for active cities
        predictions = await _fetch_latest_predictions(session, user_settings.active_cities)
        if not predictions:
            logger.info(
                "Trading cycle skipped: no predictions available",
                extra={"data": {}},
            )
            TRADING_CYCLES_TOTAL.labels(outcome="skipped").inc()
            return

        # Validate predictions
        if not validate_predictions(predictions):
            logger.error(
                "Trading cycle aborted: invalid predictions",
                extra={"data": {}},
            )
            TRADING_CYCLES_TOTAL.labels(outcome="skipped").inc()
            return

        # Process each city's prediction
        for prediction in predictions:
            # Fetch market prices from Kalshi
            market_prices = await _fetch_market_prices(
                kalshi_client, prediction.city, prediction.date
            )
            if not market_prices:
                logger.info(
                    "Skipping city: no market prices",
                    extra={"data": {"city": prediction.city}},
                )
                continue

            # Filter out brackets with invalid prices (e.g., zero-liquidity tails)
            market_prices = validate_market_prices(market_prices)
            if not market_prices:
                logger.error(
                    "Skipping city: no valid market prices after filtering",
                    extra={"data": {"city": prediction.city}},
                )
                continue

            # Fetch market tickers mapping
            market_tickers = await _fetch_market_tickers(
                kalshi_client, prediction.city, prediction.date
            )

            # Build KellySettings from user settings
            kelly_settings = None
            if user_settings.use_kelly_sizing:
                from backend.trading.kelly import KellySettings

                kelly_settings = KellySettings(
                    use_kelly_sizing=True,
                    kelly_fraction=user_settings.kelly_fraction,
                    max_bankroll_pct_per_trade=user_settings.max_bankroll_pct_per_trade,
                    max_contracts_per_trade=user_settings.max_contracts_per_trade,
                )

            # Build GuardrailSettings from user settings
            from backend.trading.ev_calculator import GuardrailSettings

            guardrail_settings = GuardrailSettings(
                model_weight=user_settings.model_weight,
                max_model_market_divergence=user_settings.max_model_market_divergence,
                min_market_prob_for_yes=user_settings.min_market_prob_for_yes,
            )

            # Scan for opportunities
            signals = scan_all_brackets(
                prediction,
                market_prices,
                market_tickers,
                kelly_settings=kelly_settings,
                bankroll_cents=bankroll_cents,
                max_trade_size_cents=user_settings.max_trade_size_cents,
                guardrail_settings=guardrail_settings,
                min_ev_threshold_yes=user_settings.min_ev_threshold_yes,
                min_ev_threshold_no=user_settings.min_ev_threshold_no,
                fee_estimate_mode=user_settings.fee_estimate_mode,
            )
            if not signals:
                logger.debug(
                    "No +EV signals",
                    extra={"data": {"city": prediction.city}},
                )
                continue

            # Risk check and execute/queue each signal
            for signal in signals:
                # Per-bracket position cap: check existing open contracts
                open_qty = await _get_open_bracket_qty(
                    session, user_id, signal.city, signal.bracket, prediction.date
                )
                cap = user_settings.max_contracts_per_bracket
                remaining = max(0, cap - open_qty)
                if remaining <= 0:
                    logger.info(
                        "Bracket cap reached -- skipping",
                        extra={
                            "data": {
                                "city": signal.city,
                                "bracket": signal.bracket,
                                "open_qty": open_qty,
                                "cap": cap,
                            }
                        },
                    )
                    BRACKET_CAP_BLOCKED_TOTAL.labels(city=signal.city).inc()
                    continue
                if signal.quantity > remaining:
                    logger.info(
                        "Bracket cap -- clamping quantity",
                        extra={
                            "data": {
                                "city": signal.city,
                                "bracket": signal.bracket,
                                "original_qty": signal.quantity,
                                "clamped_qty": remaining,
                                "open_qty": open_qty,
                                "cap": cap,
                            }
                        },
                    )
                    signal = signal.model_copy(update={"quantity": remaining})

                allowed, risk_reason = await risk_mgr.check_trade(signal)
                if not allowed:
                    logger.info(
                        "Trade blocked by risk manager",
                        extra={
                            "data": {
                                "city": signal.city,
                                "bracket": signal.bracket,
                                "reason": risk_reason,
                            }
                        },
                    )
                    # Truncate reason to first segment to bound label cardinality
                    short_reason = (risk_reason or "unknown").split(":")[0].strip()
                    TRADES_RISK_BLOCKED_TOTAL.labels(reason=short_reason).inc()
                    continue

                if user_settings.trading_mode == "auto":
                    try:
                        await execute_trade(signal, kalshi_client, session, user_id)
                    except Exception as trade_exc:
                        # Log and skip — don't let one failed trade roll back
                        # previously successful trades in this cycle.
                        logger.warning(
                            "Trade execution failed, skipping",
                            extra={
                                "data": {
                                    "city": signal.city,
                                    "bracket": signal.bracket,
                                    "side": signal.side,
                                    "error": str(trade_exc),
                                }
                            },
                        )
                        continue
                    TRADES_EXECUTED_TOTAL.labels(mode="auto", city=signal.city).inc()
                    await publish_event_safe(
                        "trade.executed",
                        {
                            "city": signal.city,
                            "bracket": signal.bracket,
                            "side": signal.side,
                        },
                    )
                else:
                    # Skip if there's already a PENDING trade for this bracket
                    is_dup = await has_pending_duplicate(
                        session,
                        user_id,
                        signal.city,
                        signal.bracket,
                        signal.side,
                        signal.market_ticker,
                    )
                    if is_dup:
                        logger.debug(
                            "Skipping duplicate pending trade",
                            extra={
                                "data": {
                                    "city": signal.city,
                                    "bracket": signal.bracket,
                                    "side": signal.side,
                                }
                            },
                        )
                        continue

                    notification_svc = await _get_notification_service(session, user_id)
                    await queue_trade(
                        signal,
                        session,
                        user_id,
                        signal.market_ticker,
                        notification_svc,
                    )
                    TRADES_EXECUTED_TOTAL.labels(mode="queued", city=signal.city).inc()
                    await publish_event_safe(
                        "trade.queued",
                        {
                            "city": signal.city,
                            "bracket": signal.bracket,
                            "side": signal.side,
                        },
                    )

        await session.commit()

        logger.info(
            "Trading cycle complete",
            extra={"data": {"trading_day": str(get_trading_day())}},
        )

    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def _expire_pending_trades() -> int:
    """Expire pending trades past their TTL.

    Returns:
        Number of trades expired.
    """
    from backend.trading.trade_queue import expire_stale_trades

    reset_engine()
    session = await get_task_session()
    try:
        count = await expire_stale_trades(session)
        await session.commit()
        return count
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def _settle_and_postmortem() -> None:
    """Settle trades using Kalshi's authoritative market results.

    Fetches settlement data from the Kalshi API (which side won each market),
    then settles all OPEN trades whose market_ticker appears in the results.
    NWS temperature data is fetched separately for display only.
    """
    reset_engine()

    from sqlalchemy import func, select

    from backend.common.models import Settlement, Trade, TradeStatus
    from backend.trading.cooldown import CooldownManager
    from backend.trading.postmortem import settle_from_kalshi

    session = await get_task_session()
    kalshi_client = None
    try:
        # Get user ID and create Kalshi client
        user_id = await _get_user_id(session)
        if user_id is None:
            logger.info(
                "Settlement skipped: no user configured",
                extra={"data": {}},
            )
            return

        kalshi_client = await _get_kalshi_client(session, user_id)
        if kalshi_client is None:
            logger.warning(
                "Settlement skipped: could not create Kalshi client",
                extra={"data": {}},
            )
            return

        # Fetch Kalshi settlements (authoritative win/loss source)
        kalshi_settlements = await kalshi_client.get_settlements()
        ticker_results = {s.ticker: s.market_result for s in kalshi_settlements}

        if not ticker_results:
            logger.info(
                "Settlement skipped: no Kalshi settlements available",
                extra={"data": {}},
            )
            return

        logger.info(
            "Kalshi settlements fetched",
            extra={
                "data": {
                    "total_settlements": len(ticker_results),
                    "sample_tickers": list(ticker_results.keys())[:5],
                }
            },
        )

        # Find trades that need settlement
        open_trades_result = await session.execute(
            select(Trade).where(Trade.status == TradeStatus.OPEN)
        )
        open_trades = list(open_trades_result.scalars().all())

        if open_trades:
            open_tickers = [t.market_ticker for t in open_trades]
            logger.info(
                "Open trades for settlement matching",
                extra={
                    "data": {
                        "open_count": len(open_trades),
                        "open_tickers": open_tickers,
                    }
                },
            )

        settled_count = 0
        for trade in open_trades:
            # Check if Kalshi has settled this market
            market_result = ticker_results.get(trade.market_ticker)
            if market_result is None:
                # Log unmatched trades at debug level for diagnostics
                logger.debug(
                    "No settlement match for open trade",
                    extra={
                        "data": {
                            "trade_id": trade.id[:8] if trade.id else "?",
                            "market_ticker": trade.market_ticker,
                            "in_kalshi": trade.market_ticker in ticker_results,
                        }
                    },
                )
                continue  # Market not settled on Kalshi yet

            # Optionally fetch NWS temp for display
            nws_settlement = None
            settle_date = trade.market_date or trade.trade_date
            if settle_date is not None:
                nws_result = await session.execute(
                    select(Settlement).where(
                        Settlement.city == trade.city,
                        func.date(Settlement.settlement_date) == func.date(settle_date),
                    )
                )
                nws_settlement = nws_result.scalar_one_or_none()

            await settle_from_kalshi(trade, market_result, session, nws_settlement)
            settled_count += 1

            # Update cooldown based on win/loss
            user_settings = await _load_user_settings(session)
            if user_settings is not None:
                cm = CooldownManager(user_settings, session, trade.user_id)
                if trade.status == TradeStatus.WON:
                    await cm.on_trade_win()
                elif trade.status == TradeStatus.LOST:
                    await cm.on_trade_loss()

        await session.commit()

        if settled_count > 0:
            await publish_event_safe("trade.settled", {"settled_count": settled_count})

            # Check if model retraining should be triggered
            await _check_retraining_trigger(session, settled_count)

        logger.info(
            "Settlement cycle complete",
            extra={"data": {"settled_count": settled_count}},
        )

    except Exception:
        await session.rollback()
        raise
    finally:
        if kalshi_client is not None:
            await kalshi_client.close()
        await session.close()


async def _sync_resting_orders(
    session: object,  # AsyncSession
    kalshi_client: object,
    user_id: str,
) -> int:
    """Check RESTING trades and update based on Kalshi order status.

    Called at the start of each trading cycle. Fetches all orders from Kalshi
    once, then matches against RESTING trades in the DB. Transitions:
    - Kalshi "executed" → OPEN (filled)
    - Kalshi "canceled" / not found → CANCELED (expired or manually canceled)
    - Kalshi "resting" → no change (still waiting for fill)

    Returns:
        Count of trades that transitioned out of RESTING.
    """
    from sqlalchemy import select

    from backend.common.models import Trade, TradeStatus

    resting_result = await session.execute(
        select(Trade).where(
            Trade.user_id == user_id,
            Trade.status == TradeStatus.RESTING,
        )
    )
    resting_trades = resting_result.scalars().all()

    if not resting_trades:
        return 0

    # Fetch all orders from Kalshi once and index by order_id
    try:
        all_orders = await kalshi_client.get_orders(status=None, limit=200)
    except Exception as exc:
        logger.warning(
            "Failed to fetch orders for resting sync",
            extra={"data": {"error": str(exc)}},
        )
        return 0

    orders_by_id = {o.order_id: o for o in all_orders}

    transitioned = 0
    for trade in resting_trades:
        try:
            order = orders_by_id.get(trade.kalshi_order_id)

            if order is None:
                # Order not found — likely expired on Kalshi; delete from DB
                await session.delete(trade)
                transitioned += 1
                RESTING_ORDER_SYNCED_TOTAL.labels(outcome="expired").inc()
                logger.info(
                    "Resting order expired — deleted (not found on Kalshi)",
                    extra={
                        "data": {
                            "trade_id": trade.id,
                            "order_id": trade.kalshi_order_id,
                        }
                    },
                )
                continue

            if order.status == "executed":
                # Filled! Update trade to OPEN with actual fill data
                trade.status = TradeStatus.OPEN

                # Update price to actual fill price.
                # taker_fill_cost is YES-equivalent for NO buys.
                # If available, use it; otherwise keep the price already
                # stored (which was converted at resting-order creation).
                taker_fill_cost = order.taker_fill_cost or 0
                if taker_fill_cost > 0 and order.fill_count > 0:
                    fill_price = taker_fill_cost // order.fill_count
                    if trade.side == "no":
                        fill_price = 100 - fill_price
                    trade.price_cents = fill_price

                trade.quantity = order.fill_count
                if order.taker_fees:
                    trade.fees_cents = order.taker_fees

                transitioned += 1
                RESTING_ORDER_SYNCED_TOTAL.labels(outcome="filled").inc()
                logger.info(
                    "Resting order filled",
                    extra={
                        "data": {
                            "trade_id": trade.id,
                            "order_id": trade.kalshi_order_id,
                            "fill_count": order.fill_count,
                            "fill_price_cents": trade.price_cents,
                        }
                    },
                )

            elif order.status == "canceled":
                await session.delete(trade)
                transitioned += 1
                RESTING_ORDER_SYNCED_TOTAL.labels(outcome="expired").inc()
                logger.info(
                    "Resting order canceled — deleted",
                    extra={
                        "data": {
                            "trade_id": trade.id,
                            "order_id": trade.kalshi_order_id,
                        }
                    },
                )

            else:
                # Still "resting" on Kalshi. With v2 time_in_force=
                # good_till_canceled, the order never auto-expires; we
                # must actively cancel it once it's older than the
                # configured resting window. This restores the v1.9.7
                # behavior the v1.9.8 endpoint migration regressed.
                created_at = trade.created_at
                if created_at is not None and created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)
                now = datetime.now(UTC)
                age_seconds = (now - created_at).total_seconds() if created_at else 0.0

                if age_seconds >= RESTING_MAX_AGE_SECONDS:
                    try:
                        await kalshi_client.cancel_order(trade.kalshi_order_id)
                    except Exception as cancel_exc:
                        # Don't crash the cycle on a cancel failure — the
                        # order will be retried on the next sync.
                        logger.warning(
                            "Failed to cancel stale resting order",
                            extra={
                                "data": {
                                    "trade_id": trade.id,
                                    "order_id": trade.kalshi_order_id,
                                    "age_s": round(age_seconds, 1),
                                    "error": str(cancel_exc),
                                }
                            },
                        )
                    else:
                        await session.delete(trade)
                        transitioned += 1
                        RESTING_ORDER_SYNCED_TOTAL.labels(outcome="expired").inc()
                        logger.info(
                            "Resting order cancelled — exceeded max age",
                            extra={
                                "data": {
                                    "trade_id": trade.id,
                                    "order_id": trade.kalshi_order_id,
                                    "age_s": round(age_seconds, 1),
                                }
                            },
                        )

        except Exception as exc:
            RESTING_ORDER_SYNCED_TOTAL.labels(outcome="error").inc()
            logger.warning(
                "Failed to sync resting order",
                extra={
                    "data": {
                        "trade_id": trade.id,
                        "error": str(exc),
                    }
                },
            )

    return transitioned


async def _check_retraining_trigger(
    session: object,  # AsyncSession
    settled_count: int,
) -> None:
    """Check if conditions are met to trigger model retraining post-settlement.

    Trigger conditions (any one fires):
    1. Settlement count since last training >= retrain_settlement_threshold
    2. Average Brier score across cities > retrain_brier_threshold
    3. Days since last TrainingReport > retrain_max_days

    If triggered, dispatches the train_all_models Celery task asynchronously
    with triggered_by="settlement".
    """
    from sqlalchemy import func, select

    from backend.common.config import get_settings
    from backend.common.models import Trade, TradeStatus, TrainingReport

    settings = get_settings()

    try:
        # Find the most recent training report
        last_report_result = await session.execute(
            select(TrainingReport).order_by(TrainingReport.completed_at.desc()).limit(1)
        )
        last_report = last_report_result.scalar_one_or_none()

        trigger_reason: str | None = None

        if last_report is None:
            # Never trained before — trigger on first settlement batch
            trigger_reason = "first_training"
        else:
            from datetime import datetime

            # Check 1: Settlement count since last training
            settled_since = await session.execute(
                select(func.count())
                .select_from(Trade)
                .where(
                    Trade.status.in_([TradeStatus.WON, TradeStatus.LOST]),
                    Trade.settled_at > last_report.completed_at,
                )
            )
            count_since = settled_since.scalar() or 0

            if count_since >= settings.retrain_settlement_threshold:
                trigger_reason = f"settlement_count_{count_since}"

            # Check 2: Time elapsed since last training
            if trigger_reason is None:
                now = datetime.utcnow()
                days_since = (now - last_report.completed_at).total_seconds() / 86400
                if days_since >= settings.retrain_max_days:
                    trigger_reason = f"days_elapsed_{int(days_since)}"

            # Check 3: Brier score degradation
            if trigger_reason is None:
                try:
                    from backend.prediction.calibration import check_calibration

                    scores: list[float] = []
                    for city in ["NYC", "CHI", "MIA", "AUS"]:
                        report = await check_calibration(city, session, lookback_days=90)
                        if report.status == "ok" and report.brier_score is not None:
                            scores.append(report.brier_score)
                    if scores:
                        avg_brier = sum(scores) / len(scores)
                        if avg_brier > settings.retrain_brier_threshold:
                            trigger_reason = f"brier_degradation_{avg_brier:.3f}"
                except Exception:
                    pass  # Non-fatal — skip Brier check on errors

        if trigger_reason is not None:
            logger.info(
                "Triggering model retraining post-settlement",
                extra={"data": {"reason": trigger_reason, "settled_count": settled_count}},
            )
            from backend.prediction.train_models import train_all_models

            train_all_models.delay(
                triggered_by="settlement",
                trigger_reason=trigger_reason,
            )
        else:
            logger.debug(
                "No retraining trigger conditions met",
                extra={"data": {"settled_count": settled_count}},
            )

    except Exception:
        logger.warning(
            "Failed to check retraining trigger — non-fatal",
            exc_info=True,
        )


# ─── Helper Functions ───


async def _get_open_bracket_qty(
    db,
    user_id: str,
    city: str,
    bracket_label: str,
    market_date,
) -> int:
    """Count OPEN contracts for a specific bracket on a given market date.

    Used by the per-bracket position cap to prevent re-buying the same
    bracket for the same market beyond the configured limit.

    Args:
        db: Async database session.
        user_id: The user's ID.
        city: City code (e.g., "NYC").
        bracket_label: Bracket label (e.g., "39° to 40°F").
        market_date: The market event date (date or datetime).

    Returns:
        Total quantity of OPEN contracts for this bracket/date.
    """
    from sqlalchemy import func, select

    from backend.common.models import Trade, TradeStatus

    result = await db.execute(
        select(func.coalesce(func.sum(Trade.quantity), 0)).where(
            Trade.user_id == user_id,
            Trade.city == city,
            Trade.bracket_label == bracket_label,
            Trade.status == TradeStatus.OPEN,
            func.date(Trade.market_date) == market_date,
        )
    )
    return int(result.scalar())


def _are_markets_open() -> bool:
    """Check if Kalshi weather markets are currently tradeable.

    Markets open at 10:00 AM ET the day before the event and close
    around 11:59 PM ET on the event day. For simplicity, allow trading
    between 6:00 AM ET and 11:00 PM ET every day.

    Returns:
        True if markets are open, False otherwise.
    """
    now = datetime.now(ET)
    hour = now.hour
    return 6 <= hour <= 23


async def _load_user_settings(db) -> object | None:
    """Load user settings from the first user in the database.

    This is a v1 placeholder for single-user systems. In multi-user,
    this would iterate over all active users.

    Args:
        db: Async database session.

    Returns:
        UserSettings if a user exists, None otherwise.
    """
    from sqlalchemy import select

    from backend.common.models import User
    from backend.common.schemas import UserSettings

    result = await db.execute(select(User).limit(1))
    user = result.scalar_one_or_none()
    if user is None:
        return None

    # Parse active_cities from comma-separated string
    cities = [c.strip() for c in (user.active_cities or "").split(",") if c.strip()]

    return UserSettings(
        trading_mode=user.trading_mode or "manual",
        max_trade_size_cents=user.max_trade_size_cents or 100,
        daily_loss_limit_cents=user.daily_loss_limit_cents or 1000,
        max_daily_exposure_cents=user.max_daily_exposure_cents or 2500,
        min_ev_threshold=user.min_ev_threshold or 0.05,
        min_ev_threshold_yes=user.min_ev_threshold_yes
        if user.min_ev_threshold_yes is not None
        else 0.15,
        min_ev_threshold_no=user.min_ev_threshold_no
        if user.min_ev_threshold_no is not None
        else 0.05,
        cooldown_per_loss_minutes=user.cooldown_per_loss_minutes or 60,
        consecutive_loss_limit=user.consecutive_loss_limit or 3,
        active_cities=cities or ["NYC", "CHI", "MIA", "AUS"],
        notifications_enabled=user.notifications_enabled
        if user.notifications_enabled is not None
        else True,
        use_kelly_sizing=user.use_kelly_sizing if user.use_kelly_sizing is not None else False,
        kelly_fraction=user.kelly_fraction if user.kelly_fraction is not None else 0.25,
        max_bankroll_pct_per_trade=user.max_bankroll_pct_per_trade
        if user.max_bankroll_pct_per_trade is not None
        else 0.05,
        max_contracts_per_trade=user.max_contracts_per_trade
        if user.max_contracts_per_trade is not None
        else 10,
        max_contracts_per_bracket=user.max_contracts_per_bracket
        if user.max_contracts_per_bracket is not None
        else 3,
        enable_consecutive_loss_limit=user.enable_consecutive_loss_limit
        if user.enable_consecutive_loss_limit is not None
        else True,
        model_weight=user.model_weight if user.model_weight is not None else 0.4,
        max_model_market_divergence=user.max_model_market_divergence
        if user.max_model_market_divergence is not None
        else 0.25,
        min_market_prob_for_yes=user.min_market_prob_for_yes
        if user.min_market_prob_for_yes is not None
        else 0.15,
    )


async def _get_user_id(db) -> str | None:
    """Get the first user's ID from the database.

    Args:
        db: Async database session.

    Returns:
        User ID string, or None if no users exist.
    """
    from sqlalchemy import select

    from backend.common.models import User

    result = await db.execute(select(User.id).limit(1))
    row = result.scalar_one_or_none()
    return row


async def _get_kalshi_client(db, user_id: str) -> object | None:
    """Build an authenticated Kalshi client for the given user.

    Decrypts the user's stored API credentials and creates a KalshiClient.

    Args:
        db: Async database session.
        user_id: The user ID.

    Returns:
        KalshiClient instance, or None if credentials are unavailable.
    """
    from sqlalchemy import select

    from backend.common.encryption import decrypt_api_key
    from backend.common.models import User

    try:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None:
            return None

        from backend.kalshi.client import KalshiClient

        private_key_pem = decrypt_api_key(user.encrypted_private_key)
        demo = user.demo_mode if user.demo_mode is not None else True
        return KalshiClient(
            api_key_id=user.kalshi_key_id,
            private_key_pem=private_key_pem,
            demo=demo,
        )
    except Exception as exc:
        logger.error(
            "Failed to create Kalshi client",
            extra={"data": {"error": str(exc)}},
        )
        return None


async def _get_notification_service(db, user_id: str) -> object | None:
    """Build a notification service with the user's push subscription.

    Args:
        db: Async database session.
        user_id: The user ID.

    Returns:
        NotificationService instance, or None if push is not configured.
    """
    import json

    from sqlalchemy import select

    from backend.common.models import User
    from backend.trading.notifications import NotificationService

    try:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None or not user.push_subscription:
            return None

        subscription = json.loads(user.push_subscription)
        return NotificationService(subscription=subscription)
    except Exception as exc:
        logger.error(
            "Failed to create notification service",
            extra={"data": {"error": str(exc)}},
        )
        return None


async def _get_bankroll_cents(db, user_id: str, user_settings: object) -> int:
    """Get user's available bankroll for Kelly position sizing.

    Bankroll = base exposure limit + lifetime settled P&L.
    Uses max_daily_exposure_cents as the base bankroll proxy since we
    don't track deposits/withdrawals yet.

    Args:
        db: Async database session.
        user_id: The user ID.
        user_settings: UserSettings with max_daily_exposure_cents.

    Returns:
        Bankroll in cents (minimum 100 = $1.00).
    """
    from sqlalchemy import func, select

    from backend.common.models import Trade

    result = await db.execute(
        select(func.coalesce(func.sum(Trade.pnl_cents), 0)).where(
            Trade.user_id == user_id,
            Trade.settled_at.isnot(None),
        )
    )
    lifetime_pnl = int(result.scalar())

    base = getattr(user_settings, "max_daily_exposure_cents", 2500)
    return max(base + lifetime_pnl, 100)  # Floor at $1.00


async def _fetch_latest_predictions(db, cities: list[str]) -> list:
    """Fetch the latest BracketPredictions from the database.

    Args:
        db: Async database session.
        cities: List of city codes to fetch predictions for.

    Returns:
        List of BracketPrediction schema objects.
    """
    import json

    from sqlalchemy import select

    from backend.common.models import Prediction
    from backend.common.schemas import BracketPrediction, BracketProbability

    predictions = []
    for city in cities:
        result = await db.execute(
            select(Prediction)
            .where(Prediction.city == city)
            .order_by(Prediction.generated_at.desc())
            .limit(1)
        )
        pred = result.scalar_one_or_none()
        if pred is None:
            continue

        # Parse brackets from JSON
        brackets_data = pred.brackets_json
        if isinstance(brackets_data, str):
            brackets_data = json.loads(brackets_data)

        brackets = [BracketProbability(**b) for b in brackets_data]

        # Parse model_sources from comma-separated string
        model_sources = [s.strip() for s in (pred.model_sources or "").split(",") if s.strip()]

        predictions.append(
            BracketPrediction(
                city=city,
                date=pred.prediction_date.date()
                if hasattr(pred.prediction_date, "date")
                else pred.prediction_date,
                brackets=brackets,
                ensemble_mean_f=pred.ensemble_mean_f,
                ensemble_std_f=pred.ensemble_std_f,
                confidence=pred.confidence,
                model_sources=model_sources,
                generated_at=pred.generated_at,
            )
        )

    return predictions


def best_yes_price_from_orderbook(orderbook) -> int | None:
    """Extract best (lowest) YES ask price from an orderbook.

    Falls back to deriving from best NO bid if YES side is empty.
    Each entry in orderbook.yes / orderbook.no is [price_cents, quantity].

    Args:
        orderbook: KalshiOrderbook with yes/no lists of [price, qty] pairs.

    Returns:
        Best YES price in cents, or None if no liquidity on either side.
    """
    if orderbook.yes:
        return min(level[0] for level in orderbook.yes)
    if orderbook.no:
        best_no_bid = max(level[0] for level in orderbook.no)
        return 100 - best_no_bid
    return None


async def _fetch_market_prices(kalshi_client, city: str, target_date) -> dict[str, int]:
    """Fetch current market prices from Kalshi for a city's brackets.

    Tries the Redis cache first (populated by the Kalshi WebSocket feed).
    Falls back to the REST API if the cache is empty or stale.
    When the REST market listing returns zero prices (yes_ask=0, last_price=0),
    falls back to the orderbook endpoint to derive prices from live liquidity.

    Args:
        kalshi_client: Authenticated KalshiClient.
        city: City code (e.g., "NYC").
        target_date: The event date.

    Returns:
        Dict mapping bracket label to YES price in cents.
    """
    from backend.common.metrics import KALSHI_WS_CACHE_HITS_TOTAL, ORDERBOOK_FALLBACK_TOTAL
    from backend.kalshi.markets import WEATHER_SERIES_TICKERS, parse_bracket_from_market

    try:
        series = WEATHER_SERIES_TICKERS.get(city)
        if series is None:
            return {}

        # Try Redis cache first (WebSocket feed populates this)
        try:
            from backend.kalshi.cache import get_city_prices, get_redis_client

            redis = await get_redis_client()
            try:
                cache_date_str = (
                    target_date.strftime("%y%m%d")
                    if hasattr(target_date, "strftime")
                    else str(target_date)
                )
                cached = await get_city_prices(redis, city, cache_date_str)
                if cached is not None:
                    prices, _tickers = cached
                    if prices and any(v > 0 for v in prices.values()):
                        KALSHI_WS_CACHE_HITS_TOTAL.labels(source="cache").inc()
                        logger.debug(
                            "Market prices served from cache",
                            extra={"data": {"city": city, "brackets": len(prices)}},
                        )
                        return prices
            finally:
                await redis.aclose()
        except Exception:
            pass  # Cache miss or error — fall through to REST

        # REST fallback
        KALSHI_WS_CACHE_HITS_TOTAL.labels(source="rest_fallback").inc()

        # Build event ticker
        if hasattr(target_date, "strftime"):
            date_str = target_date.strftime("%y%b%d").upper()
        else:
            date_str = str(target_date)
        event_ticker = f"{series}-{date_str}"

        markets = await kalshi_client.get_event_markets(event_ticker)

        prices: dict[str, int] = {}
        for market in markets:
            bracket_info = parse_bracket_from_market(
                {
                    "floor_strike": market.floor_strike,
                    "cap_strike": market.cap_strike,
                }
            )
            label = bracket_info["label"]
            # Use yes_ask as the market price (what you'd pay to buy YES)
            # Fast path: market listing has a nonzero price
            price = market.yes_ask if market.yes_ask > 0 else market.last_price
            if price > 0:
                prices[label] = price
                continue

            # Orderbook fallback: market listing returned zero prices
            # (Kalshi API sometimes omits yes_ask/last_price from listings)
            try:
                orderbook = await kalshi_client.get_orderbook(market.ticker)
                ob_price = best_yes_price_from_orderbook(orderbook)
                if ob_price is not None and 1 <= ob_price <= 99:
                    prices[label] = ob_price
                    ORDERBOOK_FALLBACK_TOTAL.labels(city=city).inc()
                    logger.info(
                        "Used orderbook fallback for bracket pricing",
                        extra={
                            "data": {
                                "city": city,
                                "bracket": label,
                                "ticker": market.ticker,
                                "ob_price": ob_price,
                            }
                        },
                    )
                else:
                    logger.debug(
                        "Skipping bracket: no liquidity in orderbook",
                        extra={
                            "data": {
                                "city": city,
                                "bracket": label,
                                "ticker": market.ticker,
                            }
                        },
                    )
            except Exception as ob_exc:
                logger.warning(
                    "Orderbook fallback failed for bracket",
                    extra={
                        "data": {
                            "city": city,
                            "bracket": label,
                            "ticker": market.ticker,
                            "error": str(ob_exc),
                        }
                    },
                )

        return prices
    except Exception as exc:
        logger.error(
            "Failed to fetch market prices",
            extra={"data": {"city": city, "error": str(exc)}},
        )
        return {}


async def _fetch_market_tickers(kalshi_client, city: str, target_date) -> dict[str, str]:
    """Fetch market ticker mapping from Kalshi for a city's brackets.

    Tries the Redis cache first (populated by the Kalshi WebSocket feed).
    Falls back to the REST API if the cache is empty or stale.

    Args:
        kalshi_client: Authenticated KalshiClient.
        city: City code (e.g., "NYC").
        target_date: The event date.

    Returns:
        Dict mapping bracket label to market ticker string.
    """
    from backend.common.metrics import KALSHI_WS_CACHE_HITS_TOTAL
    from backend.kalshi.markets import WEATHER_SERIES_TICKERS, parse_bracket_from_market

    try:
        series = WEATHER_SERIES_TICKERS.get(city)
        if series is None:
            return {}

        # Try Redis cache first (WebSocket feed populates this)
        try:
            from backend.kalshi.cache import get_city_prices, get_redis_client

            redis = await get_redis_client()
            try:
                cache_date_str = (
                    target_date.strftime("%y%m%d")
                    if hasattr(target_date, "strftime")
                    else str(target_date)
                )
                cached = await get_city_prices(redis, city, cache_date_str)
                if cached is not None:
                    _prices, tickers = cached
                    if tickers:
                        KALSHI_WS_CACHE_HITS_TOTAL.labels(source="cache").inc()
                        logger.debug(
                            "Market tickers served from cache",
                            extra={"data": {"city": city, "tickers": len(tickers)}},
                        )
                        return tickers
            finally:
                await redis.aclose()
        except Exception:
            pass  # Cache miss or error — fall through to REST

        # REST fallback
        KALSHI_WS_CACHE_HITS_TOTAL.labels(source="rest_fallback").inc()

        if hasattr(target_date, "strftime"):
            date_str = target_date.strftime("%y%b%d").upper()
        else:
            date_str = str(target_date)
        event_ticker = f"{series}-{date_str}"

        markets = await kalshi_client.get_event_markets(event_ticker)

        tickers: dict[str, str] = {}
        for market in markets:
            bracket_info = parse_bracket_from_market(
                {
                    "floor_strike": market.floor_strike,
                    "cap_strike": market.cap_strike,
                }
            )
            label = bracket_info["label"]
            tickers[label] = market.ticker

        return tickers
    except Exception as exc:
        logger.error(
            "Failed to fetch market tickers",
            extra={"data": {"city": city, "error": str(exc)}},
        )
        return {}


# ─── Celery Beat Schedule ───
# Add this to your Celery app configuration (e.g., backend/celery_app.py)

CELERY_BEAT_SCHEDULE = {
    "trading-cycle": {
        "task": "backend.trading.scheduler.trading_cycle",
        "schedule": 900.0,  # Every 15 minutes (crontab(minute="*/15"))
    },
    "expire-pending": {
        "task": "backend.trading.scheduler.check_pending_trades",
        "schedule": 300.0,  # Every 5 minutes (crontab(minute="*/5"))
    },
    "settle-trades": {
        "task": "backend.trading.scheduler.settle_trades",
        "schedule": 86400.0,  # Daily (crontab(hour=9, minute=0) for 9 AM ET)
    },
}
