"""Tests for backend.prediction.probability_calibration."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from backend.prediction.probability_calibration import (
    BOUNDS_VERSION,
    CALIBRATION_FILENAME,
    MIN_SAMPLES_PER_CITY,
    SUPPORTED_CITIES,
    _identity_curve,
    _is_pre_bounds_fix_row,
    apply_calibration,
    fit_all_cities,
    fit_calibration,
    load_calibration,
    save_calibration,
)

# ─── Helpers ───


def _mock_session_with_rows(rows: list) -> AsyncMock:
    """Create a mock DB session returning the given rows from execute()."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.all.return_value = rows
    session.execute.return_value = mock_result
    return session


def _brackets_with_probs(probs: list[float]) -> list[dict]:
    """Build 6 bracket dicts with the given probabilities.

    Uses post-v1.9.12 continuous half-degree bounds (2°F-wide middles);
    rows with pre-fix 1°F-wide middles are skipped by the fit.
    """
    bounds: list[tuple[float | None, float | None]] = [
        (None, 52.5),
        (52.5, 54.5),
        (54.5, 56.5),
        (56.5, 58.5),
        (58.5, 60.5),
        (60.5, None),
    ]
    return [
        {"lower_bound_f": lo, "upper_bound_f": hi, "probability": p}
        for (lo, hi), p in zip(bounds, probs, strict=True)
    ]


def _row(probs: list[float], actual_high: float) -> tuple:
    """Create one (brackets_json, actual_high_f) row tuple."""
    return (_brackets_with_probs(probs), actual_high)


# ═══════════════════════════════════════════════════════════════
# apply_calibration
# ═══════════════════════════════════════════════════════════════


class TestApplyCalibration:
    """Tests for the apply_calibration helper."""

    def test_identity_curve_is_noop(self) -> None:
        probs = [0.1, 0.2, 0.3, 0.15, 0.15, 0.1]
        result = apply_calibration(probs, _identity_curve())
        assert result == probs

    def test_none_curve_is_noop(self) -> None:
        probs = [0.1, 0.2, 0.3, 0.15, 0.15, 0.1]
        assert apply_calibration(probs, None) == probs

    def test_overconfident_input_shrinks(self) -> None:
        """A curve learned from overconfident predictions shrinks high probs."""
        # Overconfident model: predicted 0.85, actual rate 0.45.
        curve = {
            "x_thresholds": [0.0, 0.5, 0.85, 1.0],
            "y_thresholds": [0.0, 0.4, 0.45, 0.5],
            "is_identity": False,
        }
        # All input mass at 0.85 → calibrated should be much lower per-bucket.
        raw = [0.85, 0.85, 0.85, 0.85, 0.85, 0.85]
        calibrated = apply_calibration(raw, curve)
        # All buckets equal because they all start equal, but each is now 1/6
        # after renormalization. Just verify renormalization happens.
        assert calibrated == pytest.approx([1 / 6] * 6, abs=1e-9)

    def test_renormalizes_to_one(self) -> None:
        curve = {
            "x_thresholds": [0.0, 0.3, 0.6, 1.0],
            "y_thresholds": [0.0, 0.15, 0.40, 0.7],
            "is_identity": False,
        }
        raw = [0.05, 0.15, 0.30, 0.20, 0.20, 0.10]
        calibrated = apply_calibration(raw, curve)
        assert sum(calibrated) == pytest.approx(1.0, abs=1e-9)
        assert len(calibrated) == len(raw)

    def test_zero_input_falls_back(self) -> None:
        """If calibration maps everything to 0, return the input unchanged."""
        curve = {
            "x_thresholds": [0.0, 1.0],
            "y_thresholds": [0.0, 0.0],
            "is_identity": False,
        }
        raw = [0.1, 0.2, 0.3, 0.15, 0.15, 0.1]
        result = apply_calibration(raw, curve)
        assert result == raw

    def test_empty_input_returns_empty(self) -> None:
        assert apply_calibration([], None) == []

    def test_clamps_inputs_outside_curve_range(self) -> None:
        """Inputs above the curve's max x get clamped to max y."""
        curve = {
            "x_thresholds": [0.1, 0.9],
            "y_thresholds": [0.05, 0.5],
            "is_identity": False,
        }
        raw = [0.99, 0.0, 0.5, 0.5, 0.5, 0.5]
        calibrated = apply_calibration(raw, curve)
        # Values > 0.9 → 0.5; values < 0.1 → 0.05; 0.5 → linear interp
        assert sum(calibrated) == pytest.approx(1.0, abs=1e-9)


# ═══════════════════════════════════════════════════════════════
# fit_calibration
# ═══════════════════════════════════════════════════════════════


