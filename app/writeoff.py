"""Write-off (核销) schemes linking ERP orders and invoices.

A scheme is a named batch of write-off entries.  Saving a scheme replaces its
whole entry set atomically (an empty set clears the scheme).  The same
document may be split across schemes as long as the cumulative written-off
amount never exceeds the document amount; a replacement is validated against
the overall result after releasing the scheme's previous entries.  Every
entry's order-side and invoice-side suppliers must belong to the same current
group, and relation changes are rejected while they would push existing
write-offs across groups.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping, Sequence

from . import db
from .common import ApiProblem, utc_now
from .grouping import Grouping, build_grouping

MAX_ENTRIES_PER_BATCH = 500
MAX_SCHEME_ID_LENGTH = 64


# --------------------------------------------------------------------- helpers


def fmt_cents(cents: int) -> str:
    return f"¥{cents / 100:,.2f}"


def validate_scheme_id(scheme_id: str) -> str:
    cleaned = (scheme_id or "").strip()
    if not cleaned:
        raise ApiProblem(400, "invalid_scheme", "方案 ID 不能为空。")
    if len(cleaned) > MAX_SCHEME_ID_LENGTH:
        raise ApiProblem(
            400,
            "invalid_scheme",
            f"方案 ID 最长 {MAX_SCHEME_ID_LENGTH} 个字符。",
        )
    return cleaned


def _record_brief(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["record_id"]),
        "source": row["source"],
        "source_code": row["source_code"],
        "name": row["name"],
    }


# --------------------------------------------------------------------- loaders


def load_orders(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT o.id, o.seq, o.record_id, o.amount_cents, "
        "       r.source, r.source_code, r.name "
        "FROM erp_orders o JOIN records r ON r.id = o.record_id "
        "ORDER BY o.seq"
    ).fetchall()
    return [dict(row) for row in rows]


def load_invoices(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT i.id, i.seq, i.record_id, i.amount_cents, "
        "       r.source, r.source_code, r.name "
        "FROM erp_invoices i JOIN records r ON r.id = i.record_id "
        "ORDER BY i.seq"
    ).fetchall()
    return [dict(row) for row in rows]


def load_schemes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT scheme_id, created_at, updated_at FROM schemes ORDER BY scheme_id"
    ).fetchall()
    return [dict(row) for row in rows]


def load_writeoffs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, scheme_id, order_id, invoice_id, amount_cents, created_at "
        "FROM writeoffs ORDER BY scheme_id, id"
    ).fetchall()
    return [dict(row) for row in rows]


def _load_grouping(conn: sqlite3.Connection) -> tuple[Grouping, dict[int, dict[str, Any]]]:
    record_rows = conn.execute(
        "SELECT id, source, source_code, name FROM records ORDER BY id"
    ).fetchall()
    decision_rows = conn.execute(
        "SELECT id, record_a, record_b, relation FROM decisions ORDER BY id"
    ).fetchall()
    records = {int(row["id"]): dict(row) for row in record_rows}
    grouping = build_grouping(list(records), [dict(row) for row in decision_rows])
    return grouping, records


# --------------------------------------------------------------- derived facts


def usage_by_document(
    writeoffs: Sequence[Mapping[str, Any]], exclude_scheme: str | None = None
) -> tuple[dict[str, int], dict[str, int]]:
    """Cumulative written-off cents per document, optionally skipping a scheme."""
    per_order: dict[str, int] = {}
    per_invoice: dict[str, int] = {}
    for entry in writeoffs:
        if exclude_scheme is not None and entry["scheme_id"] == exclude_scheme:
            continue
        amount = int(entry["amount_cents"])
        per_order[entry["order_id"]] = per_order.get(entry["order_id"], 0) + amount
        per_invoice[entry["invoice_id"]] = (
            per_invoice.get(entry["invoice_id"], 0) + amount
        )
    return per_order, per_invoice


def cross_group_entries(
    grouping: Grouping,
    orders: Sequence[Mapping[str, Any]],
    invoices: Sequence[Mapping[str, Any]],
    writeoffs: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Write-off entries whose two suppliers sit in different groups.

    Used by both the relation-change preview and its confirmation: any
    non-empty result means the proposed grouping would tear an existing
    write-off across groups and the change must be refused.
    """
    order_record = {order["id"]: int(order["record_id"]) for order in orders}
    invoice_record = {invoice["id"]: int(invoice["record_id"]) for invoice in invoices}
    crossing: list[dict[str, Any]] = []
    for entry in writeoffs:
        left = order_record.get(entry["order_id"])
        right = invoice_record.get(entry["invoice_id"])
        if left is None or right is None:
            continue
        left_group = grouping.group_of.get(left)
        right_group = grouping.group_of.get(right)
        if left_group != right_group:
            crossing.append(
                {
                    "scheme_id": entry["scheme_id"],
                    "order_id": entry["order_id"],
                    "invoice_id": entry["invoice_id"],
                    "amount_cents": int(entry["amount_cents"]),
                    "order_record_id": left,
                    "invoice_record_id": right,
                    "order_group": left_group,
                    "invoice_group": right_group,
                }
            )
    crossing.sort(key=lambda item: (item["scheme_id"], item["order_id"], item["invoice_id"]))
    return crossing


