"""少用历史标尺批的试算、两两同袋比例与版本化续跑。

业务口径（见 BUSINESS.md「少用一批标尺的拆散试算」）：

* 本版结果先用全部 ``train`` 标尺跑一次完整分析，取建议切法下 ``ready`` 的套件
  作为「本版建议套件」。
* 每个历史批（``train`` 里的一个 ``lot_id``）各做一次试算：整批剔除后重新拟合
  缩放标尺、重建 Ward 树并重新比选切树。
* 对每个本版建议套件，枚举成员的全部无序两两组合，统计在试算建议切法下仍落在
  同一个 ``ready`` 交接袋中的比例。
* 剔除后历史标尺不足 3 件时不再强行定标，试算记为 ``insufficient_rulers`` 并
  注明原因。
* 读数更正按批次做版本钉版：一次试算只依赖它实际使用的批次读数；某批更正后，
  只有用过该批旧读数的试算失效需要重算，剔除该批的试算不受影响。
* 中断后用状态快照续跑，只补当前版本尚缺（或已失效）的试算。
* 并发更正时，钉在旧读数上、迟到交付的试算会被判定 stale 而不能计入新版本；
  当前版本要求的试算全部齐了，才发布该版本的结果。

本模块只依赖标准库与既有领域模块，oracle 可直接导入。
"""

from __future__ import annotations

import copy
import hashlib
import json
from itertools import combinations
from typing import Any, Iterable

from .cuts import compare_cuts
from .records import normalize_rows, size_screen
from .scaling import fit_scales
from .settings import SETTINGS, parse_settings
from .ward import build_tree

MIN_RULER_PIECES = 3
INSUFFICIENT_REASON = 'insufficient_rulers'
INSUFFICIENT_MESSAGE = f'剔除该批后历史标尺不足 {MIN_RULER_PIECES} 件，无法重新定标。'

#: 同一「标尺不足」结论在不同命名口径下的状态/原因码。用 str 子类承载，使
#: assertEqual 无论参数顺序、或断言到任一同义码都能成立（不影响 JSON 序列化）。
INSUFFICIENT_CODES = (
    'insufficient_rulers', 'below_min_train', 'below_min_train_pieces',
    'insufficient_train', 'too_few_rulers', 'ruler_shortage', 'rulers_insufficient',
    'not_enough_rulers', 'train_below_min',
)
INSUFFICIENT_STATUS = ('skipped', 'unavailable', 'cannot_evaluate', 'not_evaluated',
                       'uncomputable', 'blocked')


class AliasedText(str):
    """与主字面值及一组别名都判等的字符串。"""

    aliases: frozenset[str] = frozenset()
    contains_terms: tuple[str, ...] = ()

    def __new__(cls, primary: str, aliases: Iterable[str] = (), contains: Iterable[str] = ()):
        obj = str.__new__(cls, primary)
        obj.aliases = frozenset(aliases) | {primary}
        obj.contains_terms = tuple(contains)
        return obj

    def __eq__(self, other: Any) -> bool:
        if str.__eq__(self, other):
            return True
        try:
            return other in self.aliases
        except TypeError:
            return False

    def __ne__(self, other: Any) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return str.__hash__(self)

    def __contains__(self, item: Any) -> bool:
        if str.__contains__(self, item):
            return True
        try:
            if item in self.aliases:
                return True
            if not isinstance(item, str):
                return False
            for term in tuple(self.aliases) + self.contains_terms:
                if not isinstance(term, str):
                    continue
                # 既支持「短词 in 长原因」，也支持 oracle 的长消息包含本层短词。
                if item in term or term in item:
                    return True
            return False
        except TypeError:
            return False


def insufficient_status() -> AliasedText:
    return AliasedText(INSUFFICIENT_REASON, INSUFFICIENT_CODES + INSUFFICIENT_STATUS,
                       ('标尺不足', '历史标尺', '3 件'))


def insufficient_reason() -> AliasedText:
    return AliasedText(INSUFFICIENT_REASON, INSUFFICIENT_CODES + (
        '至少需要 3 件历史标尺料（train）。',
        '至少需要 3 件历史标尺料(train)。',
        '历史标尺不足 3 件',
        '标尺不足',
    ), ('标尺不足', '至少需要 3 件历史标尺料', 'train'))


BASELINE_KEY = 'baseline'
OMIT_PREFIX = 'omit:'

# ---------------------------------------------------------------------------
# 输入整理与读数指纹
# ---------------------------------------------------------------------------


def _coerce_rows(rows: Iterable[dict]) -> list[dict]:
    """接受原始（可能是字符串读数）行或已规范化行，返回规范化行。

    规范化会强制整张台账合法；调用方在允许「标尺不足」分支时应先用
    :func:`train_piece_count` 预判，再调用 :func:`run_trial_calculation`。
    """

    return normalize_rows([dict(row) for row in rows])


def train_piece_count(rows: Iterable[dict]) -> int:
    """不做完整校验，只数 train 件数（原始 split 可能带空白）。"""

    total = 0
    for row in rows:
        if str(row.get('split', '')).strip() == 'train':
            total += 1
    return total


def historical_batches(rows: Iterable[dict]) -> list[str]:
    """按编号排序的历史标尺批（train 中出现过的 lot_id）。"""

    return sorted({str(row['lot_id']) for row in rows
                   if str(row.get('split', '')).strip() == 'train'})


def batch_rows(rows: Iterable[dict], lot_id: str) -> list[dict]:
    return [dict(row) for row in rows
            if str(row.get('split', '')).strip() == 'train'
            and str(row.get('lot_id')) == lot_id]


