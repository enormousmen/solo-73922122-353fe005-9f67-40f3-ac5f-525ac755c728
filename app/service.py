"""Review workflow: preview, revalidate on confirm, and atomic persistence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import Counter
from typing import Any, Mapping, Sequence

from . import db, writeoff
from .common import ApiProblem, utc_now
from .grouping import (
    REL_DIFF,
    REL_SAME,
    Grouping,
    build_grouping,
    canonical_json,
    crossing_diff_decisions,
    find_same_path,
    invalid_diff_decisions,
)

PREVIEW_PENDING = "pending"
PREVIEW_CONFIRMED = "confirmed"
PREVIEW_STALE = "stale"


def normalize_pair(left: int, right: int) -> tuple[int, int]:
    return (left, right) if left < right else (right, left)


def load_records(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, seq, source, source_code, name FROM records ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


def load_decisions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, record_a, record_b, relation, created_at "
        "FROM decisions ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


def record_brief(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": int(record["id"]),
        "source": record["source"],
        "source_code": record["source_code"],
        "name": record["name"],
    }


def decision_view(
    decision: Mapping[str, Any],
    by_id: Mapping[int, Mapping[str, Any]],
    tentative: bool = False,
) -> dict[str, Any]:
    return {
        "id": decision.get("id"),
        "relation": decision["relation"],
        "record_a": record_brief(by_id[int(decision["record_a"])]),
        "record_b": record_brief(by_id[int(decision["record_b"])]),
        "created_at": decision.get("created_at"),
        "tentative": tentative,
    }


def decisions_touching(
    decisions: Sequence[Mapping[str, Any]], involved: set[int]
) -> list[Mapping[str, Any]]:
    return [
        decision
        for decision in decisions
        if int(decision["record_a"]) in involved or int(decision["record_b"]) in involved
    ]


def make_snapshot(
    involved_ids: set[int],
    grouping: Grouping,
    decisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The revalidation contract.

    It deliberately contains only the records in the groups involved in the
    pending operation and determinations touching those records.  A change to
    an unrelated group therefore cannot affect confirmation.
    """
    group_keys = sorted({grouping.group_of[rid] for rid in involved_ids})
    return {
        "involved_ids": sorted(involved_ids),
        "record_group": {
            str(rid): grouping.group_of[rid] for rid in sorted(involved_ids)
        },
        "group_members": {
            key: sorted(grouping.by_key[key]) for key in group_keys
        },
        "associated_decisions": sorted(
            [
                {
                    "id": int(decision["id"]),
                    "record_a": int(decision["record_a"]),
                    "record_b": int(decision["record_b"]),
                    "relation": decision["relation"],
                }
                for decision in decisions_touching(decisions, involved_ids)
            ],
            key=lambda item: item["id"],
        ),
    }


def snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()[:20]


def _group_labels(grouping: Grouping, involved: set[int], prefix: str) -> dict[str, str]:
    keys = sorted(
        {grouping.group_of[rid] for rid in involved},
        key=lambda key: (len(grouping.by_key[key]), grouping.by_key[key]),
    )
    return {key: f"{prefix}{index + 1}" for index, key in enumerate(keys)}


