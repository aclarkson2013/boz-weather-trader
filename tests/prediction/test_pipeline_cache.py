"""Tests for the pipeline's cross-process model-cache invalidation.

Celery runs a prefork pool, so ``pipeline.reload_models()`` after a retrain
only clears globals in the child that ran the task. Sibling children used to
keep serving whatever they cached at startup until the container restarted —
that is what silently disabled probability calibration for most of Era E
(see docs/ALGO_CHANGELOG.md, 2026-08-21 review, "ROOT CAUSE A").

These tests pin the mtime-based reload that fixes it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.prediction import pipeline
from backend.prediction.probability_calibration import (
    BOUNDS_VERSION,
    CALIBRATION_FILENAME,
    load_calibration,
)
from backend.prediction.source_weights import SOURCE_WEIGHTS_FILENAME


@pytest.fixture(autouse=True)
def _reset_pipeline_cache():
    """Ensure each test starts and ends with a clean module cache."""
    pipeline.reload_models()
    yield
    pipeline.reload_models()


def _write_calibration(model_dir: Path, y_first: float) -> None:
    """Write a valid calibration file whose curve is identifiable by y_first."""
    payload = {
        "bounds_version": BOUNDS_VERSION,
        "computed_at": "2026-08-21T00:00:00+00:00",
        "curves": {
            "NYC": {
                "x_thresholds": [0.0, 1.0],
                "y_thresholds": [y_first, 1.0],
                "sample_count": 500,
                "fitted_at": "2026-08-21T00:00:00+00:00",
                "is_identity": False,
            }
        },
    }
    (model_dir / CALIBRATION_FILENAME).write_text(json.dumps(payload), encoding="utf-8")


def test_calibration_reloads_when_file_changes(tmp_path: Path) -> None:
    """A refit written by another process is picked up without reload_models()."""
    _write_calibration(tmp_path, y_first=0.10)

    with patch(
        "backend.prediction.pipeline.get_settings",
        return_value=MagicMock(xgb_model_dir=str(tmp_path)),
    ):
        first = pipeline._get_calibration_curves()
        assert first is not None
        assert first["NYC"]["y_thresholds"][0] == 0.10

        # Simulate a sibling Celery child refitting and rewriting the file.
        # os.utime keeps the test independent of filesystem mtime resolution.
        _write_calibration(tmp_path, y_first=0.42)
        path = tmp_path / CALIBRATION_FILENAME
        os.utime(path, (1_800_000_000, 1_800_000_000))

        second = pipeline._get_calibration_curves()

    assert second is not None
    assert second["NYC"]["y_thresholds"][0] == 0.42, (
        "cache did not refresh after the calibration file changed on disk"
    )


def test_calibration_recovers_from_missing_then_written(tmp_path: Path) -> None:
    """The exact Era E failure: no file at boot, file appears after a refit."""
    with patch(
        "backend.prediction.pipeline.get_settings",
        return_value=MagicMock(xgb_model_dir=str(tmp_path)),
    ):
        # Boot with no calibration file → identity (None).
        assert pipeline._get_calibration_curves() is None

        # A later refit writes real curves.
        _write_calibration(tmp_path, y_first=0.33)

        curves = pipeline._get_calibration_curves()

    assert curves is not None, "stale 'no calibration' cache survived a refit"
    assert curves["NYC"]["y_thresholds"][0] == 0.33


def test_calibration_cached_when_file_unchanged(tmp_path: Path) -> None:
    """Unchanged file → no repeated disk reads (the cache still caches)."""
    _write_calibration(tmp_path, y_first=0.10)

    with (
        patch(
            "backend.prediction.pipeline.get_settings",
            return_value=MagicMock(xgb_model_dir=str(tmp_path)),
        ),
        patch(
            "backend.prediction.probability_calibration.load_calibration",
            wraps=load_calibration,
        ) as spy,
    ):
        pipeline._get_calibration_curves()
        pipeline._get_calibration_curves()
        pipeline._get_calibration_curves()

    assert spy.call_count == 1


def test_source_weights_reload_when_file_changes(tmp_path: Path) -> None:
    """Source weights follow the same mtime-based refresh."""
    path = tmp_path / SOURCE_WEIGHTS_FILENAME
    path.write_text(json.dumps({"weights": {"NWS": 1.0}}), encoding="utf-8")

    with patch(
        "backend.prediction.pipeline.get_settings",
        return_value=MagicMock(xgb_model_dir=str(tmp_path)),
    ):
        assert pipeline._get_source_weights() == {"NWS": 1.0}

        path.write_text(json.dumps({"weights": {"Open-Meteo:ICON": 1.0}}), encoding="utf-8")
        os.utime(path, (1_800_000_000, 1_800_000_000))

        assert pipeline._get_source_weights() == {"Open-Meteo:ICON": 1.0}


def test_file_mtime_returns_sentinel_for_missing_file(tmp_path: Path) -> None:
    """A missing artifact yields the -1.0 sentinel rather than raising."""
    assert pipeline._file_mtime(str(tmp_path), "does_not_exist.json") == -1.0
