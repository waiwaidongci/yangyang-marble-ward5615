"""Validate physical measurements and identify pieces unfit for this kit size."""

import re
from collections import Counter
from math import isfinite

from .settings import FEATURES, FIELDS, MAX_PENDING, SPLITS

CODE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,39}')


def measurement(value, lower: float, upper: float) -> float:
    if isinstance(value, bool):
        raise ValueError('需要数字读数')
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError('需要数字读数') from error
    if not isfinite(number) or not lower <= number <= upper:
        raise ValueError(f'读数须在 {lower} 至 {upper} 之间')
    return number if number else 0.0


def normalize_rows(rows: list[dict]) -> list[dict]:
    if not 1 <= len(rows) <= 5000:
        raise ValueError('台账须包含 1 至 5000 条记录。')
    pieces = {}
    counts = Counter()
    for number, raw in enumerate(rows, 2):
        try:
            if not isinstance(raw, dict) or raw.keys() != FIELDS.keys():
                raise ValueError('列须与边角料台账模板一致')
            row = {key: str(raw[key]).strip() if raw[key] is not None else '' for key in ('id', 'lot_id', 'split')}
            for name in ('id', 'lot_id'):
                if not CODE.fullmatch(row[name]):
                    raise ValueError(f'{FIELDS[name]}须为 1 至 40 位字母、数字、短横线或下划线并以字母或数字开头')
            if row['id'] in pieces:
                raise ValueError(f"边角料编号 {row['id']} 重复")
            if row['split'] not in SPLITS:
                raise ValueError('用途须为 train、holdout 或 pending')
            for field in FEATURES:
                lower, upper = (0, 100) if field == 'tone_lstar' else (1, 2000)
                try:
                    row[field] = measurement(raw[field], lower, upper)
                except ValueError as error:
                    raise ValueError(f'{FIELDS[field]}{error}') from error
            if row['short_mm'] > row['long_mm']:
                raise ValueError('短边须小于或等于长边')
        except ValueError as error:
            raise ValueError(f'CSV 第 {number} 行：{error}。') from error
        pieces[row['id']] = row
        counts[row['split']] += 1
    if counts['train'] < 3:
        raise ValueError('至少需要 3 件历史标尺料（train）。')
    if not 1 <= counts['pending'] <= MAX_PENDING:
        raise ValueError(f'待分拣料（pending）须有 1 至 {MAX_PENDING} 件。')
    return [pieces[key] for key in sorted(pieces)]


def size_screen(rows: list[dict], options: dict) -> tuple[list[dict], list[dict]]:
    eligible = []
    aside = []
    for row in rows:
        failures = [key for key in ('long_mm', 'short_mm') if row[key] < options[f'min_{key}']]
        if failures:
            aside.append({**row, 'min_long_mm': options['min_long_mm'], 'min_short_mm': options['min_short_mm'],
                          'reason': '+'.join(f'below_min_{key}' for key in failures)})
        else:
            eligible.append(row)
    return eligible, aside