# ------------------------------------------------------------- atomic replace


def replace_scheme_writeoffs(
    conn: sqlite3.Connection,
    scheme_id: str,
    entries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Replace (or clear) one scheme's write-off entries in one transaction.

    Validation happens before any write and every failure raises ApiProblem,
    so a rejected save leaves the previous state untouched.
    """
    scheme_id = validate_scheme_id(scheme_id)
    if len(entries) > MAX_ENTRIES_PER_BATCH:
        raise ApiProblem(
            400,
            "too_many_entries",
            f"单个方案最多 {MAX_ENTRIES_PER_BATCH} 条核销明细。",
        )

    with db.write_tx(conn):
        orders = {order["id"]: order for order in load_orders(conn)}
        invoices = {invoice["id"]: invoice for invoice in load_invoices(conn)}
        grouping, records = _load_grouping(conn)

        normalised: list[dict[str, Any]] = []
        seen_pairs: set[tuple[str, str]] = set()
        for index, raw in enumerate(entries, start=1):
            order_id = str(raw.get("order_id") or "").strip()
            invoice_id = str(raw.get("invoice_id") or "").strip()
            amount = raw.get("amount_cents")
            order = orders.get(order_id)
            invoice = invoices.get(invoice_id)
            if order is None:
                raise ApiProblem(
                    400, "unknown_order", f"第 {index} 条明细的订单不存在：{order_id!r}。"
                )
            if invoice is None:
                raise ApiProblem(
                    400, "unknown_invoice", f"第 {index} 条明细的发票不存在：{invoice_id!r}。"
                )
            if not (isinstance(amount, int) and not isinstance(amount, bool) and amount > 0):
                raise ApiProblem(
                    400,
                    "invalid_amount",
                    f"第 {index} 条明细的金额必须是正整数分：{amount!r}。",
                )
            pair = (order_id, invoice_id)
            if pair in seen_pairs:
                raise ApiProblem(
                    400,
                    "duplicate_entry",
                    f"第 {index} 条明细与前面的明细重复（{order_id} × {invoice_id}）；"
                    "同一方案内同一订单-发票对只能出现一次，请合并金额。",
                )
            seen_pairs.add(pair)

            order_record = records[int(order["record_id"])]
            invoice_record = records[int(invoice["record_id"])]
            order_group = grouping.group_of[int(order["record_id"])]
            invoice_group = grouping.group_of[int(invoice["record_id"])]
            if order_group != invoice_group:
                raise ApiProblem(
                    409,
                    "suppliers_not_in_same_group",
                    f"第 {index} 条明细的双方供应商不在同一分组：订单 {order_id} 的"
                    f"“{order_record['name']}”与发票 {invoice_id} 的"
                    f"“{invoice_record['name']}”当前分属不同分组。请先在审校页将二者"
                    "判定为同一企业，或调整核销明细。",
                    order_id=order_id,
                    invoice_id=invoice_id,
                    order_supplier=order_record["name"],
                    invoice_supplier=invoice_record["name"],
                    order_group=order_group,
                    invoice_group=invoice_group,
                )
            normalised.append(
                {"order_id": order_id, "invoice_id": invoice_id, "amount_cents": amount}
            )

        # Release this scheme's existing entries, then validate the overall
        # cumulative usage of every document against its amount.
        existing = load_writeoffs(conn)
        per_order, per_invoice = usage_by_document(existing, exclude_scheme=scheme_id)
        for entry in normalised:
            per_order[entry["order_id"]] = (
                per_order.get(entry["order_id"], 0) + entry["amount_cents"]
            )
            per_invoice[entry["invoice_id"]] = (
                per_invoice.get(entry["invoice_id"], 0) + entry["amount_cents"]
            )

        def check_limits(
            usage: Mapping[str, int],
            documents: Mapping[str, Mapping[str, Any]],
            kind: str,
            label: str,
            key: str,
        ) -> None:
            for doc_id, used in sorted(usage.items()):
                limit = int(documents[doc_id]["amount_cents"])
                if used <= limit:
                    continue
                requested = sum(
                    entry["amount_cents"]
                    for entry in normalised
                    if entry[key] == doc_id
                )
                used_by_others = used - requested
                raise ApiProblem(
                    409,
                    "amount_exceeded",
                    f"{label} {doc_id} 累计核销 {fmt_cents(used)} 超过单据金额 "
                    f"{fmt_cents(limit)}：其他方案已占用 {fmt_cents(used_by_others)}，"
                    f"本方案可申请余额为 {fmt_cents(limit - used_by_others)}。",
                    document_kind=kind,
                    document_id=doc_id,
                    amount_cents=limit,
                    used_by_others_cents=used_by_others,
                    requested_cents=requested,
                )

        check_limits(per_order, orders, "order", "订单", "order_id")
        check_limits(per_invoice, invoices, "invoice", "发票", "invoice_id")

        stamp = utc_now()
        conn.execute("DELETE FROM writeoffs WHERE scheme_id = ?", (scheme_id,))
        if normalised:
            conn.execute(
                "INSERT INTO schemes (scheme_id, created_at, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(scheme_id) DO UPDATE SET updated_at = excluded.updated_at",
                (scheme_id, stamp, stamp),
            )
            conn.executemany(
                "INSERT INTO writeoffs (scheme_id, order_id, invoice_id, amount_cents, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (scheme_id, e["order_id"], e["invoice_id"], e["amount_cents"], stamp)
                    for e in normalised
                ],
            )
        else:
            conn.execute("DELETE FROM schemes WHERE scheme_id = ?", (scheme_id,))

        total = sum(entry["amount_cents"] for entry in normalised)
        if normalised:
            summary = (
                f"方案 {scheme_id} 已保存 {len(normalised)} 条核销明细，"
                f"合计 {fmt_cents(total)}。"
            )
        else:
            summary = f"方案 {scheme_id} 的核销明细已清空。"
        return {
            "ok": True,
            "scheme_id": scheme_id,
            "entry_count": len(normalised),
            "total_cents": total,
            "summary": summary,
        }


# ------------------------------------------------------------- state snapshot


def build_state(conn: sqlite3.Connection, instance: str) -> dict[str, Any]:
    """One consistent snapshot: groups, write-off details and balances."""
    with db.read_tx(conn):
        orders = load_orders(conn)
        invoices = load_invoices(conn)
        schemes = load_schemes(conn)
        writeoffs = load_writeoffs(conn)
        grouping, records = _load_grouping(conn)

        per_order, per_invoice = usage_by_document(writeoffs)

        def document_view(doc: Mapping[str, Any], used: int) -> dict[str, Any]:
            amount = int(doc["amount_cents"])
            return {
                "id": doc["id"],
                "record": _record_brief(doc),
                "group": grouping.group_of[int(doc["record_id"])],
                "amount_cents": amount,
                "used_cents": used,
                "remaining_cents": amount - used,
            }

        order_views = [
            document_view(order, per_order.get(order["id"], 0)) for order in orders
        ]
        invoice_views = [
            document_view(invoice, per_invoice.get(invoice["id"], 0))
            for invoice in invoices
        ]

        entries_by_scheme: dict[str, list[dict[str, Any]]] = {}
        orders_by_id = {order["id"]: order for order in orders}
        invoices_by_id = {invoice["id"]: invoice for invoice in invoices}
        for entry in writeoffs:
            order = orders_by_id[entry["order_id"]]
            invoice = invoices_by_id[entry["invoice_id"]]
            order_group = grouping.group_of[int(order["record_id"])]
            invoice_group = grouping.group_of[int(invoice["record_id"])]
            entries_by_scheme.setdefault(entry["scheme_id"], []).append(
                {
                    "order_id": entry["order_id"],
                    "invoice_id": entry["invoice_id"],
                    "amount_cents": int(entry["amount_cents"]),
                    "order_record": _record_brief(order),
                    "invoice_record": _record_brief(invoice),
                    "same_group": order_group == invoice_group,
                    "created_at": entry["created_at"],
                }
            )

        scheme_views = []
        known = {scheme["scheme_id"]: scheme for scheme in schemes}
        for scheme_id in sorted(set(known) | set(entries_by_scheme)):
            entries = entries_by_scheme.get(scheme_id, [])
            meta = known.get(scheme_id, {})
            scheme_views.append(
                {
                    "scheme_id": scheme_id,
                    "entry_count": len(entries),
                    "total_cents": sum(e["amount_cents"] for e in entries),
                    "updated_at": meta.get("updated_at"),
                    "entries": entries,
                }
            )

        groups = []
        for key, ids in grouping.by_key.items():
            groups.append(
                {
                    "key": key,
                    "size": len(ids),
                    "members": [
                        {
                            "id": rid,
                            "source": records[rid]["source"],
                            "source_code": records[rid]["source_code"],
                            "name": records[rid]["name"],
                        }
                        for rid in ids
                    ],
                }
            )
        groups.sort(key=lambda group: (-group["size"], group["key"]))

        order_total = sum(view["amount_cents"] for view in order_views)
        invoice_total = sum(view["amount_cents"] for view in invoice_views)
        written_off = sum(view["used_cents"] for view in order_views)

        return {
            "instance": instance,
            "generated_at": utc_now(),
            "orders": order_views,
            "invoices": invoice_views,
            "schemes": scheme_views,
            "groups": groups,
            "stats": {
                "orders": len(order_views),
                "invoices": len(invoice_views),
                "order_total_cents": order_total,
                "invoice_total_cents": invoice_total,
                "written_off_cents": written_off,
                "uninvoiced_cents": order_total - written_off,
                "schemes": len(scheme_views),
                "entries": len(writeoffs),
            },
        }
