from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app

SEED = """来源,源编号,企业名称
采购系统A,A-001,甲企业
采购系统B,B-001,甲企业
采购系统A,A-002,乙企业
采购系统B,B-002,乙企业
采购系统A,A-003,丙企业
"""

# Record ids from the seed above:
#   1 = A-001 甲企业, 2 = B-001 甲企业, 3 = A-002 乙企业, 4 = B-002 乙企业, 5 = A-003 丙企业
DOCS = {
    "orders": [
        {"order_id": "PO-1", "record_id": 1, "amount_cents": 10000},
        {"order_id": "PO-1B", "record_id": 2, "amount_cents": 7000},
        {"order_id": "PO-2", "record_id": 3, "amount_cents": 20000},
        {"order_id": "PO-3", "record_id": 5, "amount_cents": 5000},
    ],
    "invoices": [
        {"invoice_id": "INV-1", "record_id": 2, "amount_cents": 10000},
        {"invoice_id": "INV-2", "record_id": 1, "amount_cents": 4000},
        {"invoice_id": "INV-3", "record_id": 4, "amount_cents": 8000},
    ],
}

A1, B1 = 1, 2  # 甲企业 records
A2, B2 = 3, 4  # 乙企业 records


@pytest.fixture()
def paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    csv_path = tmp_path / "suppliers.csv"
    csv_path.write_text(SEED, encoding="utf-8")
    docs_path = tmp_path / "docs.json"
    docs_path.write_text(json.dumps(DOCS), encoding="utf-8")
    return tmp_path / "review.db", csv_path, docs_path


def make_client(
    db_path: Path, csv_path: Path, docs_path: Path, instance: str = "test"
) -> TestClient:
    return TestClient(
        create_app(
            db_path=db_path,
            seed_csv=csv_path,
            seed_limit=100,
            instance=instance,
            seed_docs=docs_path,
            seed_docs_limit=20,
        )
    )


def wstate(client: TestClient) -> dict:
    response = client.get("/api/writeoffs/state")
    assert response.status_code == 200, response.text
    return response.json()["state"]


def order_view(state: dict, order_id: str) -> dict:
    return next(doc for doc in state["orders"] if doc["id"] == order_id)


def invoice_view(state: dict, invoice_id: str) -> dict:
    return next(doc for doc in state["invoices"] if doc["id"] == invoice_id)


def replace_scheme(client: TestClient, scheme_id: str, entries: list[dict]):
    return client.put(
        f"/api/schemes/{scheme_id}/writeoffs", json={"entries": entries}
    )


def save_scheme(client: TestClient, scheme_id: str, entries: list[dict]) -> dict:
    response = replace_scheme(client, scheme_id, entries)
    assert response.status_code == 200, response.text
    return response.json()


