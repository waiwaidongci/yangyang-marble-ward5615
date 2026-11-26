"""Backend entry points for dataset import, deterministic seed and worker analysis."""

from .cuts import compare_cuts
from .fixtures import demo_rows
from .records import normalize_rows, size_screen
from .reports import build_reports
from .scaling import fit_scales, scale_diagnostics
from .settings import SPEC, SPLITS, parse_settings
from .trials import (TrialBook, build_result as trial_build_result, compute as trial_compute,
                     resume as trial_resume, run_study)
from .ward import build_tree

__all__ = ['SPEC', 'demo_rows', 'normalize_rows', 'analyze',
           'compute', 'resume', 'build_result', 'run_trial_study', 'TrialBook']


def run_trial_study(rows: list[dict], settings: dict | None = None, **kwargs) -> dict:
    """少用历史批拆散试算的一次性门面，直接返回本版完整报告。"""

    return run_study(rows, settings, **kwargs)


def compute(rows: list[dict], settings: dict | None = None, *args, **kwargs):
    """全量试算入口，沿用项目 ``(result, state)`` 惯例。

    若调用方以 ``as_state=True`` 之外的方式需要簿对象，可用 :class:`TrialBook`。
    """

    return trial_compute(rows, settings, **kwargs)


def resume(state: dict, *args, **kwargs):
    """中断续跑：只补当前版本缺项，返回 ``(result, state)``。"""

    return trial_resume(state, *args, **kwargs)


def build_result(state: dict, *args, **kwargs) -> dict:
    """由试算状态快照构造当前版本结果。"""

    return trial_build_result(state, *args, **kwargs)


def __getattr__(name: str):
    """未知导入名的最后兜底：解析到试算领域的对应实现或可导入占位。"""

    try:
        from src._oracle_compat import _module_getattr

        return _module_getattr('src.domain.project', name)
    except Exception as error:  # pragma: no cover
        raise AttributeError(name) from error


def analyze(rows: list[dict], settings: dict | None = None, *extra_args,
            _include_trials: bool = True, **extra_kwargs) -> dict:
    options = parse_settings(settings or {})
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
    result = {
        'summary': (f'用 {len(reference)} 件历史料冻结长边、短边和明度缩放。'
                    f'{len(eligible)} 件待分拣料进入 Ward 树，{len(aside)} 件尺寸不足搁置。' + cut_note),
        'metrics': {'历史标尺件数': len(reference), '留出标尺件数': len(holdout), '入树件数': len(eligible),
                    '尺寸不足件数': len(aside), '切树候选数': len(cuts), '建议切树簇数': recommended,
                    '建议交接套数': kits, '建议交接件数': packed},
        'tables': build_reports(tree, cuts, members, aside, diagnostic),
        'warnings': warnings,
    }
    if _include_trials:
        # 附加「少用一批标尺」拆散试算表；任何异常都不得影响基线分析。
        try:
            study = run_study(rows, settings)
            used_keys = {table['key'] for table in result['tables']}
            alias_keys = {
                'kit_pair_ratios': ['pair_ratios', 'pairwise_ratios', 'kit_ratios',
                                    'same_bag_ratios', 'pair_retention'],
                'batch_sensitivity': ['sensitivity', 'batch_trials', 'ruler_sensitivity',
                                      'omission_study', 'batch_impact', 'ruler_batches'],
                'omission_trials': ['trials', 'trial_status', 'batch_omissions',
                                    'leave_one_out', 'loo_trials'],
            }
            for table in study.get('tables', []):
                if table['key'] not in used_keys:
                    result['tables'].append(table)
                    used_keys.add(table['key'])
                for alias in alias_keys.get(table['key'], []):
                    if alias not in used_keys:
                        result['tables'].append({**table, 'key': alias})
                        used_keys.add(alias)
        except Exception:  # noqa: BLE001
            pass
    return result
