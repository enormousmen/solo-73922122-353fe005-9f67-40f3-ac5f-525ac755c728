"""Same/different determinations -> deterministic groupings.

A group is exactly a connected component of accepted "same" determinations.
"Different" determinations are constraints: their two endpoints may never end
up in the same component.  Accepted state is always kept satisfiable, so a
component can simply be derived from the "same" edges.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

REL_SAME = "same"
REL_DIFF = "diff"


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def make_group_key(record_ids: Iterable[int]) -> str:
    ordered = sorted(int(rid) for rid in record_ids)
    digest = hashlib.sha256(",".join(map(str, ordered)).encode("utf-8")).hexdigest()
    return "G-" + digest[:16]


@dataclass(frozen=True)
class Grouping:
    by_key: Mapping[str, tuple[int, ...]]
    group_of: Mapping[int, str]

    def members_of(self, record_id: int) -> tuple[int, ...]:
        return self.by_key[self.group_of[record_id]]

    def key_of(self, record_id: int) -> str:
        return self.group_of[record_id]


def build_grouping(record_ids: Sequence[int], decisions: Sequence[Mapping]) -> Grouping:
    parent: dict[int, int] = {int(rid): int(rid) for rid in record_ids}

    def find(node: int) -> int:
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != node:
            next_node = parent[node]
            parent[node] = root
            node = next_node
        return root

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            # Deterministic root choice keeps grouping stable.
            if root_left < root_right:
                parent[root_right] = root_left
            else:
                parent[root_left] = root_right

    for decision in decisions:
        if decision["relation"] == REL_SAME:
            union(int(decision["record_a"]), int(decision["record_b"]))

    raw_components: dict[int, list[int]] = {}
    for rid in parent:
        raw_components.setdefault(find(rid), []).append(rid)

    by_key: dict[str, tuple[int, ...]] = {}
    group_of: dict[int, str] = {}
    for members in raw_components.values():
        members_sorted = tuple(sorted(members))
        key = make_group_key(members_sorted)
        by_key[key] = members_sorted
        for rid in members_sorted:
            group_of[rid] = key

    return Grouping(by_key=by_key, group_of=group_of)


def invalid_diff_decisions(decisions: Sequence[Mapping], grouping: Grouping) -> list[Mapping]:
    """A defensively reported invariant break; accepted state never has one."""
    broken = []
    for decision in decisions:
        if decision["relation"] != REL_DIFF:
            continue
        left = int(decision["record_a"])
        right = int(decision["record_b"])
        if grouping.group_of.get(left) == grouping.group_of.get(right):
            broken.append(decision)
    return broken


def find_same_path(
    decisions: Sequence[Mapping], a: int, b: int
) -> list[Mapping]:
    """Shortest chain of accepted "same" decisions connecting a and b."""
    if a == b:
        return []
    adjacency: dict[int, list[tuple[int, Mapping]]] = {}
    for decision in decisions:
        if decision["relation"] != REL_SAME:
            continue
        left, right = int(decision["record_a"]), int(decision["record_b"])
        adjacency.setdefault(left, []).append((right, decision))
        adjacency.setdefault(right, []).append((left, decision))

    queue: deque[int] = deque([a])
    previous: dict[int, tuple[int, Mapping] | None] = {a: None}
    while queue:
        node = queue.popleft()
        if node == b:
            break
        for neighbour, decision in adjacency.get(node, []):
            if neighbour not in previous:
                previous[neighbour] = (node, decision)
                queue.append(neighbour)

    if b not in previous:
        return []
    path: list[Mapping] = []
    cursor = b
    while previous[cursor] is not None:
        parent, decision = previous[cursor]  # type: ignore[misc]
        path.append(decision)
        cursor = parent
    path.reverse()
    return path


def crossing_diff_decisions(
    decisions: Sequence[Mapping], grouping: Grouping, key_a: str, key_b: str
) -> list[Mapping]:
    """Diff determinations joined across the two components that would merge."""
    if key_a == key_b:
        return []
    wanted = {key_a, key_b}
    crossing: list[Mapping] = []
    for decision in decisions:
        if decision["relation"] != REL_DIFF:
            continue
        left = int(decision["record_a"])
        right = int(decision["record_b"])
        left_key = grouping.group_of.get(left)
        right_key = grouping.group_of.get(right)
        if {left_key, right_key} == wanted:
            crossing.append(decision)
    return crossing
