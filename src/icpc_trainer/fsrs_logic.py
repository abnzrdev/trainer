from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum


class Rating(IntEnum):
    AGAIN = 1
    HARD = 2
    GOOD = 3
    EASY = 4


@dataclass(slots=True)
class FSRSReviewState:
    stability: float
    difficulty: float
    last_reviewed: datetime


@dataclass(slots=True)
class FSRSUpdateResult:
    stability: float
    difficulty: float
    last_reviewed: datetime
    next_review_date: datetime


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _retrievability(stability: float, elapsed_days: float) -> float:
    safe_stability = max(stability, 0.1)
    safe_days = max(elapsed_days, 0.0)
    return (1.0 + (safe_days / (9.0 * safe_stability))) ** -1


def calculate_next_review(
    last_state: FSRSReviewState,
    rating: int,
    elapsed_days: float,
) -> FSRSUpdateResult:
    rating_value = Rating(rating)
    now = datetime.utcnow()

    previous_stability = max(last_state.stability, 0.1)
    previous_difficulty = _clamp(last_state.difficulty, 1.0, 10.0)
    retrievability = _retrievability(previous_stability, elapsed_days)

    if rating_value == Rating.AGAIN:
        new_difficulty = _clamp(previous_difficulty + 0.6 * (1.0 - retrievability), 1.0, 10.0)
        new_stability = max(0.1, previous_stability * (0.35 + 0.25 * retrievability))
        next_interval_days = 1
    else:
        if rating_value == Rating.HARD:
            difficulty_delta = 0.15
            recall_bonus = 0.9
            interval_multiplier = 1.3
        elif rating_value == Rating.GOOD:
            difficulty_delta = -0.1
            recall_bonus = 1.2
            interval_multiplier = 2.4
        else:
            difficulty_delta = -0.3
            recall_bonus = 1.5
            interval_multiplier = 3.2

        new_difficulty = _clamp(previous_difficulty + difficulty_delta, 1.0, 10.0)

        growth = 1.0 + (11.0 - new_difficulty) * 0.04 * (1.0 - retrievability) * recall_bonus
        new_stability = max(0.1, previous_stability * growth)

        next_interval_days = max(1, int(round(new_stability * interval_multiplier)))

    next_review_date = now + timedelta(days=next_interval_days)

    return FSRSUpdateResult(
        stability=new_stability,
        difficulty=new_difficulty,
        last_reviewed=now,
        next_review_date=next_review_date,
    )
