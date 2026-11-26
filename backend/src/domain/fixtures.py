"""Reproducible shift ledger with separate reference, check and sorting lots."""

from random import Random

SEED = 261107
BANDS = ((103, 66, 80), (150, 92, 63), (198, 123, 43))


def demo_rows() -> list[dict]:
    rng = Random(SEED)
    rows = []
    for split, prefix, count in (('train', 'REF', 6), ('holdout', 'CHK', 2), ('pending', 'NOV25', 7)):
        for band, (length, width, tone) in enumerate(BANDS, 1):
            lot = f'{prefix}-{band:02}'
            drift = 1.2 if split == 'holdout' else 0.0
            for piece in range(1, count + 1):
                rows.append({'id': f'{lot}-{piece:02}', 'lot_id': lot, 'split': split,
                             'long_mm': round(length + rng.uniform(-5, 5) + drift, 2),
                             'short_mm': round(width + rng.uniform(-4, 4), 2),
                             'tone_lstar': round(tone + rng.uniform(-2.5, 2.5) + drift, 2)})
    for number, (length, width, tone) in enumerate(((118, 75, 96), (204, 129, 83), (82, 58, 80), (105, 53, 76)), 1):
        rows.append({'id': f'NOV25-MIX-{number:02}', 'lot_id': 'NOV25-MIX', 'split': 'pending',
                     'long_mm': float(length), 'short_mm': float(width), 'tone_lstar': float(tone)})
    return rows
