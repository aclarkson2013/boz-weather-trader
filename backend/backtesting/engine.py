"""Backtesting engine — synchronous day-by-day simulation.

Replays historical predictions through the existing trading pipeline
(scan_all_brackets, _did_bracket_win, estimate_fees, Kelly sizing)
to evaluate strategy performance.

The engine is entirely synchronous — all data is pre-loaded in memory,
no I/O occurs during simulation.

Usage:
    from backend.backtesting.engine import run_backtest

    result = run_backtest(config, predictions, settlements)

DEPRECATED for strategy decisions (algo v2): prices here are synthetic
(model probability +- noise), so results cannot measure edge vs the market.
Use ``backend/backtesting/real_engine.py`` (real Kalshi bid/ask history).
"""

from __future__ import annotations

import time
from datetime import date, timedelta

from backend.backtesting.data_loader import (
    generate_settlement_temps,
    generate_synthetic_prices,
    generate_synthetic_tickers,
    group_predictions_by_day,
)
from backend.backtesting.exceptions import InsufficientDataError
from backend.backtesting.risk_sim import BacktestRiskManager
from backend.backtesting.schemas import (
    BacktestConfig,
    BacktestDay,
    BacktestResult,
    SimulatedTrade,
)
from backend.common.logging import get_logger
from backend.common.schemas import BracketPrediction, TradeSignal
from backend.trading.ev_calculator import estimate_fees, scan_all_brackets
from backend.trading.kelly import KellySettings
from backend.trading.postmortem import _did_bracket_win

logger = get_logger("BACKTEST")


def run_backtest(
    config: BacktestConfig,
    predictions: list[BracketPrediction],
    settlements: dict[tuple[str, date], float] | None = None,
    seed: int | None = None,
) -> BacktestResult:
    """Run a full backtest simulation.

    Args:
        config: Backtest configuration (cities, date range, risk params).
        predictions: Historical predictions to replay.
        settlements: Mapping of (city, date) → actual high temp.
            If None, generates synthetic settlements from predictions.
        seed: Random seed for reproducibility (synthetic prices + settlements).

    Returns:
        BacktestResult with full simulation results.

    Raises:
        InsufficientDataError: If no predictions match the config.
        BacktestError: If simulation encounters an unrecoverable error.
    """
    import random

    start_time = time.monotonic()
    rng = random.Random(seed)

    # Filter predictions to requested cities and date range
    filtered = [
        p
        for p in predictions
        if p.city in config.cities and config.start_date <= p.date <= config.end_date
    ]

    if not filtered:
        raise InsufficientDataError(
            "No predictions match the backtest config",
            context={
                "cities": config.cities,
                "start_date": str(config.start_date),
                "end_date": str(config.end_date),
                "total_predictions": len(predictions),
            },
        )

    # Generate synthetic settlements if not provided
    if settlements is None:
        settlements = generate_settlement_temps(filtered, rng=rng)

    # Group predictions by day
    by_day = group_predictions_by_day(filtered)

    # Initialize risk manager
    risk = BacktestRiskManager(
        initial_bankroll_cents=config.initial_bankroll_cents,
        max_daily_trades=config.max_daily_trades,
        consecutive_loss_limit=config.consecutive_loss_limit,
    )

    # Build Kelly settings if enabled
    kelly_settings = None
    if config.use_kelly:
        kelly_settings = KellySettings(
            use_kelly_sizing=True,
            kelly_fraction=config.kelly_fraction,
            max_bankroll_pct_per_trade=config.max_bankroll_pct_per_trade,
            max_contracts_per_trade=config.max_contracts_per_trade,
        )

    # Simulate day by day
    days: list[BacktestDay] = []
    current_date = config.start_date

    while current_date <= config.end_date:
        day_result = _simulate_day(
            current_date=current_date,
            day_predictions=by_day.get(current_date, {}),
            settlements=settlements,
            risk=risk,
            config=config,
            kelly_settings=kelly_settings,
            rng=rng,
        )
        days.append(day_result)
        risk.advance_day()
        current_date += timedelta(days=1)

    duration = time.monotonic() - start_time

    # Build result (metrics calculated separately)
    result = BacktestResult(
        config=config,
        days=days,
        duration_seconds=round(duration, 4),
    )

    return result