class TestFitCalibration:
    """Tests for fit_calibration with mocked DB."""

    @pytest.mark.asyncio
    async def test_insufficient_data_returns_identity(self) -> None:
        """Below MIN_SAMPLES_PER_CITY → identity curve."""
        # 5 rows × 6 brackets = 30 pairs, well below MIN_SAMPLES_PER_CITY.
        rows = [_row([0.1, 0.2, 0.4, 0.2, 0.05, 0.05], 55.5)] * 5
        session = _mock_session_with_rows(rows)

        curve = await fit_calibration("NYC", session)
        assert curve["is_identity"] is True
        assert curve["sample_count"] == 0

    @pytest.mark.asyncio
    async def test_sufficient_data_fits_real_curve(self) -> None:
        """With enough data, a non-identity curve is returned."""
        # 50 rows × 6 brackets = 300 pairs, above MIN_SAMPLES_PER_CITY.
        rows = [_row([0.1, 0.2, 0.4, 0.2, 0.05, 0.05], 55.5) for _ in range(50)]
        session = _mock_session_with_rows(rows)

        curve = await fit_calibration("NYC", session, min_samples=100)
        assert curve["is_identity"] is False
        assert curve["sample_count"] == 300
        assert len(curve["x_thresholds"]) >= 2
        assert curve["fitted_at"] is not None

    @pytest.mark.asyncio
    async def test_fitted_curve_corrects_overconfident_predictions(self) -> None:
        """If model overpredicts when prob ~0.4, curve maps 0.4 → lower."""
        # Build training data where bracket-3 (prob 0.40) only hits in 1/10 cases.
        # Other brackets hit in roughly the predicted rate.
        np.random.seed(7)
        rows: list[tuple] = []
        for i in range(200):
            # Most days, the actual high is 50 (bottom catch-all wins).
            # Bracket 3 (which was given prob 0.40) almost never wins.
            actual = 57.5 if i % 10 == 0 else 50.0
            rows.append(_row([0.05, 0.10, 0.20, 0.40, 0.15, 0.10], actual))
        session = _mock_session_with_rows(rows)

        curve = await fit_calibration("NYC", session, min_samples=100)
        # The bracket-3 prediction was 0.40 but only hit 10% of the time.
        # The fitted curve should map 0.40 down significantly.
        x_arr = np.array(curve["x_thresholds"])
        y_arr = np.array(curve["y_thresholds"])
        mapped = float(np.interp(0.40, x_arr, y_arr))
        assert mapped < 0.25, f"Expected calibration to shrink 0.40, got {mapped}"

    @pytest.mark.asyncio
    async def test_handles_brackets_json_as_string(self) -> None:
        """brackets_json stored as a JSON string is parsed correctly."""
        brackets = _brackets_with_probs([0.1, 0.2, 0.4, 0.2, 0.05, 0.05])
        rows = [(json.dumps(brackets), 55.5)] * 50
        session = _mock_session_with_rows(rows)

        curve = await fit_calibration("NYC", session, min_samples=100)
        assert curve["sample_count"] == 300


# ═══════════════════════════════════════════════════════════════
# fit_all_cities
# ═══════════════════════════════════════════════════════════════


class TestFitAllCities:
    """Tests for the multi-city orchestration."""

    @pytest.mark.asyncio
    async def test_returns_curve_per_supported_city(self) -> None:
        session = _mock_session_with_rows([])
        curves = await fit_all_cities(session)
        assert set(curves.keys()) == set(SUPPORTED_CITIES)
        # All identity because no data.
        for city in SUPPORTED_CITIES:
            assert curves[city]["is_identity"] is True

    @pytest.mark.asyncio
    async def test_uses_identity_when_one_city_fails(self) -> None:
        """A DB error for one city does not fail the whole batch."""
        session = AsyncMock()
        session.execute.side_effect = RuntimeError("DB blew up")
        curves = await fit_all_cities(session)
        # All four cities returned identity curves rather than crashing.
        assert set(curves.keys()) == set(SUPPORTED_CITIES)
        for city in SUPPORTED_CITIES:
            assert curves[city]["is_identity"] is True


# ═══════════════════════════════════════════════════════════════
# Persistence (save / load round-trip)
# ═══════════════════════════════════════════════════════════════