def _group_payloads(
    grouping: Grouping,
    labels: Mapping[str, str],
    by_id: Mapping[int, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    payloads = []
    for key in sorted(labels, key=lambda item: (len(grouping.by_key[item]), grouping.by_key[item])):
        members = [
            record_brief(by_id[rid]) for rid in grouping.by_key[key]
        ]
        payloads.append(
            {
                "key": key,
                "label": labels[key],
                "size": len(members),
                "members": members,
            }
        )
    return payloads


def _change_payloads(
    before: Grouping,
    after: Grouping,
    involved: set[int],
    before_labels: Mapping[str, str],
    after_labels: Mapping[str, str],
    by_id: Mapping[int, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    changes = []
    for rid in sorted(involved):
        before_key = before.group_of[rid]
        after_key = after.group_of[rid]
        changes.append(
            {
                "record": record_brief(by_id[rid]),
                "before_group": before_key,
                "before_label": before_labels[before_key],
                "before_group_size": len(before.by_key[before_key]),
                "after_group": after_key,
                "after_label": after_labels[after_key],
                "after_group_size": len(after.by_key[after_key]),
                "changed": before_key != after_key,
            }
        )
    return changes


def _summary_for(
    action: Mapping[str, Any],
    before: Grouping,
    after: Grouping,
    involved: set[int],
    by_id: Mapping[int, Mapping[str, Any]],
) -> str:
    before_count = len({before.group_of[rid] for rid in involved})
    after_count = len({after.group_of[rid] for rid in involved})
    kind = action["kind"]

    if kind == "withdraw":
        if action.get("relation") == REL_SAME:
            if after_count > before_count:
                return (
                    f"撤回该“同一企业”判定后，所涉记录将由 {before_count} 个分组"
                    f"拆分为 {after_count} 个分组；其余判定继续生效。"
                )
            return "撤回该“同一企业”判定；其余同一企业关系仍使所涉记录保持同组。"
        return "撤回该“不同企业”约束；当前分组不变，此约束不再阻止两端合并。"

    left = int(action["record_a"])
    right = int(action["record_b"])
    left_name = by_id[left]["name"]
    right_name = by_id[right]["name"]
    if action["relation"] == REL_SAME:
        if before.group_of[left] == before.group_of[right]:
            return (
                "新增“同一企业”判定；两端已通过既有判定处于同一分组，"
                "分组不变，判定依据增加。"
            )
        return (
            f"将“{left_name}”与“{right_name}”判定为同一企业，"
            f"合并 2 个分组（共 {len(involved)} 条记录）。"
        )
    return (
        f"将“{left_name}”与“{right_name}”判定为不同企业；"
        "分组不变，并新增“两端不得同组”的约束。"
    )


def _conflict(
    code: str,
    message: str,
    evidence: Sequence[Mapping[str, Any]],
    by_id: Mapping[int, Mapping[str, Any]],
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "evidence": [decision_view(item, by_id) for item in evidence],
    }
    if extra:
        payload.update(extra)
    return payload


def _writeoff_crossing_conflict(
    conn: sqlite3.Connection, grouping: Grouping
) -> dict[str, Any] | None:
    """Conflict payload if the proposed grouping tears write-offs apart.

    Both the relation-change preview and its confirmation run this check
    against the grouping that would result: any existing write-off whose
    order-side and invoice-side suppliers would land in different groups
    blocks the change and names the schemes involved.
    """
    crossing = writeoff.cross_group_entries(
        grouping,
        writeoff.load_orders(conn),
        writeoff.load_invoices(conn),
        writeoff.load_writeoffs(conn),
    )
    if not crossing:
        return None
    schemes = sorted({item["scheme_id"] for item in crossing})
    return {
        "code": "writeoff_crosses_groups",
        "message": (
            f"该关系变更会使 {len(crossing)} 条已保存核销的双方供应商落入不同分组"
            f"（涉及方案：{'、'.join(schemes)}）。"
            "请先在核销页解除相关核销，再重新预览该关系变更。"
        ),
        "evidence": [],
        "schemes": schemes,
        "writeoffs": crossing,
    }


def build_state(conn: sqlite3.Connection, instance: str) -> dict[str, Any]:
    with db.read_tx(conn):
        records = load_records(conn)
        decisions = load_decisions(conn)
        by_id = {int(record["id"]): record for record in records}
        grouping = build_grouping([int(r["id"]) for r in records], decisions)
        broken = invalid_diff_decisions(decisions, grouping)

        groups = []
        for key, ids in grouping.by_key.items():
            members = [record_brief(by_id[rid]) for rid in ids]
            name_counts = Counter(by_id[rid]["name"] for rid in ids)
            subject = max(name_counts.items(), key=lambda item: (item[1], item[0]))[0]
            same_basis = [
                decision_view(decision, by_id)
                for decision in decisions
                if decision["relation"] == REL_SAME
                and grouping.group_of[int(decision["record_a"])] == key
                and grouping.group_of[int(decision["record_b"])] == key
            ]
            diff_links = []
            for decision in decisions:
                if decision["relation"] != REL_DIFF:
                    continue
                left, right = int(decision["record_a"]), int(decision["record_b"])
                left_in = grouping.group_of[left] == key
                right_in = grouping.group_of[right] == key
                if left_in != right_in:
                    other_id = right if left_in else left
                    view = decision_view(decision, by_id)
                    view["other_record"] = record_brief(by_id[other_id])
                    view["other_group"] = grouping.group_of[other_id]
                    diff_links.append(view)
            groups.append(
                {
                    "key": key,
                    "size": len(members),
                    "members": members,
                    "suggested_subject": subject,
                    "same_basis": same_basis,
                    "diff_links": diff_links,
                }
            )

        # Show merged groups first, then singletons; deterministic ordering.
        groups.sort(
            key=lambda group: (
                -group["size"],
                min(member["name"] for member in group["members"]),
                group["key"],
            )
        )

        decision_payloads = []
        for decision in decisions:
            view = decision_view(decision, by_id)
            view["group_a"] = grouping.group_of[int(decision["record_a"])]
            view["group_b"] = grouping.group_of[int(decision["record_b"])]
            view["same_group"] = view["group_a"] == view["group_b"]
            decision_payloads.append(view)

        # Same-name pairs are the primary review queue.
        by_name: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            by_name.setdefault(record["name"], []).append(record)
        direct: dict[tuple[int, int], Mapping[str, Any]] = {
            (int(d["record_a"]), int(d["record_b"])): d for d in decisions
        }
        pairs = []
        for name, same_name_records in by_name.items():
            ordered = sorted(same_name_records, key=lambda r: int(r["id"]))
            for index, left in enumerate(ordered):
                for right in ordered[index + 1 :]:
                    pair = normalize_pair(int(left["id"]), int(right["id"]))
                    existing = direct.get(pair)
                    pairs.append(
                        {
                            "name": name,
                            "record_a": record_brief(left),
                            "record_b": record_brief(right),
                            "decision": decision_view(existing, by_id) if existing else None,
                            "same_group": grouping.group_of[pair[0]]
                            == grouping.group_of[pair[1]],
                            "group_a": grouping.group_of[pair[0]],
                            "group_b": grouping.group_of[pair[1]],
                        }
                    )
        pairs.sort(
            key=lambda pair: (
                pair["decision"] is not None,
                pair["same_group"],
                pair["name"],
                pair["record_a"]["id"],
            )
        )

        return {
            "instance": instance,
            "generated_at": utc_now(),
            "records": [record_brief(record) for record in records],
            "groups": groups,
            "decisions": decision_payloads,
            "same_name_pairs": pairs,
            "stats": {
                "records": len(records),
                "groups": len(groups),
                "merged_groups": sum(1 for group in groups if group["size"] > 1),
                "same_decisions": sum(1 for d in decisions if d["relation"] == REL_SAME),
                "diff_decisions": sum(1 for d in decisions if d["relation"] == REL_DIFF),
            },
            "invariant_violations": [
                decision_view(item, by_id) for item in broken
            ],
        }


def create_preview_add(
    conn: sqlite3.Connection, record_a: int, record_b: int, relation: str
) -> dict[str, Any]:
    if relation not in (REL_SAME, REL_DIFF):
        raise ApiProblem(400, "invalid_relation", "relation 必须是 same 或 diff")
    if record_a == record_b:
        raise ApiProblem(400, "invalid_pair", "不能对同一条记录添加判定")
    action = {
        "kind": "add",
        "record_a": min(record_a, record_b),
        "record_b": max(record_a, record_b),
        "relation": relation,
    }
    return _create_preview(conn, action)


def create_preview_withdraw(conn: sqlite3.Connection, decision_id: int) -> dict[str, Any]:
    return _create_preview(conn, {"kind": "withdraw", "decision_id": decision_id})


def _create_preview(conn: sqlite3.Connection, action: dict[str, Any]) -> dict[str, Any]:
    with db.write_tx(conn):
        records = load_records(conn)
        by_id = {int(record["id"]): record for record in records}
        decisions = load_decisions(conn)
        all_ids = list(by_id)
        grouping = build_grouping(all_ids, decisions)
        broken = invalid_diff_decisions(decisions, grouping)
        if broken:
            raise ApiProblem(
                500,
                "inconsistent_state",
                "数据库中的判定已自相矛盾，请联系管理员检查。",
                invalid_decisions=[decision_view(item, by_id) for item in broken],
            )

        tentative: list[dict[str, Any]] = list(decisions)
        if action["kind"] == "add":
            left, right = int(action["record_a"]), int(action["record_b"])
            if left not in by_id or right not in by_id:
                raise ApiProblem(400, "unknown_record", "记录不存在")
            pair = normalize_pair(left, right)
            existing = next(
                (
                    decision
                    for decision in decisions
                    if normalize_pair(
                        int(decision["record_a"]), int(decision["record_b"])
                    )
                    == pair
                ),
                None,
            )
            if existing is not None:
                if existing["relation"] == action["relation"]:
                    conflict = _conflict(
                        "pair_decision_exists",
                        "该记录对已经存在相同的判定，请勿重复添加。",
                        [existing],
                        by_id,
                    )
                elif action["relation"] == REL_SAME:
                    conflict = _conflict(
                        "same_conflicts_with_diff",
                        "无法判定为同一企业：该记录对已有“不同企业”判定。",
                        [existing],
                        by_id,
                    )
                else:
                    path = find_same_path(decisions, left, right)
                    conflict = _conflict(
                        "diff_within_same_group",
                        "无法判定为不同企业：该记录对已有“同一企业”判定。",
                        path or [existing],
                        by_id,
                        extra={
                            "same_path": [
                                decision_view(item, by_id) for item in (path or [existing])
                            ]
                        },
                    )
                return _conflict_response(action, conflict, by_id)

            key_left = grouping.group_of[left]
            key_right = grouping.group_of[right]
            involved = set(grouping.by_key[key_left]) | set(grouping.by_key[key_right])

            if action["relation"] == REL_SAME:
                if key_left != key_right:
                    crossing = crossing_diff_decisions(
                        decisions, grouping, key_left, key_right
                    )
                    if crossing:
                        conflict = _conflict(
                            "same_conflicts_with_diff",
                            "无法判定为同一企业：待合并的两个分组之间存在“不同企业”判定，"
                            "合并会使该约束的两端落入同一分组。",
                            crossing,
                            by_id,
                        )
                        return _conflict_response(action, conflict, by_id)
            else:
                if key_left == key_right:
                    path = find_same_path(decisions, left, right)
                    conflict = _conflict(
                        "diff_within_same_group",
                        "无法判定为不同企业：两条记录已通过“同一企业”判定归入同一分组。",
                        path,
                        by_id,
                        extra={"same_path": [decision_view(item, by_id) for item in path]},
                    )
                    return _conflict_response(action, conflict, by_id)

            tentative.append(
                {
                    "id": None,
                    "record_a": pair[0],
                    "record_b": pair[1],
                    "relation": action["relation"],
                    "created_at": None,
                }
            )
        else:
            decision_id = int(action["decision_id"])
            existing = next(
                (decision for decision in decisions if int(decision["id"]) == decision_id),
                None,
            )
            if existing is None:
                raise ApiProblem(404, "unknown_decision", "要撤回的判定不存在")
            action["relation"] = existing["relation"]
            action["record_a"] = int(existing["record_a"])
            action["record_b"] = int(existing["record_b"])
            left, right = int(existing["record_a"]), int(existing["record_b"])
            involved = set(grouping.by_key[grouping.group_of[left]]) | set(
                grouping.by_key[grouping.group_of[right]]
            )
            tentative = [
                decision
                for decision in decisions
                if int(decision["id"]) != decision_id
            ]

        after = build_grouping(all_ids, tentative)
        broken_after = invalid_diff_decisions(tentative, after)
        if broken_after:
            raise ApiProblem(
                500,
                "inconsistent_operation",
                "该操作会产生自相矛盾的分组，已拒绝。",
            )

        # The proposed grouping must keep every existing write-off's two
        # suppliers inside one group; otherwise the change is refused here.
        writeoff_conflict = _writeoff_crossing_conflict(conn, after)
        if writeoff_conflict is not None:
            return _conflict_response(action, writeoff_conflict, by_id, decisions)

        involved_set = set(involved)
        snapshot = make_snapshot(involved_set, grouping, decisions)
        digest = snapshot_digest(snapshot)
        before_labels = _group_labels(grouping, involved_set, "前组")
        after_labels = _group_labels(after, involved_set, "后组")

        changes = _change_payloads(
            grouping, after, involved_set, before_labels, after_labels, by_id
        )
        basis_before = [
            decision_view(decision, by_id)
            for decision in decisions_touching(decisions, involved_set)
        ]
        basis_after = [
            decision_view(decision, by_id, tentative=decision.get("id") is None)
            for decision in decisions_touching(tentative, involved_set)
        ]

        summary = _summary_for(action, grouping, after, involved_set, by_id)

        if action["kind"] == "add":
            operation_view = {
                "kind": "add",
                "relation": action["relation"],
                "record_a": record_brief(by_id[int(action["record_a"])]),
                "record_b": record_brief(by_id[int(action["record_b"])]),
            }
        else:
            decision = next(
                decision for decision in decisions
                if int(decision["id"]) == int(action["decision_id"])
            )
            operation_view = {
                "kind": "withdraw",
                "decision": decision_view(decision, by_id),
            }

        preview_id = uuid.uuid4().hex
        created_at = utc_now()
        conn.execute(
            "INSERT INTO previews "
            "(id, created_at, action_json, snapshot_json, digest, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                preview_id,
                created_at,
                canonical_json(action),
                canonical_json(snapshot),
                digest,
                PREVIEW_PENDING,
            ),
        )

        return {
            "ok": True,
            "preview": {
                "preview_id": preview_id,
                "created_at": created_at,
                "snapshot_digest": digest,
                "operation": operation_view,
                "action": action,
                "confirmable": True,
                "conflict": None,
                "summary": summary,
                "affected_ids": sorted(involved_set),
                "changes": changes,
                "groups_before": _group_payloads(
                    grouping, before_labels, by_id
                ),
                "groups_after": _group_payloads(
                    after, after_labels, by_id
                ),
                "basis_before": basis_before,
                "basis_after": basis_after,
            },
        }


def _conflict_response(
    action: Mapping[str, Any],
    conflict: Mapping[str, Any],
    by_id: Mapping[int, Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    if action["kind"] == "add":
        operation = {
            "kind": "add",
            "relation": action["relation"],
            "record_a": record_brief(by_id[int(action["record_a"])]),
            "record_b": record_brief(by_id[int(action["record_b"])]),
        }
    else:
        decision = next(
            (
                item
                for item in decisions
                if int(item["id"]) == int(action.get("decision_id", -1))
            ),
            None,
        )
        operation = {
            "kind": "withdraw",
            "decision": decision_view(decision, by_id) if decision else None,
        }
    return {
        "ok": True,
        "preview": {
            "preview_id": None,
            "created_at": utc_now(),
            "snapshot_digest": None,
            "operation": operation,
            "action": action,
            "confirmable": False,
            "conflict": conflict,
            "summary": conflict["message"],
            "affected_ids": [],
            "changes": [],
            "groups_before": [],
            "groups_after": [],
            "basis_before": [],
            "basis_after": [],
        },
    }


def _recheck_operation(
    action: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    grouping: Grouping,
    by_id: Mapping[int, Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Return a conflict payload if the action stopped being valid."""
    if action["kind"] == "add":
        left, right = int(action["record_a"]), int(action["record_b"])
        if left not in by_id or right not in by_id:
            return _conflict(
                "unknown_record", "记录不存在，操作已失效。", [], by_id
            )
        pair = normalize_pair(left, right)
        existing = next(
            (
                decision
                for decision in decisions
                if normalize_pair(
                    int(decision["record_a"]), int(decision["record_b"])
                )
                == pair
            ),
            None,
        )
        if existing is not None:
            return _conflict(
                "pair_decision_exists",
                "该记录对已经存在判定，请重新预览。",
                [existing],
                by_id,
            )
        if action["relation"] == REL_SAME:
            key_left = grouping.group_of[left]
            key_right = grouping.group_of[right]
            if key_left != key_right:
                crossing = crossing_diff_decisions(
                    decisions, grouping, key_left, key_right
                )
                if crossing:
                    return _conflict(
                        "same_conflicts_with_diff",
                        "合并会违反“不同企业”约束，请重新预览。",
                        crossing,
                        by_id,
                    )
        else:
            if grouping.group_of[left] == grouping.group_of[right]:
                path = find_same_path(decisions, left, right)
                return _conflict(
                    "diff_within_same_group",
                    "两条记录已处于同一分组，请重新预览。",
                    path,
                    by_id,
                    extra={
                        "same_path": [
                            decision_view(item, by_id) for item in path
                        ]
                    },
                )
    else:
        decision_id = int(action["decision_id"])
        if not any(int(decision["id"]) == decision_id for decision in decisions):
            return _conflict(
                "unknown_decision", "要撤回的判定已不存在，请重新预览。", [], by_id
            )
    return None


def confirm_preview(
    conn: sqlite3.Connection, preview_id: str, client_digest: str | None = None
) -> tuple[int, dict[str, Any]]:
    """Revalidate and atomically persist. Returns (HTTP status, body)."""
    with db.write_tx(conn):
        row = conn.execute(
            "SELECT id, action_json, snapshot_json, digest, status, created_at "
            "FROM previews WHERE id = ?",
            (preview_id,),
        ).fetchone()
        if row is None:
            raise ApiProblem(404, "unknown_preview", "预览不存在，请重新预览。")

        if row["status"] != PREVIEW_PENDING:
            return 409, {
                "ok": False,
                "error": {
                    "code": "preview_not_pending",
                    "message": "该预览已确认或已过期，请重新预览。",
                    "status": row["status"],
                },
                "preview_id": preview_id,
            }

        if client_digest is not None and client_digest != row["digest"]:
            conn.execute(
                "UPDATE previews SET status = ? WHERE id = ?",
                (PREVIEW_STALE, preview_id),
            )
            return 409, {
                "ok": False,
                "error": {
                    "code": "preview_stale",
                    "message": "预览摘要与页面不一致，请重新预览。",
                },
                "preview_id": preview_id,
            }

        action = json.loads(row["action_json"])
        stored_snapshot = json.loads(row["snapshot_json"])
        involved = {int(rid) for rid in stored_snapshot["involved_ids"]}

        records = load_records(conn)
        by_id = {int(record["id"]): record for record in records}
        decisions = load_decisions(conn)
        grouping = build_grouping(list(by_id), decisions)

        current_snapshot = make_snapshot(involved, grouping, decisions)
        if canonical_json(current_snapshot) != canonical_json(stored_snapshot):
            conn.execute(
                "UPDATE previews SET status = ? WHERE id = ?",
                (PREVIEW_STALE, preview_id),
            )
            return 409, {
                "ok": False,
                "error": {
                    "code": "preview_stale",
                    "message": (
                        "预览已过期：所涉原组成员或这些成员关联的判定在预览后发生变化，"
                        "请重新预览后再确认。"
                    ),
                },
                "preview_id": preview_id,
                "current_digest": snapshot_digest(current_snapshot),
                "preview_digest": row["digest"],
            }

        conflict = _recheck_operation(action, records, decisions, grouping, by_id)
        if conflict is not None:
            conn.execute(
                "UPDATE previews SET status = ? WHERE id = ?",
                (PREVIEW_STALE, preview_id),
            )
            return 409, {
                "ok": False,
                "error": {
                    "code": conflict["code"],
                    "message": conflict["message"],
                    "evidence": conflict.get("evidence", []),
                },
                "preview_id": preview_id,
            }

        # Compute the would-be grouping before writing anything, so every
        # remaining check can still refuse without touching state.
        remaining = [
            decision
            for decision in decisions
            if not (
                action["kind"] == "withdraw"
                and int(decision["id"]) == int(action["decision_id"])
            )
        ]
        if action["kind"] == "add":
            remaining.append(
                {
                    "id": None,
                    "record_a": normalize_pair(
                        int(action["record_a"]), int(action["record_b"])
                    )[0],
                    "record_b": normalize_pair(
                        int(action["record_a"]), int(action["record_b"])
                    )[1],
                    "relation": action["relation"],
                }
            )
        final_grouping = build_grouping(list(by_id), remaining)
        broken = invalid_diff_decisions(remaining, final_grouping)
        if broken:
            raise ApiProblem(
                500,
                "inconsistent_operation",
                "保存时检测到分组自相矛盾，已回滚。",
            )

        # Re-check the proposed grouping against the current write-offs: a
        # write-off saved after the preview must still block this change.
        # The preview stays pending so it can be confirmed once the blocking
        # write-offs are released.
        writeoff_conflict = _writeoff_crossing_conflict(conn, final_grouping)
        if writeoff_conflict is not None:
            return 409, {
                "ok": False,
                "error": {
                    "code": writeoff_conflict["code"],
                    "message": writeoff_conflict["message"],
                    "schemes": writeoff_conflict["schemes"],
                    "writeoffs": writeoff_conflict["writeoffs"],
                },
                "preview_id": preview_id,
            }

        applied: dict[str, Any]
        if action["kind"] == "add":
            left, right = int(action["record_a"]), int(action["record_b"])
            pair = normalize_pair(left, right)
            cursor = conn.execute(
                "INSERT INTO decisions (record_a, record_b, relation, created_at) "
                "VALUES (?, ?, ?, ?)",
                (pair[0], pair[1], action["relation"], utc_now()),
            )
            new_decision = {
                "id": int(cursor.lastrowid),
                "record_a": pair[0],
                "record_b": pair[1],
                "relation": action["relation"],
                "created_at": utc_now(),
            }
            applied = {
                "kind": "add",
                "decision": decision_view(new_decision, by_id),
            }
            summary = (
                "已保存同一企业判定并更新分组。"
                if action["relation"] == REL_SAME
                else "已保存不同企业判定；分组不变，约束已生效。"
            )
        else:
            decision_id = int(action["decision_id"])
            removed = next(
                decision for decision in decisions if int(decision["id"]) == decision_id
            )
            conn.execute("DELETE FROM decisions WHERE id = ?", (decision_id,))
            applied = {
                "kind": "withdraw",
                "decision": decision_view(removed, by_id),
            }
            summary = "已撤回该判定；其余判定继续生效，分组已更新。"

        applied["summary"] = summary

        stamp = utc_now()
        conn.execute("DELETE FROM group_members")
        conn.execute("DELETE FROM groups")
        conn.executemany(
            "INSERT INTO groups (group_key, member_count, updated_at) VALUES (?, ?, ?)",
            [(key, len(ids), stamp) for key, ids in final_grouping.by_key.items()],
        )
        conn.executemany(
            "INSERT INTO group_members (record_id, group_key) VALUES (?, ?)",
            [
                (rid, key)
                for key, ids in final_grouping.by_key.items()
                for rid in ids
            ],
        )

        conn.execute(
            "UPDATE previews SET status = ?, confirmed_at = ? WHERE id = ?",
            (PREVIEW_CONFIRMED, stamp, preview_id),
        )

        applied["affected_group_count"] = len(
            {final_grouping.group_of[rid] for rid in involved}
        )
        return 200, {
            "ok": True,
            "preview_id": preview_id,
            "applied": applied,
        }


def cancel_preview(conn: sqlite3.Connection, preview_id: str) -> dict[str, Any]:
    with db.write_tx(conn):
        row = conn.execute(
            "SELECT status FROM previews WHERE id = ?", (preview_id,)
        ).fetchone()
        if row is None:
            raise ApiProblem(404, "unknown_preview", "预览不存在")
        if row["status"] == PREVIEW_PENDING:
            conn.execute(
                "UPDATE previews SET status = 'cancelled' WHERE id = ?", (preview_id,)
            )
        return {"ok": True, "preview_id": preview_id, "status": "cancelled"}


def count_preview_statuses(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM previews GROUP BY status"
    ).fetchall()
    return {row["status"]: int(row["count"]) for row in rows}
