"""Compare exact-K cuts and mark clusters that meet workshop kit thresholds."""

from math import fsum

from .ward import Tree


def compare_cuts(tree: Tree, options: dict, eligible: list[dict]) -> tuple[list[dict], list[dict], int | None]:
    if not tree.leaves:
        return [], [], None
    low = min(options['min_clusters'], tree.leaves)
    high = min(options['max_clusters'], tree.leaves)
    sources = {row['id']: row for row in eligible}
    candidates = []
    members = []
    for clusters in range(low, high + 1):
        included = tree.leaves - clusters
        nodes = tree.cut(clusters)
        kits = 0
        packed = 0
        for number, index in enumerate(nodes, 1):
            node = tree.nodes[index]
            count = len(node.members)
            failures = []
            if count < options['min_kit_pieces']:
                failures.append('below_min_kit_pieces')
            if node.height > options['max_ward_distance']:
                failures.append('above_max_ward_distance')
            if not failures:
                kits += 1
                packed += count
            for piece in node.members:
                members.append({**sources[piece], 'clusters': clusters, 'group_id': f'K{clusters:02}-G{number:02}',
                                'node': index, 'group_pieces': count, 'group_sse': node.sse,
                                'group_ward_distance': node.height, 'kit_status': 'hold' if failures else 'ready',
                                'reason': '+'.join(failures)})
        candidates.append({'clusters': clusters, 'kits_ready': kits, 'pieces_ready': packed,
                           'pieces_held': tree.leaves - packed, 'within_sse': fsum(tree.nodes[i].sse for i in nodes),
                           'last_included_distance': tree.nodes[tree.merges[included - 1].node].height if included else None,
                           'next_merge_distance': tree.nodes[tree.merges[included].node].height if clusters > 1 else None,
                           'min_kit_pieces': options['min_kit_pieces'], 'max_ward_distance': options['max_ward_distance']})
    best = max(candidates, key=lambda cut: (cut['pieces_ready'], -cut['clusters']))
    recommended = best['clusters'] if best['pieces_ready'] else None
    for candidate in candidates:
        candidate['recommended'] = candidate['clusters'] == recommended
    return candidates, members, recommended