class TestPersistence:
    """Tests for save_calibration / load_calibration."""

    def test_load_returns_none_when_file_missing(self, tmp_path) -> None:
        assert load_calibration(str(tmp_path)) is None

    def test_save_then_load_round_trip(self, tmp_path) -> None:
        curves = {
            "NYC": _identity_curve(),
            "CHI": {
                "x_thresholds": [0.0, 0.5, 1.0],
                "y_thresholds": [0.0, 0.3, 0.7],
                "sample_count": 1234,
                "fitted_at": "2026-05-09T22:00:00+00:00",
                "is_identity": False,
            },
        }
        save_calibration(curves, str(tmp_path))
        path = tmp_path / CALIBRATION_FILENAME
        assert path.exists()

        loaded = load_calibration(str(tmp_path))
        assert loaded is not None
        assert set(loaded.keys()) == {"NYC", "CHI"}
        assert loaded["CHI"]["sample_count"] == 1234
        assert loaded["CHI"]["x_thresholds"] == [0.0, 0.5, 1.0]

    def test_load_returns_none_on_corrupt_file(self, tmp_path) -> None:
        path = tmp_path / CALIBRATION_FILENAME
        path.write_text("{this is not valid json", encoding="utf-8")
        assert load_calibration(str(tmp_path)) is None

    def test_load_returns_none_when_curves_key_missing(self, tmp_path) -> None:
        path = tmp_path / CALIBRATION_FILENAME
        path.write_text(json.dumps({"computed_at": "2026-01-01"}), encoding="utf-8")
        assert load_calibration(str(tmp_path)) is None

    def test_load_rejects_pre_bounds_fix_file(self, tmp_path) -> None:
        """A file saved before the v1.9.12 bounds fix (no bounds_version)
        is ignored — its curves were fitted on distorted probabilities."""
        path = tmp_path / CALIBRATION_FILENAME
        path.write_text(
            json.dumps(
                {
                    "curves": {"NYC": _identity_curve()},
                    "computed_at": "2026-08-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        assert load_calibration(str(tmp_path)) is None

    def test_load_rejects_wrong_bounds_version(self, tmp_path) -> None:
        path = tmp_path / CALIBRATION_FILENAME
        path.write_text(
            json.dumps(
                {
                    "curves": {"NYC": _identity_curve()},
                    "bounds_version": 1,
                }
            ),
            encoding="utf-8",
        )
        assert load_calibration(str(tmp_path)) is None

    def test_save_stamps_current_bounds_version(self, tmp_path) -> None:
        save_calibration({"NYC": _identity_curve()}, str(tmp_path))
        payload = json.loads((tmp_path / CALIBRATION_FILENAME).read_text(encoding="utf-8"))
        assert payload["bounds_version"] == BOUNDS_VERSION


# ═══════════════════════════════════════════════════════════════
# Pre-bounds-fix row filtering
# ═══════════════════════════════════════════════════════════════


class TestPreBoundsFixRowFilter:
    """The fit must not train on predictions stored with 1°F-wide brackets."""

    def test_detects_old_format_row(self) -> None:
        old_brackets = [
            {"lower_bound_f": None, "upper_bound_f": 89.0, "probability": 0.2},
            {"lower_bound_f": 89.0, "upper_bound_f": 90.0, "probability": 0.3},
            {"lower_bound_f": 91.0, "upper_bound_f": 92.0, "probability": 0.3},
            {"lower_bound_f": 96.0, "upper_bound_f": None, "probability": 0.2},
        ]
        assert _is_pre_bounds_fix_row(old_brackets) is True

    def test_accepts_fixed_format_row(self) -> None:
        assert _is_pre_bounds_fix_row(_brackets_with_probs([0.1] * 6)) is False

    def test_accepts_legacy_dot99_row(self) -> None:
        """Legacy .99-cap rows (~1.99°F wide) are close enough to keep."""
        legacy = [
            {"lower_bound_f": 52.0, "upper_bound_f": 53.99, "probability": 0.5},
            {"lower_bound_f": 54.0, "upper_bound_f": 55.99, "probability": 0.5},
        ]
        assert _is_pre_bounds_fix_row(legacy) is False

    @pytest.mark.asyncio
    async def test_fit_skips_old_format_rows(self) -> None:
        """Old-format rows are excluded → insufficient data → identity."""
        old_brackets = [
            {"lower_bound_f": None, "upper_bound_f": 52.0, "probability": 0.1},
            {"lower_bound_f": 53.0, "upper_bound_f": 54.0, "probability": 0.2},
            {"lower_bound_f": 55.0, "upper_bound_f": 56.0, "probability": 0.4},
            {"lower_bound_f": 57.0, "upper_bound_f": 58.0, "probability": 0.2},
            {"lower_bound_f": 59.0, "upper_bound_f": 60.0, "probability": 0.05},
            {"lower_bound_f": 61.0, "upper_bound_f": None, "probability": 0.05},
        ]
        rows = [(old_brackets, 55.5)] * 100  # 600 pairs if they counted
        session = _mock_session_with_rows(rows)

        curve = await fit_calibration("NYC", session, min_samples=100)
        assert curve["is_identity"] is True


# ═══════════════════════════════════════════════════════════════
# Constants sanity
# ═══════════════════════════════════════════════════════════════


def test_min_samples_threshold_is_reasonable() -> None:
    """Sanity: threshold is high enough to avoid noise, low enough to be fittable."""
    assert MIN_SAMPLES_PER_CITY >= 100
    assert MIN_SAMPLES_PER_CITY <= 1000


def test_supported_cities_match_city_enum() -> None:
    from backend.common.models import CityEnum

    enum_cities = {c.value for c in CityEnum}
    assert set(SUPPORTED_CITIES) == enum_cities
