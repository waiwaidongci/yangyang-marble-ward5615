"""Freeze population scales from the historical reference cohort."""

from dataclasses import dataclass
from statistics import fmean, pstdev

from .settings import FEATURES

SCALE_FLOOR = 1.0


@dataclass(frozen=True)
class Scale:
    feature: str
    center: float
    population_sd: float
    effective_scale: float
    weight: float

    def apply(self, row: dict) -> float:
        return (row[self.feature] - self.center) * self.weight / self.effective_scale


def fit_scales(reference: list[dict], tone_weight: float) -> tuple[Scale, ...]:
    scales = []
    for feature in FEATURES:
        values = [row[feature] for row in reference]
        sd = pstdev(values)
        weight = tone_weight if feature == 'tone_lstar' else 1.0
        scales.append(Scale(feature, fmean(values), sd, max(SCALE_FLOOR, sd), weight))
    return tuple(scales)


def scale_diagnostics(scales: tuple[Scale, ...], reference_count: int, holdout: list[dict]) -> list[dict]:
    diagnostics = []
    for scale in scales:
        mean = fmean(row[scale.feature] for row in holdout) if holdout else None
        shift = None if mean is None else (mean - scale.center) / scale.effective_scale
        diagnostics.append({'feature': scale.feature, 'reference_count': reference_count,
                            'reference_mean': scale.center, 'reference_population_sd': scale.population_sd,
                            'scale_floor': SCALE_FLOOR, 'effective_scale': scale.effective_scale,
                            'coordinate_weight': scale.weight, 'holdout_count': len(holdout),
                            'holdout_mean': mean, 'holdout_shift_sd': shift})
    return diagnostics
