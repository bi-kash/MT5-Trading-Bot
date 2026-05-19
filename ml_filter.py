"""Optional ML probabilistic filter.

Loads a pre-trained scikit-learn classifier (see train_ml.py) that estimates
the probability a given signal will close profitably. Signals below
ML_MIN_PROBABILITY are filtered out.

This is intentionally lightweight: a small logistic-regression / gradient-boost
model fed with the same features the strategy uses (EMA spreads, ADX, RSI,
ATR-normalized distance, time of day). It is an *edge filter*, not a predictor.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from config import settings
from logger import get_logger

log = get_logger("ml")


class MLFilter:
    def __init__(self) -> None:
        self.model = None
        if settings.use_ml_filter:
            self._load()

    def _load(self) -> None:
        path = Path(settings.ml_model_path)
        if not path.exists():
            log.warning("ML filter enabled but model not found at %s — filter disabled", path)
            return
        try:
            import joblib
            self.model = joblib.load(path)
            log.info("ML filter loaded from %s", path)
        except Exception as exc:
            log.error("Failed to load ML model: %s", exc)
            self.model = None

    @staticmethod
    def features_from_row(row: pd.Series, side: str) -> np.ndarray:
        sign = 1.0 if side == "buy" else -1.0
        return np.array([[
            sign * (row["ema_fast"] - row["ema_slow"]) / max(row["atr"], 1e-9),
            sign * (row["close"] - row["ema_trend"]) / max(row["atr"], 1e-9),
            float(row["adx"]),
            float(row["rsi"]) - 50.0,
            row.name.hour if hasattr(row.name, "hour") else 0,
        ]], dtype=float)

    def passes(self, row: pd.Series, side: str) -> bool:
        if not settings.use_ml_filter or self.model is None:
            return True
        try:
            X = self.features_from_row(row, side)
            proba = float(self.model.predict_proba(X)[0, 1])
            ok = proba >= settings.ml_min_probability
            log.debug("ML proba=%.3f side=%s -> %s", proba, side, "PASS" if ok else "BLOCK")
            return ok
        except Exception as exc:
            log.warning("ML filter error: %s — allowing trade", exc)
            return True
