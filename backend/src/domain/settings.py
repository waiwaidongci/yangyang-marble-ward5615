"""CSV fields, project metadata and immutable per-run settings."""

from math import isfinite

FIELDS = {
    'id': '边角料编号', 'lot_id': '来源批次', 'split': '用途',
    'long_mm': '外接矩形长边（mm）', 'short_mm': '外接矩形短边（mm）', 'tone_lstar': '色卡明度 L*',
}
FEATURES = ('long_mm', 'short_mm', 'tone_lstar')
SPLITS = ('train', 'holdout', 'pending')
MAX_PENDING = 240
SETTINGS = [
    {'key': 'min_clusters', 'label': '最少切树簇数', 'default': 3, 'min': 1, 'max': 16, 'step': 1},
    {'key': 'max_clusters', 'label': '最多切树簇数', 'default': 6, 'min': 1, 'max': 16, 'step': 1},
    {'key': 'min_kit_pieces', 'label': '每套最少件数', 'default': 4, 'min': 2, 'max': 24, 'step': 1},
    {'key': 'min_long_mm', 'label': '单件最小长边（mm）', 'default': 85, 'min': 1, 'max': 2000, 'step': 1},
    {'key': 'min_short_mm', 'label': '单件最小短边（mm）', 'default': 55, 'min': 1, 'max': 2000, 'step': 1},
    {'key': 'tone_weight', 'label': '明度标准化坐标倍率', 'default': 1.25, 'min': 0.1, 'max': 5, 'step': 0.05},
    {'key': 'max_ward_distance', 'label': '套件最大 Ward 合并距离', 'default': 1.5,
     'min': 0.01, 'max': 100, 'step': 0.01},
]
SPEC = {
    'columns': list(FIELDS), 'fields': FIELDS, 'settings': SETTINGS,
    'modules': {'datasets': '边角料测量台账', 'runs': '缩放冻结与 Ward 层次',
                'results': '切树比选与套件成员', 'reviews': '分拣交班签核'},
    'dataset_name': '20261125-河埠石材边角料.csv',
    'limitation': '每份台账最多 240 件待分拣料。明度与外接矩形仅用于初筛，备料前复测可用边与板面瑕疵。',
    'recommendation': '比较各切树候选与搁置清单，复核后将同一候选的套件成员表交给拣料员。',
}


def parse_settings(raw: dict) -> dict:
    if not isinstance(raw, dict) or raw.keys() - {item['key'] for item in SETTINGS}:
        raise ValueError('存在本项目未定义的运行参数。')
    parsed = {}
    for item in SETTINGS:
        key = item['key']
        value = raw.get(key, item['default'])
        if type(value) not in (int, float) or not item['min'] <= value <= item['max'] or not isfinite(value):
            raise ValueError(f"{item['label']}须在 {item['min']} 至 {item['max']} 之间。")
        if item['step'] == 1:
            if value != int(value):
                raise ValueError(f"{item['label']}须为整数。")
            value = int(value)
        parsed[key] = value
    if parsed['min_clusters'] > parsed['max_clusters']:
        raise ValueError('最少切树簇数须小于或等于最多切树簇数。')
    return parsed
