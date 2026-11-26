"""Exact greedy Ward tree with deterministic node IDs and SSE accounting."""

from dataclasses import dataclass
from heapq import heapify, heappop, heappush
from itertools import combinations
from math import dist, sqrt


@dataclass(frozen=True)
class Node:
    members: tuple[str, ...]
    centroid: tuple[float, ...]
    sse: float = 0.0
    height: float = 0.0


@dataclass(frozen=True)
class Merge:
    node: int
    left: int
    right: int
    delta_sse: float
    partition_sse: float


@dataclass(frozen=True)
class Tree:
    leaves: int
    nodes: tuple[Node, ...]
    merges: tuple[Merge, ...]

    def cut(self, clusters: int) -> list[int]:
        active = set(range(self.leaves))
        for merge in self.merges[:self.leaves - clusters]:
            active.difference_update((merge.left, merge.right))
            active.add(merge.node)
        return sorted(active, key=lambda index: self.nodes[index].members[0])


def merge_cost(a: Node, b: Node) -> float:
    n, m = len(a.members), len(b.members)
    return n * m / (n + m) * dist(a.centroid, b.centroid) ** 2


def build_tree(points: dict[str, tuple[float, ...]]) -> Tree:
    nodes = [Node((key,), points[key]) for key in sorted(points)]
    leaves = len(nodes)
    active = set(range(leaves))
    heap = [(merge_cost(nodes[left], nodes[right]), left, right) for left, right in combinations(range(leaves), 2)]
    heapify(heap)
    merges = []
    total_sse = 0.0
    while len(active) > 1:
        delta, left, right = heappop(heap)
        if left not in active or right not in active:
            continue
        a, b = nodes[left], nodes[right]
        n, m = len(a.members), len(b.members)
        center = tuple((n * x + m * y) / (n + m) for x, y in zip(a.centroid, b.centroid))
        index = len(nodes)
        node = Node(tuple(sorted(a.members + b.members)), center, a.sse + b.sse + delta, sqrt(2 * delta))
        nodes.append(node)
        total_sse += delta
        merges.append(Merge(index, left, right, delta, total_sse))
        active.difference_update((left, right))
        for other in active:
            heappush(heap, (merge_cost(nodes[other], node), other, index))
        active.add(index)
    return Tree(leaves, tuple(nodes), tuple(merges))