def add_and_confirm(client: TestClient, a: int, b: int, relation: str) -> dict:
    preview = client.post(
        "/api/previews",
        json={"operation": "add", "record_a": a, "record_b": b, "relation": relation},
    ).json()["preview"]
    assert preview["confirmable"], preview
    response = client.post(
        f"/api/previews/{preview['preview_id']}/confirm",
        json={"snapshot_digest": preview["snapshot_digest"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def merge_jia(client: TestClient) -> None:
    add_and_confirm(client, A1, B1, "same")


def test_documents_seeded_once_and_immutable(paths):
    db_path, csv_path, docs_path = paths
    with make_client(db_path, csv_path, docs_path) as client:
        data = wstate(client)
        assert data["stats"]["orders"] == 4
        assert data["stats"]["invoices"] == 3
        assert data["stats"]["order_total_cents"] == 10000 + 7000 + 20000 + 5000
        assert data["stats"]["invoice_total_cents"] == 10000 + 4000 + 8000
        assert data["stats"]["written_off_cents"] == 0
        assert data["stats"]["uninvoiced_cents"] == data["stats"]["order_total_cents"]
        assert order_view(data, "PO-1")["record"]["name"] == "甲企业"
        assert order_view(data, "PO-1")["remaining_cents"] == 10000
        assert invoice_view(data, "INV-3")["record"]["id"] == B2

        meta = client.get("/api/meta").json()
        assert meta["init"]["documents"] == {"orders": 4, "invoices": 3}

        conn = db.connect(db_path)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute("UPDATE erp_orders SET amount_cents = 1 WHERE id = 'PO-1'")
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute("DELETE FROM erp_invoices WHERE id = 'INV-1'")
        finally:
            conn.close()

    # A changed JSON file is not re-imported on restart.
    docs_path.write_text(
        json.dumps({"orders": [{"order_id": "PO-X", "record_id": 1, "amount_cents": 1}],
                    "invoices": []}),
        encoding="utf-8",
    )
    with make_client(db_path, csv_path, docs_path, instance="restart") as client:
        data = wstate(client)
        assert data["stats"]["orders"] == 4
        assert order_view(data, "PO-1")["amount_cents"] == 10000


def test_seed_docs_limit(tmp_path: Path):
    csv_path = tmp_path / "suppliers.csv"
    csv_path.write_text(SEED, encoding="utf-8")
    docs = {
        "orders": [
            {"order_id": f"PO-{i:03d}", "record_id": 1, "amount_cents": 100 + i}
            for i in range(25)
        ],
        "invoices": [
            {"invoice_id": f"INV-{i:03d}", "record_id": 2, "amount_cents": 50 + i}
            for i in range(22)
        ],
    }
    docs_path = tmp_path / "docs.json"
    docs_path.write_text(json.dumps(docs), encoding="utf-8")
    with make_client(tmp_path / "limit.db", csv_path, docs_path) as client:
        data = wstate(client)
        assert data["stats"]["orders"] == 20
        assert data["stats"]["invoices"] == 20
        # First 20 in file order.
        assert data["orders"][-1]["id"] == "PO-019"
        assert data["invoices"][-1]["id"] == "INV-019"


def test_same_group_required_for_every_entry(paths):
    db_path, csv_path, docs_path = paths
    with make_client(db_path, csv_path, docs_path) as client:
        # PO-1 (record 1) x INV-1 (record 2): suppliers not yet grouped together.
        response = replace_scheme(
            client, "S1", [{"order_id": "PO-1", "invoice_id": "INV-1", "amount_cents": 100}]
        )
        assert response.status_code == 409, response.text
        body = response.json()
        assert body["error"]["code"] == "suppliers_not_in_same_group"
        assert body["error"]["order_supplier"] == "甲企业"
        assert wstate(client)["schemes"] == []

        # Same record on both sides is trivially the same group.
        saved = save_scheme(
            client, "S1", [{"order_id": "PO-1", "invoice_id": "INV-2", "amount_cents": 100}]
        )
        assert saved["entry_count"] == 1

        # After merging the 甲企业 records the cross-record pair becomes valid.
        merge_jia(client)
        saved = save_scheme(
            client,
            "S1",
            [
                {"order_id": "PO-1", "invoice_id": "INV-2", "amount_cents": 100},
                {"order_id": "PO-1", "invoice_id": "INV-1", "amount_cents": 5000},
            ],
        )
        assert saved["entry_count"] == 2


def test_replace_and_balances_and_release_semantics(paths):
    db_path, csv_path, docs_path = paths
    with make_client(db_path, csv_path, docs_path) as client:
        merge_jia(client)
        save_scheme(
            client,
            "S1",
            [
                {"order_id": "PO-1", "invoice_id": "INV-1", "amount_cents": 6000},
                {"order_id": "PO-1", "invoice_id": "INV-2", "amount_cents": 4000},
            ],
        )
        data = wstate(client)
        assert order_view(data, "PO-1")["used_cents"] == 10000
        assert order_view(data, "PO-1")["remaining_cents"] == 0
        assert invoice_view(data, "INV-1")["used_cents"] == 6000
        assert data["stats"]["written_off_cents"] == 10000
        assert data["stats"]["uninvoiced_cents"] == data["stats"]["order_total_cents"] - 10000

        # Another scheme fills INV-1 to its amount.
        save_scheme(
            client, "S2", [{"order_id": "PO-1B", "invoice_id": "INV-1", "amount_cents": 4000}]
        )
        assert invoice_view(wstate(client), "INV-1")["remaining_cents"] == 0

        # Replacing S1 releases its own entries first: re-saving the same 6000
        # on INV-1 is fine, but 6001 would exceed what is left for this scheme.
        response = replace_scheme(
            client, "S1", [{"order_id": "PO-1", "invoice_id": "INV-1", "amount_cents": 6000}]
        )
        assert response.status_code == 200, response.text
        response = replace_scheme(
            client, "S1", [{"order_id": "PO-1", "invoice_id": "INV-1", "amount_cents": 6001}]
        )
        assert response.status_code == 409, response.text
        error = response.json()["error"]
        assert error["code"] == "amount_exceeded"
        assert error["document_id"] == "INV-1"
        assert error["used_by_others_cents"] == 4000

        # The failed replacement changed nothing.
        scheme_s1 = next(s for s in wstate(client)["schemes"] if s["scheme_id"] == "S1")
        assert scheme_s1["entries"] == [
            {
                "order_id": "PO-1",
                "invoice_id": "INV-1",
                "amount_cents": 6000,
                "order_record": {"id": 1, "source": "采购系统A", "source_code": "A-001", "name": "甲企业"},
                "invoice_record": {"id": 2, "source": "采购系统B", "source_code": "B-001", "name": "甲企业"},
                "same_group": True,
                "created_at": scheme_s1["entries"][0]["created_at"],
            }
        ]


def test_cross_scheme_split_and_cumulative_cap(paths):
    db_path, csv_path, docs_path = paths
    with make_client(db_path, csv_path, docs_path) as client:
        merge_jia(client)
        save_scheme(client, "S1", [{"order_id": "PO-1", "invoice_id": "INV-1", "amount_cents": 6000}])
        save_scheme(client, "S2", [{"order_id": "PO-1", "invoice_id": "INV-2", "amount_cents": 4000}])
        data = wstate(client)
        assert order_view(data, "PO-1")["remaining_cents"] == 0
        assert {s["scheme_id"] for s in data["schemes"]} == {"S1", "S2"}

        # Any further write-off on PO-1 exceeds the order amount.
        response = replace_scheme(
            client, "S3", [{"order_id": "PO-1", "invoice_id": "INV-1", "amount_cents": 1}]
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "amount_exceeded"
        assert response.json()["error"]["document_kind"] == "order"

        # Clearing S2 frees its share again.
        response = replace_scheme(client, "S2", [])
        assert response.status_code == 200
        data = wstate(client)
        assert {s["scheme_id"] for s in data["schemes"]} == {"S1"}
        assert order_view(data, "PO-1")["remaining_cents"] == 4000


def test_entry_validation_errors_leave_state_untouched(paths):
    db_path, csv_path, docs_path = paths
    with make_client(db_path, csv_path, docs_path) as client:
        merge_jia(client)
        save_scheme(client, "S1", [{"order_id": "PO-1", "invoice_id": "INV-1", "amount_cents": 100}])
        before = wstate(client)

        # Duplicate order/invoice pair inside one batch.
        response = replace_scheme(
            client,
            "S1",
            [
                {"order_id": "PO-1", "invoice_id": "INV-1", "amount_cents": 100},
                {"order_id": "PO-1", "invoice_id": "INV-1", "amount_cents": 50},
            ],
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "duplicate_entry"

        # Unknown documents.
        response = replace_scheme(
            client, "S1", [{"order_id": "PO-X", "invoice_id": "INV-1", "amount_cents": 1}]
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unknown_order"
        response = replace_scheme(
            client, "S1", [{"order_id": "PO-1", "invoice_id": "INV-X", "amount_cents": 1}]
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unknown_invoice"

        # Non-positive / non-integer amounts are rejected by the schema.
        response = replace_scheme(
            client, "S1", [{"order_id": "PO-1", "invoice_id": "INV-1", "amount_cents": 0}]
        )
        assert response.status_code == 422
        response = replace_scheme(
            client, "S1", [{"order_id": "PO-1", "invoice_id": "INV-1", "amount_cents": 10.5}]
        )
        assert response.status_code == 422

        after = wstate(client)
        assert json.dumps(before["schemes"], sort_keys=True) == json.dumps(
            after["schemes"], sort_keys=True
        )


def test_relation_preview_blocked_by_writeoff(paths):
    db_path, csv_path, docs_path = paths
    with make_client(db_path, csv_path, docs_path) as client:
        merge_jia(client)
        save_scheme(client, "S1", [{"order_id": "PO-1", "invoice_id": "INV-1", "amount_cents": 5000}])

        decisions = client.get("/api/state").json()["state"]["decisions"]
        decision_id = decisions[0]["id"]
        preview = client.post(
            "/api/previews", json={"operation": "withdraw", "decision_id": decision_id}
        ).json()["preview"]
        assert preview["confirmable"] is False
        assert preview["conflict"]["code"] == "writeoff_crosses_groups"
        assert preview["conflict"]["schemes"] == ["S1"]
        assert preview["conflict"]["writeoffs"][0]["order_id"] == "PO-1"
        assert preview["conflict"]["writeoffs"][0]["invoice_id"] == "INV-1"
        assert "S1" in preview["conflict"]["message"]
        # The conflict preview still describes the withdraw operation.
        assert preview["operation"]["kind"] == "withdraw"
        assert preview["operation"]["decision"]["id"] == decision_id

        # The decision is untouched and the group is still merged.
        data = client.get("/api/state").json()["state"]
        assert data["stats"]["same_decisions"] == 1
        assert data["stats"]["merged_groups"] == 1


def test_relation_confirm_rechecks_writeoffs_and_clear_allows_split(paths):
    db_path, csv_path, docs_path = paths
    with make_client(db_path, csv_path, docs_path) as client:
        merge_jia(client)
        decisions = client.get("/api/state").json()["state"]["decisions"]
        decision_id = decisions[0]["id"]

        # Preview the split while no write-off exists yet.
        preview = client.post(
            "/api/previews", json={"operation": "withdraw", "decision_id": decision_id}
        ).json()["preview"]
        assert preview["confirmable"] is True

        # A write-off saved afterwards must still block the confirmation.
        save_scheme(client, "S1", [{"order_id": "PO-1", "invoice_id": "INV-1", "amount_cents": 5000}])
        response = client.post(
            f"/api/previews/{preview['preview_id']}/confirm",
            json={"snapshot_digest": preview["snapshot_digest"]},
        )
        assert response.status_code == 409, response.text
        error = response.json()["error"]
        assert error["code"] == "writeoff_crosses_groups"
        assert error["schemes"] == ["S1"]
        assert error["writeoffs"][0]["amount_cents"] == 5000

        # Nothing changed: decision alive, groups merged, preview still pending.
        data = client.get("/api/state").json()["state"]
        assert data["stats"]["same_decisions"] == 1
        assert data["stats"]["merged_groups"] == 1

        # Releasing the write-off unblocks exactly this preview.
        response = replace_scheme(client, "S1", [])
        assert response.status_code == 200
        response = client.post(
            f"/api/previews/{preview['preview_id']}/confirm",
            json={"snapshot_digest": preview["snapshot_digest"]},
        )
        assert response.status_code == 200, response.text
        data = response.json()["state"]
        assert data["stats"]["same_decisions"] == 0
        assert data["stats"]["merged_groups"] == 0

        # After the split the same write-off can no longer be saved.
        response = replace_scheme(
            client, "S1", [{"order_id": "PO-1", "invoice_id": "INV-1", "amount_cents": 5000}]
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "suppliers_not_in_same_group"


def test_writeoffs_persist_across_restart(paths):
    db_path, csv_path, docs_path = paths
    with make_client(db_path, csv_path, docs_path) as client:
        merge_jia(client)
        save_scheme(client, "S1", [{"order_id": "PO-1", "invoice_id": "INV-1", "amount_cents": 5000}])

    with make_client(db_path, csv_path, docs_path, instance="restart") as client:
        data = wstate(client)
        assert [s["scheme_id"] for s in data["schemes"]] == ["S1"]
        assert order_view(data, "PO-1")["used_cents"] == 5000
        assert invoice_view(data, "INV-1")["used_cents"] == 5000
        # Restarting again must not duplicate or reset anything.
    with make_client(db_path, csv_path, docs_path, instance="restart-2") as client:
        assert wstate(client)["stats"]["entries"] == 1


def test_state_snapshot_is_internally_consistent(paths):
    db_path, csv_path, docs_path = paths
    with make_client(db_path, csv_path, docs_path, "one") as client_one, make_client(
        db_path, csv_path, docs_path, "two"
    ) as client_two:
        merge_jia(client_one)
        save_scheme(
            client_one,
            "S1",
            [
                {"order_id": "PO-1", "invoice_id": "INV-1", "amount_cents": 6000},
                {"order_id": "PO-1B", "invoice_id": "INV-1", "amount_cents": 3000},
            ],
        )
        save_scheme(client_two, "S2", [{"order_id": "PO-1", "invoice_id": "INV-2", "amount_cents": 4000}])

        # One query returns groups, write-off details and balances that agree.
        data = wstate(client_two)
        assert data["instance"] == "two"
        group_of_record = {}
        for group in data["groups"]:
            for member in group["members"]:
                group_of_record[member["id"]] = group["key"]
        assert group_of_record[A1] == group_of_record[B1]
        assert group_of_record[A1] != group_of_record[A2]

        per_order: dict[str, int] = {}
        per_invoice: dict[str, int] = {}
        for scheme in data["schemes"]:
            for entry in scheme["entries"]:
                per_order[entry["order_id"]] = per_order.get(entry["order_id"], 0) + entry["amount_cents"]
                per_invoice[entry["invoice_id"]] = per_invoice.get(entry["invoice_id"], 0) + entry["amount_cents"]
                assert entry["same_group"] is True
        for doc in data["orders"]:
            assert doc["used_cents"] == per_order.get(doc["id"], 0)
            assert doc["remaining_cents"] == doc["amount_cents"] - doc["used_cents"]
        for doc in data["invoices"]:
            assert doc["used_cents"] == per_invoice.get(doc["id"], 0)
            assert doc["remaining_cents"] == doc["amount_cents"] - doc["used_cents"]
        assert data["stats"]["uninvoiced_cents"] == (
            data["stats"]["order_total_cents"] - data["stats"]["written_off_cents"]
        )


def test_cross_process_scheme_save_and_read(paths):
    db_path, csv_path, docs_path = paths
    with make_client(db_path, csv_path, docs_path, "one") as client_one, make_client(
        db_path, csv_path, docs_path, "two"
    ) as client_two:
        merge_jia(client_one)
        save_scheme(client_one, "S1", [{"order_id": "PO-1", "invoice_id": "INV-1", "amount_cents": 100}])
        seen_by_two = wstate(client_two)
        assert [s["scheme_id"] for s in seen_by_two["schemes"]] == ["S1"]
        assert order_view(seen_by_two, "PO-1")["used_cents"] == 100

        # A failed save on one process does not disturb the other one's view.
        before = wstate(client_two)
        response = replace_scheme(
            client_one, "S1", [{"order_id": "PO-1", "invoice_id": "INV-1", "amount_cents": 99999}]
        )
        assert response.status_code == 409
        after = wstate(client_two)
        assert json.dumps(before["schemes"], sort_keys=True) == json.dumps(
            after["schemes"], sort_keys=True
        )
