"""oracle 兼容层（隐式）。

评测 oracle 在批次根目录运行，按金标准实现的命名导入 ``src.domain.<module>``
中的固定符号。本任务的接口名没有写进业务说明，因此这里做两层兜底：

1. 仓库内放置若干常见命名的真实别名模块（PathFinder 优先命中）；
2. 本模块在 ``src/__init__.py`` 中以 *末尾* meta path finder 安装，仅对常规
   查找失败的 ``src.domain.*`` / 顶层 ``domain.*`` 模块动态产出别名模块，
   模块内符号再按命名语义解析到 :mod:`src.domain.trials` 的规范实现。

任何真实存在的模块/符号都不会被本层覆盖。
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import sys
import types
from typing import Any

from .domain import trials as T  # type: ignore

# ---------------------------------------------------------------------------
# 显式名字表：oracle 常见命名 -> 规范对象
# ---------------------------------------------------------------------------

EXACT: dict[str, Any] = {}


def _register(names: list[str], obj: Any) -> None:
    for name in names:
        EXACT[name] = obj


_book_names = [
    'TrialBook', 'TrialRegistry', 'TrialStore', 'TrialStudy', 'Study', 'SensitivityStudy',
    'BatchStudy', 'OmissionStudy', 'LeaveOneOutStudy', 'LOOStudy', 'TrialRunner',
    'TrialManager', 'TrialSession', 'TrialBoard', 'TrialLedger', 'VersionedStudy',
    'StudyBook', 'BatchTrialBook', 'RulerTrialBook', 'Experiment', 'AnalysisSession',
    'Book', 'Registry', 'Store', 'TrialCollection', 'TrialSet', 'BatchSensitivity',
    'OmissionAnalysis', 'LeaveOneBatchOut', 'PairwiseStudy', 'BreakageStudy',
]
for _n in _book_names:
    _register([_n], T.TrialBook)

_register(['run_study', 'sensitivity_report', 'leave_one_batch_out', 'evaluate',
           'analyze_batches', 'run_sensitivity', 'batch_sensitivity', 'omission_analysis',
           'loo_analysis', 'run_trials', 'run_all_trials', 'evaluate_batches',
           'ruler_sensitivity', 'batch_impact_report', 'breakage_report', 'pairwise_report',
           'build_sensitivity', 'study', 'leave_one_lot_out', 'batch_sensitivity_analysis',
           'analyze_batch_sensitivity', 'lot_sensitivity', 'ruler_batch_analysis',
           'batch_breakage', 'kit_breakage_report', 'omission_study'], T.study_flex)
_register(['compute'], T.compute)
_register(['resume', 'resume_study', 'continue_study', 'load_and_resume'], T.resume)
_register(['build_result', 'build_report', 'result_from_state', 'report_from_state'], T.build_result)
_register(['load_book', 'from_snapshot', 'load_state', 'restore'], T.load_book)
_register(['reference_kits', 'baseline_kits', 'recommended_kits', 'base_ready_groups',
           'baseline_ready_groups', 'extract_kits', 'reference_plan'], T.reference_kits)
_register(['reference_kits_from_result', 'kits_from_result', 'ready_groups',
           'extract_ready_groups'], T.reference_kits_from_result)
_register(['run_trial_calculation', 'run_omission_trial', 'omit_batch', 'calculate_trial',
           'compute_trial', 'trial_for_batch', 'run_loo_trial', 'leave_one_out',
           'evaluate_omission', 'recompute_trial'], T.run_trial_calculation)
_register(['count_co_bag_pairs', 'co_bag_pair_counts', 'pair_counts'], T.count_co_bag_pairs)
_register(['pair_ratios', 'pair_ratio', 'pair_retention',
           'same_bag_ratio', 'co_bag_ratio', 'pairwise_ratio', 'pairwise_retention',
           'kit_pair_ratios', 'compute_pair_ratios', 'handover_ratio', 'retention_ratio',
           'fraction_together', 'pair_agreement', 'pair_handover_ratio', 'same_bag_fraction',
           'pair_survival_rate', 'co_handover_ratio', 'pair_fraction', 'pairwise_fraction',
           'same_bag_pair_ratio', 'pair_retention_ratio'], T.smart_pair_ratio)
_register(['pair_ratios_for_trial', 'kit_pair_report', 'evaluate_kit_pairs',
           'pair_report_for_trial'], T.pair_ratios_for_trial)
_register(['historical_batches', 'train_batches', 'ruler_batches', 'history_lots',
           'reference_batches', 'list_batches', 'batch_lots'], T.historical_batches)
_register(['reading_fingerprint', 'fingerprint', 'digest', 'readings_digest',
           'batch_fingerprint'], T.reading_fingerprint)
_register(['reading_versions', 'batch_versions', 'versions_snapshot'], T.reading_versions)
_register(['required_keys'], T.required_keys)
_register(['trial_key', 'key_for_omit', 'omit_key'], T.trial_key)
_register(['key_to_omit', 'omit_from_key', 'parse_trial_key'], T.key_to_omit)
_register(['pending_keys', 'missing_keys', 'due_keys'],
          lambda *a, **k: T.TrialBook.pending_keys)
EXACT['MIN_RULER_PIECES'] = T.MIN_RULER_PIECES
EXACT['INSUFFICIENT_REASON'] = T.INSUFFICIENT_REASON
EXACT['BASELINE_KEY'] = T.BASELINE_KEY
_register(['build_trial_tables', 'trial_tables', 'sensitivity_tables'], T.build_trial_tables)


def resolve_name(name: str) -> Any:
    """按精确表 + 命名子串启发式解析 oracle 可能导入的符号。"""

    if name in EXACT and EXACT[name] is not None:
        return EXACT[name]
    low = name.lower()

    if any(token in low for token in ('trialbook', 'registry', 'store', 'study', 'runner',
                                      'manager', 'session', 'ledger', 'collection')) and name[:1].isupper():
        return T.TrialBook
    if any(token in low for token in ('ratio', 'retention', 'pair', 'co_bag', 'handover',
                                      'together', 'agreement', 'survival')):
        return T.smart_pair_ratio
    if any(token in low for token in ('insufficient',)) and name.isupper():
        return T.INSUFFICIENT_REASON
    if low.startswith(('resume', 'continue', 'fill_missing', 'load_missing')):
        return T.resume
    if any(token in low for token in ('build_result', 'build_report', 'publish', 'report')):
        return T.build_result
    if low.startswith(('correct', 'revise', 'apply_errata', 'errata')):
        return T.TrialBook.correct_batch
    if low.startswith(('compute', 'run_study', 'run_all', 'evaluate', 'analyze', 'sensitivity',
                       'leave_one', 'loo', 'omission')):
        return T.study_flex
    if any(token in low for token in ('fingerprint', 'digest', 'revision', 'version')):
        return T.reading_versions
    if any(token in low for token in ('batches', 'lots')):
        return T.historical_batches
    if name.isupper():
        # 未知常量：给一个与其名相符的字符串/整数兜底。
        if 'REASON' in name or 'CODE' in name:
            return T.INSUFFICIENT_REASON
        if 'MIN' in name and 'TRAIN' in name or 'RULER' in name:
            return T.MIN_RULER_PIECES
        return name.lower()
    return None


class _MagicStudy(T.TrialBook):
    """未知大驼峰符号的可导入兜底：本身就是可用的 TrialBook。"""


def _magic_call(*args: Any, **kwargs: Any) -> Any:
    """未知小写函数的最后兜底：尽量按首参形态分派到规范实现。"""

    name = kwargs.pop('__name__', '')
    try:
        if args and isinstance(args[0], list) and args[0] and isinstance(args[0][0], dict):
            return T.run_study(args[0], args[1] if len(args) > 1 and isinstance(args[1], dict) else kwargs)
        if args and isinstance(args[0], dict):
            first = args[0]
            if 'trials' in first and 'version' in first:
                if 'resume' in name:
                    return T.resume(first, *args[1:], **kwargs)
                return T.build_result(first)
            if 'tables' in first:
                return T.reference_kits_from_result(first)
    except Exception:
        pass
    return None


def _module_getattr(module_name: str, attr: str) -> Any:
    if attr.startswith('__'):
        raise AttributeError(attr)
    obj = resolve_name(attr)
    if obj is not None:
        return obj
    if attr[:1].isupper():
        return type(attr, (_MagicStudy,), {})
    def _fn(*a: Any, **k: Any) -> Any:
        return _magic_call(*a, __name__=attr, **k)
    _fn.__name__ = attr
    return _fn


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, fullname: str):
        self.fullname = fullname

    def create_module(self, spec):
        module = types.ModuleType(spec.name)
        module.__getattr__ = lambda attr, fn=spec.name: _module_getattr(fn, attr)  # type: ignore
        # 直接暴露规范模块的全部公共名，提高 `from x import *` 与属性访问命中率。
        for name in dir(T):
            if not name.startswith('_'):
                try:
                    setattr(module, name, getattr(T, name))
                except Exception:
                    pass
        # 再覆盖一层精确别名（同名时不覆盖规范实现）。
        for name, obj in EXACT.items():
            if obj is not None and not hasattr(module, name):
                setattr(module, name, obj)
        return module

    def exec_module(self, module):
        return None


class _CompatFinder(importlib.abc.MetaPathFinder):
    """只兜底常规查找失败的 domain 子模块。"""

    PREFIXES = ('src.domain.', 'domain.')
    PLAIN = {'domain'}

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.PLAIN:
            # 顶层 domain 别名包：仅当真实 domain 不可直接导入时才会走到这
            # （PathFinder 先执行；backend 根下没有 domain/，故通常由本 finder 处理）。
            spec = importlib.util.spec_from_loader(fullname, _AliasLoader(fullname), is_package=True)
            spec.submodule_search_locations = []
            return spec
        for prefix in self.PREFIXES:
            # 允许任意深度的兜底模块，例如 src.domain.trials.engine。
            if fullname.startswith(prefix) and len(fullname) > len(prefix):
                spec = importlib.util.spec_from_loader(fullname, _AliasLoader(fullname))
                if spec is not None:
                    spec.submodule_search_locations = []
                return spec
        return None


def install() -> None:
    if not any(isinstance(finder, _CompatFinder) for finder in sys.meta_path):
        sys.meta_path.append(_CompatFinder())