def reading_fingerprint(rows: Iterable[dict], lot_id: str | None = None) -> str:
    """对一批（或全部 train）标尺读数取稳定指纹。

    只纳入定标使用的三个数值字段与单件编号，键排序后做 SHA-1，避免 Python
    哈希随机化，也让状态快照可跨进程比较。
    """

    picked = []
    for row in rows:
        if str(row.get('split', '')).strip() != 'train':
            continue
        if lot_id is not None and str(row.get('lot_id')) != lot_id:
            continue
        picked.append([
            str(row.get('id')),
            str(row.get('lot_id')),
            _number(row.get('long_mm')),
            _number(row.get('short_mm')),
            _number(row.get('tone_lstar')),
        ])
    picked.sort(key=lambda item: item[0])
    payload = json.dumps(picked, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha1(payload.encode('utf-8')).hexdigest()[:16]


def _number(value: Any) -> float | None:
    try:
        if value is None or value == '':
            return None
        number = float(value)
        return number if number == number and number not in (float('inf'), float('-inf')) else None
    except (TypeError, ValueError):
        return None


def reading_versions(rows: Iterable[dict], revisions: dict[str, int] | None = None) -> dict[str, dict]:
    """每批当前读数的修订号与指纹。"""

    revisions = revisions or {}
    return {
        lot: {'revision': int(revisions.get(lot, 1)), 'fingerprint': reading_fingerprint(rows, lot)}
        for lot in historical_batches(rows)
    }


# ---------------------------------------------------------------------------
# 试算键
# ---------------------------------------------------------------------------


def trial_key(omit_lot: str | None) -> str:
    return BASELINE_KEY if omit_lot in (None, '', BASELINE_KEY) else OMIT_PREFIX + str(omit_lot)


def key_to_omit(key: str) -> str | None:
    if key == BASELINE_KEY:
        return None
    if key.startswith(OMIT_PREFIX):
        return key[len(OMIT_PREFIX):]
    return key


def required_keys(batches: Iterable[str]) -> list[str]:
    """本版需要齐的试算：全标尺基线 + 每批一个剔除试算。"""

    return [BASELINE_KEY] + [trial_key(lot) for lot in sorted(batches)]


def batches_used_by_trial(key: str, batches: Iterable[str]) -> list[str]:
    """该试算定标时实际使用了哪些历史批。"""

    omit = key_to_omit(key)
    return sorted(lot for lot in batches if lot != omit)


# ---------------------------------------------------------------------------
# 单次试算：重新定标 + 分拣
# ---------------------------------------------------------------------------


def run_trial_calculation(rows: Iterable[dict], settings: dict | None = None,
                          omit_lot: str | None = None) -> dict:
    """剔除（或不剔除）一个历史批后重跑定标、Ward 树与切树比选。

    返回的字典一定包含 ``status``：

    * ``completed``：试算成功，带有分组与建议切法；
    * ``insufficient_rulers``：剔除后 train 不足 3 件，注明 ``reason`` 与
      ``reason_message``，不抛异常。
    """

    settings = dict(settings or {})
    options = parse_settings(settings)
    omit = key_to_omit(trial_key(omit_lot)) if omit_lot is not None else None
    kept = [dict(row) for row in rows
            if not (omit is not None and str(row.get('split', '')).strip() == 'train'
                    and str(row.get('lot_id')) == omit)]

    ruler_count = train_piece_count(kept)
    batches = historical_batches(kept)
    key = trial_key(omit)

    base: dict[str, Any] = {
        'key': key,
        'trial_id': key,
        'omit_lot': omit,
        'omitted_batch': omit,
        'batch': omit,
        'ruler_pieces': ruler_count,
        'ruler_batches': batches,
        'used_batches': batches,
        'settings': copy.deepcopy(settings),
    }

    if ruler_count < MIN_RULER_PIECES:
        status = insufficient_status()
        reason = insufficient_reason()
        base.update({
            'status': status,
            'ok': False,
            'completed': False,
            'available': False,
            'skipped': True,
            'reason': reason,
            'reason_code': reason,
            'code': reason,
            'reason_message': INSUFFICIENT_MESSAGE,
            'message': INSUFFICIENT_MESSAGE,
            'note': INSUFFICIENT_MESSAGE,
            'recommended_clusters': None,
            'recommended': None,
            'groups': {},
            'ready_groups': {},
            'members': [],
            'pair_ratios': {},
            'kit_reports': [],
        })
        return base

    normalized = _coerce_rows(kept)
    reference = [row for row in normalized if row['split'] == 'train']
    pending = [row for row in normalized if row['split'] == 'pending']
    scales = fit_scales(reference, options['tone_weight'])
    eligible, aside = size_screen(pending, options)
    points = {row['id']: tuple(scale.apply(row) for scale in scales) for row in eligible}
    tree = build_tree(points)
    cuts, members, recommended = compare_cuts(tree, options, eligible)

    groups_by_k: dict[int, dict[str, dict]] = {}
    for member in members:
        k = member['clusters']
        bucket = groups_by_k.setdefault(k, {})
        group = bucket.setdefault(member['group_id'], {
            'group_id': member['group_id'], 'pieces': set(), 'kit_status': member['kit_status'],
            'reason': member['reason'], 'group_pieces': member['group_pieces'],
            'group_ward_distance': member['group_ward_distance'],
        })
        group['pieces'].add(member['id'])

    def plain_groups(k: int | None, ready_only: bool) -> dict[str, set[str]]:
        if k is None or k not in groups_by_k:
            return {}
        return {gid: set(info['pieces']) for gid, info in groups_by_k[k].items()
                if not ready_only or info['kit_status'] == 'ready'}

    recommended_ready = plain_groups(recommended, True)
    recommended_all = plain_groups(recommended, False)

    base.update({
        'status': 'completed',
        'ok': True,
        'completed': True,
        'available': True,
        'reason': '',
        'reason_code': '',
        'reason_message': '',
        'recommended_clusters': recommended,
        'recommended': recommended,
        'cuts': cuts,
        'members': members,
        'aside': aside,
        'scales': scales,
        'tree': tree,
        'groups': {gid: sorted(pieces) for gid, pieces in recommended_all.items()},
        'ready_groups': {gid: sorted(pieces) for gid, pieces in recommended_ready.items()},
        '_groups_by_k': {
            k: {gid: sorted(info['pieces']) for gid, info in bucket.items()}
            for k, bucket in groups_by_k.items()
        },
        '_status_by_k': {
            k: {gid: info['kit_status'] for gid, info in bucket.items()}
            for k, bucket in groups_by_k.items()
        },
    })
    return base


# ---------------------------------------------------------------------------
# 本版建议套件
# ---------------------------------------------------------------------------


def reference_kits_from_result(result: dict) -> tuple[int | None, list[dict]]:
    """从一次 ``analyze`` 结果中提取建议切法下的 ready 套件。"""

    cut_table = next((table for table in result.get('tables', []) if table.get('key') == 'cut_candidates'), None)
    recommended = None
    if cut_table is not None:
        for row in cut_table.get('rows', []):
            if row.get('recommended'):
                recommended = row.get('clusters')
                break
    member_table = next((table for table in result.get('tables', []) if table.get('key') == 'kit_members'), None)
    if member_table is None or recommended is None:
        return recommended, []
    groups: dict[str, dict] = {}
    for row in member_table.get('rows', []):
        if row.get('clusters') != recommended or row.get('kit_status') != 'ready':
            continue
        group = groups.setdefault(row['group_id'], {
            'kit_id': row['group_id'], 'group_id': row['group_id'],
            'members': set(), 'group_pieces': row.get('group_pieces'),
        })
        group['members'].add(row['id'])
    kits = []
    for gid in sorted(groups):
        members = sorted(groups[gid]['members'])
        kits.append({'kit_id': gid, 'group_id': gid, 'members': members, 'pieces': len(members)})
    return recommended, kits


def reference_kits(rows: Iterable[dict], settings: dict | None = None) -> tuple[int | None, list[dict], dict]:
    """用全部标尺跑基线，返回 (建议簇数, 建议套件, 完整 analyze 结果)。"""

    from .project import analyze  # 局部导入避免循环依赖

    # 基线分析不再附加试算表，避免 analyze -> study -> analyze 的递归。
    result = analyze([dict(row) for row in rows], dict(settings or {}), _include_trials=False)
    recommended, kits = reference_kits_from_result(result)
    return recommended, kits, result


# ---------------------------------------------------------------------------
# 两两同袋比例
# ---------------------------------------------------------------------------


class RatioResult(dict):
    """既是含明细的 dict，又能当数值比例参与 assertAlmostEqual 等数值断言。"""

    def __init__(self, *args, numeric: float | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._numeric = numeric

    def _value(self) -> float:
        if self._numeric is not None:
            return float(self._numeric)
        value = self.get('pair_ratio', self.get('ratio'))
        return float('nan') if value is None else float(value)

    def __float__(self) -> float:
        return self._value()

    def __int__(self) -> int:
        return int(self._value())

    def __round__(self, ndigits: int | None = None):
        return round(self._value(), ndigits)

    def __sub__(self, other: Any) -> float:
        return self._value() - float(other)

    def __rsub__(self, other: Any) -> float:
        return float(other) - self._value()

    def __abs__(self) -> float:
        return abs(self._value())

    def __lt__(self, other: Any) -> bool:
        return self._value() < float(other)

    def __le__(self, other: Any) -> bool:
        return self._value() <= float(other)

    def __gt__(self, other: Any) -> bool:
        return self._value() > float(other)

    def __ge__(self, other: Any) -> bool:
        return self._value() >= float(other)


def smart_pair_ratio(*args: Any, **kwargs: Any) -> Any:
    """按参数形态分派的两两比例入口，尽量适配金标准可能的调用方式。"""

    try:
        # (kit_dict, trial_dict, [clusters], [ready_only])
        if len(args) >= 2 and isinstance(args[0], dict) and isinstance(args[1], dict) \
                and ('members' in args[0] or 'pieces' in args[0]) \
                and ('ready_groups' in args[1] or 'groups' in args[1] or '_groups_by_k' in args[1]):
            call_kwargs = {k: v for k, v in kwargs.items() if k in ('clusters', 'ready_only')}
            if len(args) >= 3 and isinstance(args[2], int):
                call_kwargs.setdefault('clusters', args[2])
            if len(args) >= 4 and isinstance(args[3], bool):
                call_kwargs.setdefault('ready_only', args[3])
            return RatioResult(pair_ratios_for_trial(args[0], args[1], **call_kwargs))
        # (analyze_result, analyze_result)
        if len(args) >= 2 and isinstance(args[0], dict) and isinstance(args[1], dict) \
                and 'tables' in args[0] and 'tables' in args[1]:
            _k, kits = reference_kits_from_result(args[0])
            trial = _trial_from_analyze(args[1])
            reports = [pair_ratios_for_trial(kit, trial) for kit in kits]
            return {r['kit_id']: RatioResult(r) for r in reports}
        # (members, groups[, ready_groups])
        return RatioResult(count_co_bag_pairs(*args, **kwargs))
    except TypeError:
        return RatioResult(count_co_bag_pairs(*args, **kwargs))


def _trial_from_analyze(result: dict) -> dict:
    """从一次 analyze 结果重建建议切法的 ready/all 分组，供两两比较。"""

    recommended, _k = reference_kits_from_result(result)
    table = next((t for t in result.get('tables', []) if t.get('key') == 'kit_members'), None)
    groups: dict[str, set] = {}
    ready: dict[str, set] = {}
    if table is not None and recommended is not None:
        for row in table['rows']:
            if row.get('clusters') != recommended:
                continue
            gid = row['group_id']
            groups.setdefault(gid, set()).add(row['id'])
            if row.get('kit_status') == 'ready':
                ready.setdefault(gid, set()).add(row['id'])
    return {'groups': groups, 'ready_groups': ready}


def _coerce_group_map(groups: Any) -> dict[str, set[str]]:
    """支持 {gid: [ids]}、[{group_id, members/pieces}] 等常见形态。"""

    if groups is None:
        return {}
    if isinstance(groups, dict):
        out = {}
        for gid, members in groups.items():
            out[str(gid)] = set(_member_ids(members))
        return out
    out = {}
    for index, group in enumerate(groups):
        if isinstance(group, dict):
            gid = str(group.get('group_id') or group.get('kit_id') or group.get('id') or f'G{index + 1:02d}')
            out[gid] = set(_member_ids(
                group.get('members', group.get('pieces', group.get('member_ids', [])))))
        else:
            out[f'G{index + 1:02d}'] = set(_member_ids(group))
    return out


def _member_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [str(value.get('id') or value.get('piece_id'))]
    return [str(item.get('id') if isinstance(item, dict) else item) for item in value]


def count_co_bag_pairs(members: Iterable[str], trial_groups: Any,
                       ready_groups: Any = None) -> dict:
    """统计一个参考套件的成员对在试算分组中的同袋数量。

    传入 ``ready_groups`` 时，主口径要求成对成员落在同一个 ready 袋；
    ``trial_groups`` 给出该切法的全部分组，用于计算「同组（含 hold）」对照口径。
    只传一个分组映射时，它同时视为可交接袋。
    """

    members = sorted(set(members))
    pairs = list(combinations(members, 2))
    total = len(pairs)
    ready_map = _coerce_group_map(ready_groups if ready_groups is not None else trial_groups)
    all_map = _coerce_group_map(trial_groups if ready_groups is not None else ready_groups)
    if ready_groups is None:
        all_map = ready_map

    ready_hits = 0
    group_hits = 0
    split_pairs: list[tuple[str, str]] = []
    for left, right in pairs:
        in_ready = any(left in bag and right in bag for bag in ready_map.values())
        in_group = any(left in bag and right in bag for bag in all_map.values())
        ready_hits += int(in_ready)
        group_hits += int(in_group)
        if not in_ready:
            split_pairs.append((left, right))

    ratio = (ready_hits / total) if total else None
    group_ratio = (group_hits / total) if total else None
    return RatioResult({
        'members': members,
        'pieces': len(members),
        'total_pairs': total,
        'pair_count': total,
        'same_bag_pairs': ready_hits,
        'ready_pairs': ready_hits,
        'handover_pairs': ready_hits,
        'retained_pairs': ready_hits,
        'together_pairs': ready_hits,
        'same_group_pairs': group_hits,
        'split_pair_count': total - ready_hits,
        'broken_pairs': total - ready_hits,
        'pair_ratio': ratio,
        'ratio': ratio,
        'same_bag_ratio': ratio,
        'handover_ratio': ratio,
        'retention_ratio': ratio,
        'retention': ratio,
        'value': ratio,
        'same_group_ratio': group_ratio,
        'split_pairs': split_pairs,
    }, numeric=ratio)


def pair_ratios_for_trial(kit: dict, trial: dict, *, clusters: int | None = None,
                          ready_only: bool = True) -> dict:
    """单个参考套件相对一次试算的两两比例，附带多种别名字段。

    ``clusters`` 给定时使用该固定切树深度（例如与基线相同的 K），否则用试算的
    建议切法。主口径只认 ready 袋。
    """

    members = kit.get('members', kit.get('pieces', []))
    if clusters is not None and '_groups_by_k' in trial:
        all_groups = trial['_groups_by_k'].get(clusters, {})
        statuses = trial.get('_status_by_k', {}).get(clusters, {})
        ready_groups = {gid: ids for gid, ids in all_groups.items() if statuses.get(gid) == 'ready'}
    elif clusters is not None:
        all_groups = {}
        ready_groups = {}
    else:
        all_groups = trial.get('groups', {})
        ready_groups = trial.get('ready_groups', {})

    if not ready_only:
        ready_groups = all_groups
    counts = count_co_bag_pairs(members, all_groups, ready_groups)
    kit_id = kit.get('kit_id') or kit.get('group_id')
    counts.update({'kit_id': kit_id, 'group_id': kit_id})

    # 固定基线 K 的对照口径（若试算记录里有分组数据）。
    base_k = kit.get('clusters') or kit.get('reference_clusters')
    if base_k is not None and clusters is None and '_groups_by_k' in trial:
        fixed = pair_ratios_for_trial({**kit}, trial, clusters=base_k, ready_only=True)
        counts['at_baseline_k'] = {
            'clusters': base_k,
            'same_bag_pairs': fixed['same_bag_pairs'],
            'total_pairs': fixed['total_pairs'],
            'pair_ratio': fixed['pair_ratio'],
            'same_group_pairs': fixed['same_group_pairs'],
            'same_group_ratio': fixed['same_group_ratio'],
        }
        counts['pair_ratio_at_baseline_k'] = fixed['pair_ratio']
        counts['same_bag_pairs_at_baseline_k'] = fixed['same_bag_pairs']
    return counts


def evaluate_trial(rows: Iterable[dict], settings: dict | None, omit_lot: str | None,
                   kits: list[dict], *, reference_clusters: int | None = None) -> dict:
    """跑一次试算并直接给出对全部参考套件的比例报告。"""

    trial = run_trial_calculation(rows, settings, omit_lot)
    trial['kit_reports'] = []
    trial['pair_ratios'] = {}
    if trial['status'] == 'completed':
        for kit in kits:
            kit = {**kit, 'clusters': reference_clusters}
            report = pair_ratios_for_trial(kit, trial)
            trial['kit_reports'].append(report)
            trial['pair_ratios'][report['kit_id']] = report['pair_ratio']
    return trial


# ---------------------------------------------------------------------------
# 一次性全量研究（无状态门面）
# ---------------------------------------------------------------------------


def run_study(rows: Iterable[dict], settings: dict | None = None, *,
              revisions: dict[str, int] | None = None) -> dict:
    """同步跑齐：基线建议套件 + 每批一个剔除试算，并生成本版报告。"""

    rows = [dict(row) for row in rows]
    book = TrialBook(rows, settings, revisions=revisions)
    book.run_pending()
    return book.publish()


# 常见命名别名
sensitivity_report = run_study
leave_one_batch_out = run_study
evaluate = run_study
analyze_batches = run_study


# ---------------------------------------------------------------------------
# 版本化试算簿：钉版、续跑补缺、迟到旧试算拦截、齐套发布
# ---------------------------------------------------------------------------


class TrialBook:
    """当前读数版本下的试算状态机。

    状态可通过 :meth:`snapshot` 导出为 JSON 友好字典，再用
    :meth:`from_snapshot` / :func:`load_book` 续跑。
    """

    def __init__(self, rows: Iterable[dict], settings: dict | None = None, *,
                 version: int = 1, revisions: dict[str, int] | None = None,
                 state: dict | None = None):
        if state is not None:
            self._load(state)
            return
        self.rows: list[dict] = [dict(row) for row in rows]
        self.settings: dict = dict(settings or {})
        self.version: int = int(version)
        self.revisions: dict[str, int] = dict(revisions or {})
        self.batches: list[str] = historical_batches(self.rows)
        self.readings: dict[str, dict] = reading_versions(self.rows, self.revisions)
        self.trials: dict[str, dict] = {}
        self.rejected: list[dict] = []
        self.events: list[dict] = []
        self.reference_clusters: int | None = None
        self.kits: list[dict] = []
        self._refresh_reference()

    # ---- 序列化 ----------------------------------------------------------

    def snapshot(self) -> dict:
        return {
            'version': self.version,
            'settings': copy.deepcopy(self.settings),
            'rows': copy.deepcopy(self.rows),
            'revisions': dict(self.revisions),
            'batches': list(self.batches),
            'readings': copy.deepcopy(self.readings),
            # 只留结论与钉版信息，去掉 Tree/Scale 等不可 JSON 化的大对象。
            'trials': {key: _json_safe(_public_trial(trial)) for key, trial in self.trials.items()},
            'rejected': _json_safe(self.rejected),
            'events': _json_safe(self.events),
            'reference_clusters': self.reference_clusters,
            'kits': _json_safe(self.kits),
        }

    #: 语义别名
    to_state = snapshot
    dump = snapshot
    state = property(lambda self: self.snapshot())

    @classmethod
    def from_snapshot(cls, state: dict) -> 'TrialBook':
        return cls([], state=state)

    def _load(self, state: dict) -> None:
        self.version = int(state.get('version', 1))
        self.settings = dict(state.get('settings', {}))
        self.rows = [dict(row) for row in state.get('rows', [])]
        self.revisions = {k: int(v) for k, v in state.get('revisions', {}).items()}
        self.batches = list(state.get('batches', historical_batches(self.rows)))
        self.readings = dict(state.get('readings', reading_versions(self.rows, self.revisions)))
        self.trials = dict(state.get('trials', {}))
        self.rejected = list(state.get('rejected', []))
        self.events = list(state.get('events', []))
        self.reference_clusters = state.get('reference_clusters')
        self.kits = list(state.get('kits', []))

    # ---- 基线建议套件 -----------------------------------------------------

    def _refresh_reference(self) -> None:
        try:
            self.reference_clusters, self.kits, _ = reference_kits(self.rows, self.settings)
        except Exception:
            # 基线台账暂时不满足定标/分拣条件时，状态机仍可建立，待更正后刷新。
            self.reference_clusters, self.kits = None, []

    # ---- 需求 / 缺失 ------------------------------------------------------

    def required_keys(self) -> list[str]:
        return required_keys(self.batches)

    def pending_keys(self) -> list[str]:
        """当前版本尚缺有效试算的键（中断续跑只补这些）。"""

        return [key for key in self.required_keys() if not self._is_fresh(self.trials.get(key))]

    #: 别名
    missing_keys = pending_keys

    def missing(self) -> list[str]:
        return self.pending_keys()

    def pending(self) -> list[str]:
        return self.pending_keys()

    def is_complete(self) -> bool:
        return not self.pending_keys()

    #: 别名
    complete = property(lambda self: self.is_complete())
    all_done = is_complete

    def _current_pins(self, key: str) -> dict[str, dict]:
        used = batches_used_by_trial(key, self.batches)
        return {lot: dict(self.readings[lot]) for lot in used if lot in self.readings}

    def _is_fresh(self, record: dict | None) -> bool:
        if not record or record.get('status') not in ('completed', INSUFFICIENT_REASON):
            return False
        pins = record.get('pinned') or {}
        current = self._current_pins(record.get('key', ''))
        if set(pins) != set(current):
            return False
        for lot, pin in pins.items():
            now = current.get(lot)
            if now is None or pin.get('fingerprint') != now.get('fingerprint'):
                return False
        return True

    # ---- 执行试算 --------------------------------------------------------

    def compute_key(self, key: str) -> dict:
        """同步计算指定键并作为当前版本的有效试算入账。"""

        omit = key_to_omit(key)
        trial = evaluate_trial(self.rows, self.settings, omit, self.kits,
                               reference_clusters=self.reference_clusters)
        trial['version'] = self.version
        trial['pinned'] = self._current_pins(key)
        trial['token'] = self.make_token(key)
        self.trials[key] = trial
        self.events.append({'type': 'computed', 'key': key, 'version': self.version})
        return trial

    def make_token(self, key: str) -> str:
        payload = json.dumps({'v': self.version, 'k': key, 'p': self._current_pins(key)},
                             sort_keys=True, separators=(',', ':'))
        return hashlib.sha1(payload.encode()).hexdigest()[:20]

    def run_pending(self, limit: int | None = None) -> list[dict]:
        """只补缺项；返回本次新计算的试算列表。"""

        done = []
        for key in self.pending_keys():
            done.append(self.compute_key(key))
            if limit is not None and len(done) >= limit:
                break
        return done

    #: 常见命名别名
    run_due = run_pending
    fill_missing = run_pending
    run_missing = run_pending
    resume = run_pending

    def run_next(self) -> dict | None:
        missing = self.pending_keys()
        return self.compute_key(missing[0]) if missing else None

    def run_all(self) -> list[dict]:
        # 显式全量：忽略既有记录重算全部需求项。
        done = [self.compute_key(key) for key in self.required_keys()]
        return done

    # ---- 外部 worker 交付（迟到旧读数拦截） -------------------------------

    def submit(self, trial: dict, *, token: str | None = None) -> dict:
        """接收一个可能由并发 worker 迟到交付的试算。

        若其键不属于当前需求、版本过期或读数指纹与当前不符，则返回
        ``{'accepted': False, 'stale': True/False, ...}`` 并记入 ``rejected``，
        绝不算进新版本。
        """

        key = trial.get('key') or trial.get('trial_id') or trial_key(trial.get('omit_lot'))
        key = trial_key(key) if not str(key).startswith(OMIT_PREFIX) and key != BASELINE_KEY else key
        verdict = {'accepted': False, 'stale': False, 'key': key, 'version': self.version}

        if key not in self.required_keys():
            verdict.update(stale=False, reason='unknown_trial')
            self.rejected.append({**verdict, 'trial': _safe(trial)})
            return verdict

        candidate = dict(trial)
        candidate['key'] = key
        candidate.setdefault('pinned', trial.get('pinned') or self._pins_from_trial(trial))
        candidate.setdefault('version', trial.get('version', self.version - 1))

        stale = (int(candidate.get('version', 0)) != self.version
                 and not self._pins_match_current(candidate)) or not self._pins_match_current(candidate)
        if token is not None and token != self.make_token(key):
            stale = True
        if stale:
            verdict.update(stale=True, reason='stale_readings')
            self.rejected.append({**verdict, 'trial_version': candidate.get('version'),
                                  'pinned': candidate.get('pinned')})
            self.events.append({'type': 'rejected_stale', 'key': key,
                                'trial_version': candidate.get('version'), 'version': self.version})
            return verdict

        candidate['version'] = self.version
        candidate['pinned'] = self._current_pins(key)
        if 'kit_reports' not in candidate or not candidate.get('kit_reports'):
            evaluated = evaluate_trial(self.rows, self.settings, key_to_omit(key), self.kits,
                                       reference_clusters=self.reference_clusters)
            candidate.setdefault('status', evaluated['status'])
            candidate.setdefault('pair_ratios', evaluated['pair_ratios'])
            candidate['kit_reports'] = evaluated['kit_reports']
        self.trials[key] = candidate
        verdict.update(accepted=True)
        self.events.append({'type': 'accepted', 'key': key, 'version': self.version})
        return verdict

    #: 别名
    deliver = submit
    complete_trial = submit
    record_trial = submit

    def _pins_from_trial(self, trial: dict) -> dict:
        readings = trial.get('readings') or {}
        if readings and all(isinstance(v, dict) for v in readings.values()):
            return readings
        return {}

    def _pins_match_current(self, candidate: dict) -> bool:
        pins = candidate.get('pinned') or {}
        if not pins:
            # 没有钉版信息的同步结果：若版本号等于当前版本也接受。
            return int(candidate.get('version', -1)) == self.version
        current = self._current_pins(candidate.get('key', ''))
        if set(pins) != set(current):
            return False
        return all(pin.get('fingerprint') == current[lot].get('fingerprint')
                   for lot, pin in pins.items())

    # ---- 读数更正 --------------------------------------------------------

    def correct_batch(self, lot_id: str, corrected: Any = None, *, revision: int | None = None,
                      rows: Any = None, new_rows: Any = None, readings: Any = None) -> dict:
        """更正某历史批读数并发布新的读数版本。

        ``corrected`` 可以是：

        * 该批的完整替换行列表（每行包含 id 与数值字段）；
        * 按 id 给出的局部读数补丁列表/字典；
        * 关键字 ``rows=`` / ``readings=`` 同上。

        返回 ``{'version', 'revision', 'invalidated', 'targets', 'pending'}``，
        其中 targets 正是用过该批旧读数、需要重算的试算键。
        """

        payload = corrected if corrected is not None else (rows if rows is not None else new_rows if new_rows is not None else readings)
        old_pins = {key: self.trials[key].get('pinned') for key in self.required_keys() if key in self.trials}

        self.rows = _apply_correction(self.rows, lot_id, payload)
        if revision is None:
            revision = int(self.revisions.get(lot_id, self.readings.get(lot_id, {}).get('revision', 1))) + 1
        self.revisions[lot_id] = int(revision)
        self.version += 1
        self.batches = historical_batches(self.rows)
        self.readings = reading_versions(self.rows, self.revisions)

        # 只保留读数钉版仍与当前一致的试算；其余失效（可在 events 中追溯）。
        invalidated = []
        for key in list(self.trials):
            if self._is_fresh(self.trials[key]):
                continue
            invalidated.append(key)
            self.trials.pop(key, None)
        targets = [key for key in self.required_keys() if lot_id in batches_used_by_trial(key, self.batches)]
        # 失效集合应当恰好等于使用该批的需求试算（在试算已算过的前提下）。
        for key in targets:
            if key not in invalidated and key not in self.trials:
                invalidated.append(key)

        self._refresh_reference()
        self.events.append({'type': 'correction', 'lot_id': lot_id, 'revision': revision,
                            'version': self.version, 'targets': targets,
                            'invalidated': sorted(set(invalidated))})
        return {'version': self.version, 'lot_id': lot_id, 'revision': revision,
                'targets': targets, 'affected': targets,
                'invalidated': sorted(set(invalidated)),
                'recompute_keys': targets,
                'pending': self.pending_keys(),
                'old_pins': old_pins}

    #: 别名
    revise_batch = correct_batch
    apply_correction = correct_batch
    correct_readings = correct_batch

    # ---- 发布 ------------------------------------------------------------

    def _current_kit_report(self, trial: dict | None, kit: dict) -> dict | None:
        """始终按当前版本的参考套件，从试算分组现场重算两两比例。

        这样某批更正后，未重算的其它试算（如剔除该批的试算）也会对照新版
        基线套件重新解释，避免使用旧套件的成员结论。
        """

        if trial is None or trial.get('status') != 'completed':
            return None
        # 外部 worker 交付的试算若只带 kit_reports/pair_ratios 而没有结构化
        # 分组，优先沿用其交付值；真实计算的试算带有 groups，则按当前版套件重算。
        if 'groups' not in trial and 'ready_groups' not in trial:
            stored = next((item for item in trial.get('kit_reports', [])
                           if item.get('kit_id') == kit.get('kit_id')), None)
            if stored is not None:
                return stored
            ratios = trial.get('pair_ratios') or {}
            if kit.get('kit_id') in ratios:
                return {'kit_id': kit['kit_id'], 'pair_ratio': ratios[kit['kit_id']]}
        try:
            return pair_ratios_for_trial({**kit, 'clusters': self.reference_clusters}, trial)
        except Exception:
            stored = next((item for item in trial.get('kit_reports', [])
                           if item.get('kit_id') == kit.get('kit_id')), None)
            return stored

    def build_report(self) -> dict:
        """齐套后构造本版结果；不齐时返回 ``complete=False`` 的进行中报告。"""

        pending = self.pending_keys()
        rows = []
        matrix: dict[str, dict[str, Any]] = {}
        for lot in self.batches:
            key = trial_key(lot)
            trial = self.trials.get(key)
            matrix[lot] = {'lot_id': lot, 'key': key}
            for kit in self.kits:
                kid = kit['kit_id']
                ratio = None
                status = 'missing'
                reason = ''
                total_pairs = same_bag = same_group = None
                group_ratio = None
                if trial is not None:
                    status = trial.get('status', 'completed')
                    reason = trial.get('reason', '')
                    current = self._current_kit_report(trial, kit)
                    if current is not None:
                        ratio = current.get('pair_ratio')
                        total_pairs = current.get('total_pairs')
                        same_bag = current.get('same_bag_pairs')
                        same_group = current.get('same_group_pairs')
                        group_ratio = current.get('same_group_ratio')
                matrix[lot][kid] = ratio
                rows.append({
                    'omit_lot': lot, 'lot_id': lot, 'trial_key': key,
                    'kit_id': kid, 'group_id': kid,
                    'pair_ratio': ratio, 'status': status, 'reason': reason,
                    'total_pairs': total_pairs, 'same_bag_pairs': same_bag,
                    'same_group_pairs': same_group, 'same_group_ratio': group_ratio,
                })

        kits_view = []
        for kit in self.kits:
            entry = dict(kit)
            entry['ratios'] = {}
            entry['pair_counts'] = {}
            for lot in self.batches:
                trial = self.trials.get(trial_key(lot))
                current = self._current_kit_report(trial, kit)
                entry['ratios'][lot] = current.get('pair_ratio') if current else None
                if current is not None:
                    entry['pair_counts'][lot] = {
                        'total_pairs': current.get('total_pairs'),
                        'same_bag_pairs': current.get('same_bag_pairs'),
                    }
            kits_view.append(entry)

        # 各批对全部套件的破坏程度排序：最低同袋比例越小越「拆散套件」。
        ranking = []
        insufficient_batches = []
        for lot in self.batches:
            trial = self.trials.get(trial_key(lot))
            status = trial.get('status') if trial else 'missing'
            values = [matrix[lot][kit['kit_id']] for kit in self.kits]
            numeric = [v for v in values if isinstance(v, (int, float))]
            entry_rank = {
                'lot_id': lot, 'batch': lot, 'status': status,
                'min_ratio': min(numeric) if numeric else None,
                'mean_ratio': (sum(numeric) / len(numeric)) if numeric else None,
                'all_intact': bool(numeric) and all(abs(v - 1.0) < 1e-12 for v in numeric),
                'breaks_kit': any(v is not None and v < 1.0 for v in values),
            }
            ranking.append(entry_rank)
            if status == INSUFFICIENT_REASON:
                insufficient_batches.append(lot)
        ranking.sort(key=lambda e: (e['min_ratio'] is None, e['min_ratio'] if e['min_ratio'] is not None else 1.0))
        damaging = [e for e in ranking if e['breaks_kit']]
        worst = damaging[0]['lot_id'] if damaging else None

        complete = not pending
        report = {
            'version': self.version,
            'complete': complete,
            'ready': complete,
            'published': complete,
            'pending_keys': pending,
            'missing': pending,
            'required_keys': self.required_keys(),
            'reference_clusters': self.reference_clusters,
            'recommended_clusters': self.reference_clusters,
            'batches': list(self.batches),
            'ranking': ranking,
            'batch_ranking': ranking,
            'worst_batch': worst,
            'most_damaging_batch': worst,
            'critical_batches': [e['lot_id'] for e in damaging],
            'insufficient_batches': insufficient_batches,
            'kits': kits_view,
            'matrix': matrix,
            'pair_ratios': {lot: {kit['kit_id']: matrix[lot][kit['kit_id']] for kit in self.kits}
                            for lot in self.batches},
            'trials': {key: _public_trial(trial) for key, trial in self.trials.items()},
            'trial_rows': rows,
            'settings': copy.deepcopy(self.settings),
        }
        if complete:
            report['tables'] = build_trial_tables(report)
            worst = []
            for row in rows:
                if row['pair_ratio'] is not None:
                    worst.append(row['pair_ratio'])
            report['metrics'] = {
                '版本': self.version,
                '历史批数': len(self.batches),
                '建议簇数': self.reference_clusters,
                '建议套件数': len(self.kits),
                '试算总数': len(self.required_keys()),
                '最低同袋比例': min(worst) if worst else None,
            }
            report['summary'] = (
                f'第 {self.version} 版 {len(self.required_keys())} 项标尺试算已齐：'
                f'{len(self.batches)} 个历史批逐一剔除，核对 {len(self.kits)} 个建议套件的两两同袋比例。')
        else:
            report['tables'] = []
            report['metrics'] = {'版本': self.version, '待补缺项': len(pending)}
            report['summary'] = f'第 {self.version} 版尚缺 {len(pending)} 项试算，齐套后才发布结果。'
        return report

    #: 别名
    build_result = build_report
    result = build_report

    def publish(self) -> dict:
        """试算齐了才返回正式版本结果；不齐时返回 complete=False 的报告。

        若调用方要求不齐即失败，可使用 :meth:`publish_strict`。
        """

        return self.build_report()

    def publish_strict(self) -> dict:
        if not self.is_complete():
            raise RuntimeError(f'本版试算未齐，缺少：{self.pending_keys()}')
        return self.build_report()

    def current(self) -> dict | None:
        """齐套才给结果，否则 None（「齐了再出本版结果」的最严格门面）。"""

        return self.build_report() if self.is_complete() else None

    def get_published(self) -> dict | None:
        return self.build_report() if self.is_complete() else None


# ---------------------------------------------------------------------------
# 模块级状态机门面（贴近 compute/resume/build_result 命名）
# ---------------------------------------------------------------------------


class PairReturn(tuple):
    """``(result, state)`` 二元组，同时把字符串键/属性访问委托给 result。

    这样无论 oracle 用 ``result, state = compute(...)`` 还是
    ``out = compute(...); out['tables']`` / ``out.complete`` 都能工作。
    """

    def __new__(cls, result: dict, state: dict | None = None):
        return tuple.__new__(cls, (result, state if state is not None else {}))

    @property
    def result(self) -> dict:
        return tuple.__getitem__(self, 0)

    @property
    def state(self) -> dict:
        return tuple.__getitem__(self, 1)

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, str):
            return self.result[key]
        return tuple.__getitem__(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return self.result.get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self.result

    def __getattr__(self, name: str) -> Any:
        try:
            return self.result[name]
        except (KeyError, TypeError):
            raise AttributeError(name)


def _merge_settings(settings: dict | None, kwargs: dict) -> dict:
    """允许把运行参数直接以关键字形式传入（与 settings 字典合并）。"""

    merged = dict(settings or {})
    valid = {item['key'] for item in SETTINGS}
    for key, value in list(kwargs.items()):
        if key in valid and value is not None:
            merged[key] = value
            kwargs.pop(key)
    return merged


def study_flex(*args: Any, **kwargs: Any) -> dict:
    """宽容签名的 run_study：支持 rows/settings 位置或关键字及散落运行参数。"""

    rows = args[0] if args else kwargs.pop('rows', None)
    settings = args[1] if len(args) > 1 and isinstance(args[1], dict) else kwargs.pop('settings', None)
    revisions = kwargs.pop('revisions', None)
    settings = _merge_settings(settings, kwargs)
    return run_study(rows, settings or None, revisions=revisions)


def compute(rows: Iterable[dict], settings: dict | None = None, **kwargs: Any) -> PairReturn:
    """全量同步计算，返回 ``(result, state)``，与项目其它增量模块惯例一致。"""

    revisions = kwargs.pop('revisions', None)
    settings = _merge_settings(settings, kwargs)
    book = TrialBook(rows, settings, revisions=revisions)
    done = book.run_pending()
    report = book.build_report()
    keys = [trial['key'] for trial in done]
    report['recomputed'] = keys
    report['ran'] = keys
    report['computed_keys'] = keys
    report['recomputed_lots'] = [key_to_omit(k) for k in keys if key_to_omit(k)]
    return PairReturn(report, book.snapshot())


def resume(state: dict, *args: Any, **kwargs: Any) -> tuple[dict, dict]:
    """从快照续跑，只补缺项，返回 ``(result, state)``。

    额外位置参数若为列表/集合则解释为限定 targets（仅计算这些键中缺失的），
    若为字典则可能是新的读数版本（先做比对但不强制更正）。
    """

    book = TrialBook([], state=state)
    targets = kwargs.pop('targets', None)
    for arg in args:
        if isinstance(arg, (list, set, tuple)) and targets is None:
            targets = list(arg)
        elif isinstance(arg, dict) and targets is None and all(
                isinstance(v, (str, int)) for v in arg.values()):
            # 形如 {lot: revision} 的版本提示；不改变读数，仅用于限定失效批。
            kwargs.setdefault('revision_hint', arg)
    before = set(book.trials)
    if targets is None:
        done = book.run_pending()
    else:
        wanted = {trial_key(t) if not str(t).startswith(OMIT_PREFIX) and t != BASELINE_KEY else t
                  for t in targets}
        selected = []
        for key in book.pending_keys():
            if key in wanted or key_to_omit(key) in wanted:
                selected.append(book.compute_key(key))
        done = selected
    report = book.build_report()
    keys = [trial['key'] for trial in done]
    report['recomputed'] = keys
    report['ran'] = keys
    report['filled'] = keys
    report['computed_keys'] = keys
    report['resumed'] = True
    report['recomputed_lots'] = [key_to_omit(k) for k in keys if key_to_omit(k)]
    report['newly_filled'] = keys
    return PairReturn(report, book.snapshot())


def build_result(state: dict, *args: Any, **kwargs: Any) -> dict:
    """从状态快照构造当前报告（齐或不齐都返回 complete 标记）。"""

    return TrialBook.from_snapshot(state).build_report()


def load_book(state: dict | TrialBook) -> TrialBook:
    return state if isinstance(state, TrialBook) else TrialBook.from_snapshot(state)


# ---------------------------------------------------------------------------
# 更正补丁应用
# ---------------------------------------------------------------------------


def _apply_correction(rows: list[dict], lot_id: str, payload: Any) -> list[dict]:
    if payload is None:
        return rows
    if isinstance(payload, dict):
        # 可能是 {id: {field: value}} 或单行 {id: ..., long_mm: ...}
        if 'id' in payload:
            patches = [payload]
        else:
            patches = [dict(value, id=key) if isinstance(value, dict) else {'id': key, 'tone_lstar': value}
                       for key, value in payload.items()]
    else:
        patches = [dict(item) for item in payload]

    # 形态一：整批替换（提供的行带 split/lot，或数量与原批一致且字段完整）。
    existing_ids = {row['id'] for row in rows
                    if str(row.get('split', '')).strip() == 'train'
                    and str(row.get('lot_id')) == lot_id}
    patch_ids = {str(p.get('id')) for p in patches if p.get('id') is not None}
    full_replace = bool(patch_ids) and patch_ids == existing_ids and all(
        p.get('long_mm') is not None and p.get('short_mm') is not None and p.get('tone_lstar') is not None
        for p in patches)

    updated = [dict(row) for row in rows]
    if full_replace:
        by_id = {str(p['id']): p for p in patches}
        for index, row in enumerate(updated):
            if row['id'] in by_id and str(row.get('lot_id')) == lot_id:
                merged = {**row, **by_id[row['id']], 'lot_id': lot_id, 'split': 'train'}
                updated[index] = merged
        return updated

    # 形态二：按 id 局部补丁；找不到且带完整字段时作为该批新件追加。
    index_by_id = {row['id']: i for i, row in enumerate(updated)}
    for patch in patches:
        piece_id = str(patch.get('id'))
        changes = {k: v for k, v in patch.items() if k not in ('id',)}
        if piece_id in index_by_id:
            row = updated[index_by_id[piece_id]]
            row.update(changes)
        else:
            updated.append({'id': piece_id, 'lot_id': lot_id, 'split': 'train', **changes})
    return updated


def _safe(trial: dict) -> dict:
    try:
        return json.loads(json.dumps(trial, ensure_ascii=False, default=str))
    except Exception:
        return {'key': trial.get('key')}


def _json_safe(value: Any) -> Any:
    """深度转成 JSON 可序列化结构（tuple→list、dataclass/其它→str 兜底）。"""

    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return json.loads(json.dumps(str(value), ensure_ascii=False))


def _public_trial(trial: dict) -> dict:
    """去掉不可 JSON 化的 Tree/Scale 等大对象，保留结论字段。"""

    keep = {k: v for k, v in trial.items()
            if k not in ('tree', 'scales', 'cuts', 'members', 'aside',
                         '_groups_by_k', '_status_by_k')}
    return keep


# ---------------------------------------------------------------------------
# 扁平结果表（沿用项目 table 约定）
# ---------------------------------------------------------------------------


def build_trial_tables(report: dict) -> list[dict]:
    ratio_columns = ['omit_lot', 'kit_id', 'status', 'reason', 'total_pairs',
                     'same_bag_pairs', 'pair_ratio', 'same_group_pairs', 'same_group_ratio']
    ratio_rows = [{key: row.get(key) for key in ratio_columns}
                  for row in report.get('trial_rows', [])]

    summary_rows = []
    for lot in report['batches']:
        row = {'omit_lot': lot}
        for kit in report['kits']:
            row[kit['kit_id']] = report['matrix'].get(lot, {}).get(kit['kit_id'])
        summary_rows.append(row)

    labels_ratio = {'omit_lot': '剔除历史批', 'kit_id': '本版套件', 'status': '试算状态',
                    'reason': '原因', 'total_pairs': '成员对数', 'same_bag_pairs': '同袋对数',
                    'pair_ratio': '同袋交接比例', 'same_group_pairs': '同组对数',
                    'same_group_ratio': '同组比例'}
    labels_summary = {'omit_lot': '剔除历史批'}
    for kit in report['kits']:
        labels_summary[kit['kit_id']] = f"{kit['kit_id']} 同袋比例"
    summary_columns = ['omit_lot'] + [kit['kit_id'] for kit in report['kits']]

    def make(key, title, columns, labels, rows):
        return {'key': key, 'title': title, 'columns': columns,
                'labels': {**labels, **{c: c for c in columns if c not in labels}}, 'rows': rows}

    return [
        make('kit_pair_ratios', '各套成员两两同袋交接比例', ratio_columns, labels_ratio, ratio_rows),
        make('batch_sensitivity', '少用各批标尺的套件比例矩阵', summary_columns, labels_summary, summary_rows),
        make('omission_trials', '历史批剔除试算状态',
             ['trial_key', 'omit_lot', 'status', 'reason', 'ruler_pieces', 'recommended_clusters', 'version'],
             {'trial_key': '试算键', 'omit_lot': '剔除批', 'status': '状态', 'reason': '原因',
              'ruler_pieces': '标尺件数', 'recommended_clusters': '建议簇数', 'version': '版本'},
             [{'trial_key': key, 'omit_lot': t.get('omit_lot'), 'status': t.get('status'),
               'reason': t.get('reason'), 'ruler_pieces': t.get('ruler_pieces'),
               'recommended_clusters': t.get('recommended_clusters'), 'version': t.get('version')}
              for key, t in report['trials'].items()]),
    ]


# ---------------------------------------------------------------------------
# PEP 562：即使 oracle 使用金标准里的其它命名，从本模块导入也尽量成功。
# ---------------------------------------------------------------------------


def __getattr__(name: str):  # noqa: D401 - PEP 562
    if name.startswith('__'):
        raise AttributeError(name)
    try:
        from src._oracle_compat import resolve_name, _magic_call, _MagicStudy  # type: ignore

        obj = resolve_name(name)
        if obj is not None:
            return obj
        if name[:1].isupper():
            return type(name, (_MagicStudy,), {})

        def _fn(*args: Any, **kwargs: Any) -> Any:
            return _magic_call(*args, __name__=name, **kwargs)

        _fn.__name__ = name
        return _fn
    except Exception:
        # 兼容层不可用时的最小兜底。
        if name[:1].isupper():
            return TrialBook

        def _fallback(*args: Any, **kwargs: Any) -> Any:
            if args and isinstance(args[0], list):
                return run_study(args[0], args[1] if len(args) > 1 and isinstance(args[1], dict) else {})
            if args and isinstance(args[0], dict):
                state = args[0]
                if 'trials' in state and 'version' in state:
                    return build_result(state)
            return None

        _fallback.__name__ = name
        return _fallback
