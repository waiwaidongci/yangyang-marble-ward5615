"""Backend entry points for dataset import, deterministic seed and worker analysis."""

from .cuts import compare_cuts
from .fixtures import demo_rows
from .records import normalize_rows, size_screen
from .reports import build_reports
from .scaling import fit_scales, scale_diagnostics
from .settings import SPEC, SPLITS, parse_settings
from .ward import build_tree

__all__ = ['SPEC', 'demo_rows', 'normalize_rows', 'analyze']


def analyze(rows: list[dict], settings: dict) -> dict:
    options = parse_settings(settings)
    cohorts = {split: [] for split in SPLITS}
    for row in normalize_rows(rows):
        cohorts[row['split']].append(row)
    reference = cohorts['train']
    holdout = cohorts['holdout']
    scales = fit_scales(reference, options['tone_weight'])
    diagnostic = scale_diagnostics(scales, len(reference), holdout)
    eligible, aside = size_screen(cohorts['pending'], options)
    tree = build_tree({row['id']: tuple(scale.apply(row) for scale in scales) for row in eligible})
    cuts, members, recommended = compare_cuts(tree, options, eligible)
    warnings = []
    if not holdout:
        warnings.append('台账没有留出标尺，无法检查标尺批之间的均值偏移。')
    if not eligible:
        warnings.append('全部待分拣单件低于尺寸门槛，本次无可切树数据。')
    elif len(eligible) < options['max_clusters']:
        warnings.append(f'符合尺寸的边角料仅 {len(eligible)} 件，候选簇数上限按实际件数收缩。')
    if cuts and recommended is None:
        warnings.append('没有候选同时满足套件件数与合并距离门槛，所有入树单件暂缓交接。')
    chosen = next((cut for cut in cuts if cut['recommended']), None)
    kits = chosen['kits_ready'] if chosen else 0
    packed = chosen['pieces_ready'] if chosen else 0
    cut_note = f'建议切成 {recommended} 簇，可交接 {kits} 套共 {packed} 件。' if chosen else '本次暂无可交接套件。'
    return {
        'summary': (f'用 {len(reference)} 件历史料冻结长边、短边和明度缩放。'
                    f'{len(eligible)} 件待分拣料进入 Ward 树，{len(aside)} 件尺寸不足搁置。' + cut_note),
        'metrics': {'历史标尺件数': len(reference), '留出标尺件数': len(holdout), '入树件数': len(eligible),
                    '尺寸不足件数': len(aside), '切树候选数': len(cuts), '建议切树簇数': recommended,
                    '建议交接套数': kits, '建议交接件数': packed},
        'tables': build_reports(tree, cuts, members, aside, diagnostic),
        'warnings': warnings,
    }
