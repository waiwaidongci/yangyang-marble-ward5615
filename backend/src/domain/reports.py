"""Labeled, flat result tables shared by JSON and signed CSV exports."""

from .settings import FIELDS
from .ward import Tree

PIECE_FIELDS = {key: label for key, label in FIELDS.items() if key != 'split'}


def table(key: str, title: str, labels: dict[str, str], rows: list[dict]) -> dict:
    return {'key': key, 'title': title, 'columns': list(labels), 'labels': labels,
            'rows': [{field: row[field] for field in labels} for row in rows]}


def merge_rows(tree: Tree) -> list[dict]:
    return [{'step': step, 'node': merge.node, 'left': merge.left, 'right': merge.right,
             'left_first_piece': tree.nodes[merge.left].members[0], 'right_first_piece': tree.nodes[merge.right].members[0],
             'left_pieces': len(tree.nodes[merge.left].members), 'right_pieces': len(tree.nodes[merge.right].members),
             'ward_distance': tree.nodes[merge.node].height, 'delta_sse': merge.delta_sse,
             'node_sse': tree.nodes[merge.node].sse, 'partition_sse': merge.partition_sse,
             'clusters_after_merge': tree.leaves - step}
            for step, merge in enumerate(tree.merges, 1)]


def build_reports(tree: Tree, cuts: list[dict], members: list[dict], aside: list[dict], scales: list[dict]) -> list[dict]:
    return [
        table('cut_candidates', '切树套件候选比选', {
            'clusters': '切树簇数', 'kits_ready': '可交接套数', 'pieces_ready': '可交接件数',
            'pieces_held': '暂缓件数', 'within_sse': '切树后总簇内 SSE', 'last_included_distance': '已纳入的最后合并距离',
            'next_merge_distance': '下一次合并距离', 'min_kit_pieces': '每套最少件数',
            'max_ward_distance': '套件距离上限', 'recommended': '建议切法'}, cuts),
        table('kit_members', '各切树候选套件成员', {
            'clusters': '切树簇数', 'group_id': '候选组编号', 'node': '树节点号', **PIECE_FIELDS,
            'group_pieces': '组内件数', 'group_sse': '簇内 SSE', 'group_ward_distance': '簇根合并距离',
            'kit_status': '交接状态', 'reason': '暂缓原因'}, members),
        table('ward_merges', 'Ward 层次与逐次合并代价', {
            'step': '合并步号', 'node': '合并节点号', 'left': '左子节点号', 'right': '右子节点号',
            'left_first_piece': '左簇最先编号', 'right_first_piece': '右簇最先编号', 'left_pieces': '左簇件数',
            'right_pieces': '右簇件数', 'ward_distance': 'Ward 合并距离', 'delta_sse': '此次合并 SSE 增量',
            'node_sse': '新簇 SSE', 'partition_sse': '合并后全台账簇内 SSE',
            'clusters_after_merge': '合并后簇数'}, merge_rows(tree)),
        table('scaling', '历史冻结标尺与留出批漂移', {
            'feature': '字段名', 'reference_count': '历史标尺件数', 'reference_mean': '历史均值',
            'reference_population_sd': '历史总体标准差', 'scale_floor': '有效尺度下限',
            'effective_scale': '有效尺度', 'coordinate_weight': '标准化坐标倍率', 'holdout_count': '留出件数',
            'holdout_mean': '留出均值', 'holdout_shift_sd': '留出均值偏移（有效尺度单位）'}, scales),
        table('set_aside', '尺寸不足单件搁置清单', {
            **PIECE_FIELDS, 'min_long_mm': '最小长边（mm）', 'min_short_mm': '最小短边（mm）', 'reason': '搁置原因'}, aside),
    ]