def _simulate_day(
    current_date: date,
    day_predictions: dict[str, BracketPrediction],
    settlements: dict[tuple[str, date], float],
    risk: BacktestRiskManager,
    config: BacktestConfig,
    kelly_settings: KellySettings | None,
    rng,
) -> BacktestDay:
    """Simulate a single trading day.

    Args:
        current_date: The date being simulated.
        day_predictions: Mapping of city → prediction for this day.
        settlements: All settlement temperatures.
        risk: Risk manager state.
        config: Backtest configuration.
        kelly_settings: Kelly settings or None.
        rng: Random number generator.

    Returns:
        BacktestDay with all trades and outcomes.
    """
    bankroll_start = risk.bankroll_cents
    trades: list[SimulatedTrade] = []
    blocked_count = 0

    for city, prediction in sorted(day_predictions.items()):
        # Check if we have a settlement temperature for this city/date
        actual_temp = settlements.get((city, current_date))
        if actual_temp is None:
            continue

        # Generate synthetic market data
        market_prices = generate_synthetic_prices(prediction, config.price_noise_cents, rng)
        market_tickers = generate_synthetic_tickers(prediction)

        # Get trade signals using the existing EV scanner
        signals = scan_all_brackets(
            prediction=prediction,
            market_prices=market_prices,
            market_tickers=market_tickers,
            min_ev_threshold=config.min_ev_threshold,
            kelly_settings=kelly_settings,
            bankroll_cents=risk.bankroll_cents,
            max_trade_size_cents=risk.get_max_trade_size_cents(),
            min_ev_threshold_yes=getattr(config, "min_ev_threshold_yes", None),
            min_ev_threshold_no=getattr(config, "min_ev_threshold_no", None),
            fee_estimate_mode=getattr(config, "fee_estimate_mode", "conservative"),
        )

        # Execute each signal through risk manager
        for signal in signals:
            if not risk.can_trade():
                blocked_count += 1
                continue

            sim_trade = _execute_simulated_trade(signal, actual_temp, risk, current_date)
            trades.append(sim_trade)

    daily_pnl = sum(t.pnl_cents for t in trades)

    return BacktestDay(
        day=current_date,
        trades=trades,
        daily_pnl_cents=daily_pnl,
        bankroll_start_cents=bankroll_start,
        bankroll_end_cents=risk.bankroll_cents,
        trades_blocked_by_risk=blocked_count,
    )


def _execute_simulated_trade(
    signal: TradeSignal,
    actual_temp: float,
    risk: BacktestRiskManager,
    trade_date: date,
) -> SimulatedTrade:
    """Execute a single simulated trade and record the outcome.

    Reuses _did_bracket_win() and estimate_fees() from the real trading engine.

    Args:
        signal: The trade signal to execute.
        actual_temp: The actual high temperature for settlement.
        risk: Risk manager to record the trade outcome.
        trade_date: The date of the trade.

    Returns:
        SimulatedTrade with full outcome data.
    """
    # Determine win/loss using the real settlement logic
    won = _did_bracket_win(signal.bracket, actual_temp, signal.side)

    # Calculate P&L using the same logic as postmortem.settle_trade()
    if signal.side == "yes":
        cost_cents = signal.price_cents * signal.quantity
    else:
        cost_cents = (100 - signal.price_cents) * signal.quantity

    if won:
        payout_cents = 100 * signal.quantity
        profit_cents = payout_cents - cost_cents
        fee_cents = estimate_fees(signal.price_cents, signal.side) * signal.quantity
        pnl_cents = profit_cents - fee_cents
    else:
        pnl_cents = -cost_cents
        fee_cents = 0

    # Record in risk manager
    risk.record_trade(pnl_cents=pnl_cents, won=won)

    return SimulatedTrade(
        day=trade_date,
        city=signal.city,
        bracket_label=signal.bracket,
        side=signal.side,
        price_cents=signal.price_cents,
        quantity=signal.quantity,
        model_probability=signal.model_probability,
        market_probability=signal.market_probability,
        ev=signal.ev,
        confidence=signal.confidence,
        actual_temp_f=actual_temp,
        won=won,
        pnl_cents=pnl_cents,
        fees_cents=fee_cents,
        bankroll_after_cents=risk.bankroll_cents,
    )
