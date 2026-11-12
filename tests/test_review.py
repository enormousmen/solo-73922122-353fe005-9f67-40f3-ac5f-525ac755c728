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
采购系统B,B-003,丙企业
采购系统A,A-004,丁企业
采购系统B,B-004,丁企业
采购系统A,A-005,戊企业
"""

# Convenient ids from the seed above.
A1, B1 = 1, 2   # 甲企业
A2, B2 = 3, 4   # 乙企业
A3, B3 = 5, 6   # 丙企业
A4, B4 = 7, 8   # 丁企业
A5 = 9          # 戊企业 (single source)


@pytest.fixture()
def paths(tmp_path: Path) -> tuple[Path, Path]:
    csv_path = tmp_path / "suppliers.csv"
    csv_path.write_text(SEED, encoding="utf-8")
    return tmp_path / "review.db", csv_path


def make_client(db_path: Path, csv_path: Path, instance: str = "test") -> TestClient:
    return TestClient(
        create_app(db_path=db_path, seed_csv=csv_path, seed_limit=100, instance=instance)
    )


def preview_add(client: TestClient, a: int, b: int, relation: str) -> dict:
    response = client.post(
        "/api/previews",
        json={"operation": "add", "record_a": a, "record_b": b, "relation": relation},
    )
    assert response.status_code == 200, response.text
    return response.json()["preview"]


def preview_withdraw(client: TestClient, decision_id: int) -> dict:
    response = client.post(
        "/api/previews",
        json={"operation": "withdraw", "decision_id": decision_id},
    )
    assert response.status_code == 200, response.text
    return response.json()["preview"]


def confirm(client: TestClient, preview: dict, *, digest: bool = True):
    body = {"snapshot_digest": preview["snapshot_digest"]} if digest else {}
    return client.post(
        f"/api/previews/{preview['preview_id']}/confirm", json=body
    )


def add_and_confirm(client: TestClient, a: int, b: int, relation: str) -> dict:
    preview = preview_add(client, a, b, relation)
    assert preview["confirmable"], preview
    response = confirm(client, preview)
    assert response.status_code == 200, response.text
    return response.json()


def state(client: TestClient) -> dict:
    response = client.get("/api/state")
    assert response.status_code == 200
    return response.json()["state"]


def group_of(state_data: dict, record_id: int) -> str:
    for group in state_data["groups"]:
        if any(member["id"] == record_id for member in group["members"]):
            return group["key"]
    raise AssertionError(f"record {record_id} not in any group")


def decisions_in_db(db_path: Path) -> list[sqlite3.Row]:
    conn = db.connect(db_path)
    try:
        return conn.execute("SELECT * FROM decisions ORDER BY id").fetchall()
    finally:
        conn.close()


def test_seed_immutable_and_initial_groups(paths):
    db_path, csv_path = paths
    with make_client(db_path, csv_path) as client:
        data = state(client)
        assert data["stats"]["records"] == 9
        assert data["stats"]["groups"] == 9
        assert data["stats"]["same_decisions"] == 0
        assert data["records"][0] == {
            "id": 1,
            "source": "采购系统A",
            "source_code": "A-001",
            "name": "甲企业",
        }
        jia_pair = next(
            pair for pair in data["same_name_pairs"] if pair["name"] == "甲企业"
        )
        assert jia_pair["record_a"]["id"] == 1
        assert jia_pair["record_b"]["id"] == 2

        conn = db.connect(db_path)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute("UPDATE records SET name = 'changed' WHERE id = 1")
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute("DELETE FROM records WHERE id = 1")
        finally:
            conn.close()

    # The CSV is imported only on the first startup.
    csv_path.write_text("来源,源编号,企业名称\nX,X-1,替换企业\n", encoding="utf-8")
    with make_client(db_path, csv_path, instance="restart") as client:
        assert state(client)["stats"]["records"] == 9


def test_seed_limit_100(tmp_path: Path):
    csv_path = tmp_path / "big.csv"
    lines = ["来源,源编号,企业名称"]
    for index in range(120):
        lines.append(f"系统A,A-{index:03d},企业{index}")
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with make_client(tmp_path / "big.db", csv_path) as client:
        assert state(client)["stats"]["records"] == 100


def test_add_same_preview_confirm_persist_and_restart(paths):
    db_path, csv_path = paths
    with make_client(db_path, csv_path) as client:
        preview = preview_add(client, A1, B1, "same")
        assert preview["confirmable"] is True
        assert preview["conflict"] is None
        assert len(preview["groups_before"]) == 2
        assert len(preview["groups_after"]) == 1
        assert all(change["changed"] for change in preview["changes"])
        assert sorted(preview["affected_ids"]) == [A1, B1]
        assert preview["summary"].startswith("将“甲企业”")

        response = confirm(client, preview)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["applied"]["kind"] == "add"
        data = body["state"]
        assert group_of(data, A1) == group_of(data, B1)
        assert data["stats"]["same_decisions"] == 1
        assert data["stats"]["merged_groups"] == 1

    with make_client(db_path, csv_path, instance="restart") as client:
        data = state(client)
        assert group_of(data, A1) == group_of(data, B1)
        assert len(decisions_in_db(db_path)) == 1

    conn = db.connect(db_path)
    try:
        membership = conn.execute(
            "SELECT COUNT(*) FROM group_members WHERE record_id IN (1, 2)"
        ).fetchone()[0]
        groups = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
    finally:
        conn.close()
    assert membership == 2
    assert groups == 8


def test_same_is_transitive(paths):
    db_path, csv_path = paths
    with make_client(db_path, csv_path) as client:
        add_and_confirm(client, A1, B1, "same")
        preview = preview_add(client, B1, A2, "same")  # ids 2 and 3
        assert preview["confirmable"]
        assert len(preview["groups_before"]) == 2
        assert len(preview["groups_after"]) == 1
        assert len(preview["groups_after"][0]["members"]) == 3
        response = confirm(client, preview)
        assert response.status_code == 200
        data = response.json()["state"]
        assert group_of(data, A1) == group_of(data, B1) == group_of(data, A2)

        # A redundant same decision inside the component is still a basis entry.
        preview = preview_add(client, A1, A2, "same")
        assert preview["confirmable"]
        assert preview["summary"].startswith("新增“同一企业”判定")
        assert len(preview["groups_before"]) == 1
        assert len(preview["groups_after"]) == 1


def test_diff_conflicts_with_same_group(paths):
    db_path, csv_path = paths
    with make_client(db_path, csv_path) as client:
        add_and_confirm(client, A1, B1, "same")
        add_and_confirm(client, B1, A2, "same")
        data = state(client)
        assert group_of(data, A1) == group_of(data, A2)

        preview = preview_add(client, A1, A2, "diff")
        assert preview["confirmable"] is False
        assert preview["conflict"]["code"] == "diff_within_same_group"
        assert len(preview["conflict"]["same_path"]) >= 2
        assert len(decisions_in_db(db_path)) == 2

        preview = preview_add(client, A1, B2, "diff")  # different groups
        assert preview["confirmable"] is True
        assert len(preview["groups_before"]) == 2
        assert len(preview["groups_after"]) == 2
        response = confirm(client, preview)
        assert response.status_code == 200
        assert response.json()["state"]["stats"]["diff_decisions"] == 1


def test_same_conflicts_with_existing_diff(paths):
    db_path, csv_path = paths
    with make_client(db_path, csv_path) as client:
        add_and_confirm(client, A1, A2, "diff")
        preview = preview_add(client, A1, A2, "same")
        assert preview["confirmable"] is False
        assert preview["conflict"]["code"] == "same_conflicts_with_diff"
        assert preview["conflict"]["evidence"][0]["relation"] == "diff"
        assert len(decisions_in_db(db_path)) == 1


def test_withdraw_removes_only_one_decision(paths):
    db_path, csv_path = paths
    with make_client(db_path, csv_path) as client:
        add_and_confirm(client, A1, B1, "same")  # id 1-2
        add_and_confirm(client, B1, A2, "same")  # id 2-3
        data = state(client)
        assert len({group_of(data, rid) for rid in (A1, B1, A2)}) == 1
        decision_ids = [int(row["id"]) for row in decisions_in_db(db_path)]
        assert len(decision_ids) == 2

        preview = preview_withdraw(client, decision_ids[0])
        assert preview["confirmable"] is True
        assert len(preview["groups_before"]) == 1
        assert len(preview["groups_after"]) == 2
        assert preview["summary"].startswith("撤回该“同一企业”判定后")
        response = confirm(client, preview)
        assert response.status_code == 200
        result = response.json()["state"]
        # The other same decision remains effective, so 2 and 3 stay together.
        assert group_of(result, B1) == group_of(result, A2)
        assert group_of(result, A1) != group_of(result, B1)
        remaining = decisions_in_db(db_path)
        assert len(remaining) == 1
        assert (remaining[0]["record_a"], remaining[0]["record_b"]) == (2, 3)

        # Withdrawing the last relation returns to singleton groups.
        preview = preview_withdraw(client, int(remaining[0]["id"]))
        response = confirm(client, preview)
        assert response.status_code == 200
        assert response.json()["state"]["stats"]["merged_groups"] == 0


def test_withdraw_diff_keeps_grouping(paths):
    db_path, csv_path = paths
    with make_client(db_path, csv_path) as client:
        add_and_confirm(client, A1, A2, "diff")
        decision_id = int(decisions_in_db(db_path)[0]["id"])
        preview = preview_withdraw(client, decision_id)
        assert len(preview["groups_before"]) == 2
        assert len(preview["groups_after"]) == 2
        response = confirm(client, preview)
        assert response.status_code == 200
        assert response.json()["state"]["stats"]["diff_decisions"] == 0


def test_stale_preview_on_related_group_change(paths):
    db_path, csv_path = paths
    with make_client(db_path, csv_path, "one") as client_one, make_client(
        db_path, csv_path, "two"
    ) as client_two:
        preview = preview_add(client_one, A1, B1, "same")

        # A determination touching involved record 2 changes the preview basis.
        add_and_confirm(client_two, B1, A2, "same")

        before_attempt = state(client_one)
        response = confirm(client_one, preview)
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "preview_stale"
        after_attempt = state(client_one)

        # The refused confirmation did not change determinations or grouping.
        assert json.dumps(before_attempt["decisions"], sort_keys=True) == json.dumps(
            after_attempt["decisions"], sort_keys=True
        )
        assert json.dumps(before_attempt["groups"], sort_keys=True) == json.dumps(
            after_attempt["groups"], sort_keys=True
        )
        assert len(decisions_in_db(db_path)) == 1
        assert group_of(state(client_one), B1) == group_of(state(client_one), A2)

        # A fresh preview must be taken and can then be confirmed.
        fresh = preview_add(client_two, A1, B1, "same")
        response = confirm(client_one, fresh)
        assert response.status_code == 200
        final = response.json()["state"]
        assert group_of(final, A1) == group_of(final, B1) == group_of(final, A2)


def test_unrelated_group_change_does_not_block_confirmation(paths):
    db_path, csv_path = paths
    with make_client(db_path, csv_path, "one") as client_one, make_client(
        db_path, csv_path, "two"
    ) as client_two:
        preview = preview_add(client_one, A1, B1, "same")
        unrelated_key_before = group_of(state(client_one), A2)

        # Both endpoints of this change are outside the involved groups.
        add_and_confirm(client_two, A2, B2, "same")
        assert group_of(state(client_one), A2) != unrelated_key_before

        response = confirm(client_one, preview)
        assert response.status_code == 200, response.text
        data = response.json()["state"]
        assert group_of(data, A1) == group_of(data, B1)
        assert group_of(data, A2) == group_of(data, B2)
        assert group_of(data, A1) != group_of(data, A2)
        assert len(decisions_in_db(db_path)) == 2


def test_cross_process_preview_and_confirm(paths):
    db_path, csv_path = paths
    with make_client(db_path, csv_path, "one") as client_one, make_client(
        db_path, csv_path, "two"
    ) as client_two:
        preview = preview_add(client_one, A1, B1, "same")
        response = confirm(client_two, preview)
        assert response.status_code == 200, response.text
        assert response.json()["state"]["instance"] == "two"

        seen_by_one = state(client_one)
        assert group_of(seen_by_one, A1) == group_of(seen_by_one, B1)

        # A preview can be confirmed only once.
        again = confirm(client_one, preview)
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "preview_not_pending"


def test_preview_validation_and_cancel(paths):
    db_path, csv_path = paths
    with make_client(db_path, csv_path) as client:
        assert client.post(
            "/api/previews",
            json={"operation": "withdraw", "decision_id": 999},
        ).status_code == 404
        assert client.post(
            "/api/previews",
            json={"operation": "add", "record_a": 1, "record_b": 1, "relation": "same"},
        ).status_code == 400
        assert client.post(
            "/api/previews",
            json={"operation": "add", "record_a": 1, "record_b": 2},
        ).status_code == 400

        preview = preview_add(client, A1, B1, "same")
        cancel = client.post(f"/api/previews/{preview['preview_id']}/cancel")
        assert cancel.status_code == 200
        response = confirm(client, preview)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "preview_not_pending"


def test_conflict_preview_is_not_confirmable(paths):
    db_path, csv_path = paths
    with make_client(db_path, csv_path) as client:
        add_and_confirm(client, A1, B1, "same")
        duplicate = preview_add(client, B1, A1, "same")
        assert duplicate["confirmable"] is False
        assert duplicate["conflict"]["code"] == "pair_decision_exists"
        assert duplicate["preview_id"] is None


def test_wrong_client_digest_is_rejected_without_state_change(paths):
    db_path, csv_path = paths
    with make_client(db_path, csv_path) as client:
        preview = preview_add(client, A1, B1, "same")
        before = state(client)
        response = client.post(
            f"/api/previews/{preview['preview_id']}/confirm",
            json={"snapshot_digest": "not-the-preview-digest"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "preview_stale"
        after = state(client)
        assert before["decisions"] == after["decisions"]
        assert before["groups"] == after["groups"]
        assert len(decisions_in_db(db_path)) == 0


def test_persisted_group_tables_match_current_grouping(paths):
    db_path, csv_path = paths
    with make_client(db_path, csv_path) as client:
        add_and_confirm(client, A1, B1, "same")
        add_and_confirm(client, B1, A2, "same")
        data = state(client)
        conn = db.connect(db_path)
        try:
            records = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            members = conn.execute("SELECT COUNT(*) FROM group_members").fetchone()[0]
            groups = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
            rows = conn.execute(
                "SELECT record_id, group_key FROM group_members ORDER BY record_id"
            ).fetchall()
        finally:
            conn.close()
        assert members == records == 9
        assert groups == data["stats"]["groups"]
        expected = {rid: group_of(data, rid) for rid in range(1, 10)}
        persisted = {int(row["record_id"]): row["group_key"] for row in rows}
        assert persisted == expected


def test_unrelated_groups_keep_stable_keys(paths):
    db_path, csv_path = paths
    with make_client(db_path, csv_path) as client:
        key_before = group_of(state(client), A2)
        add_and_confirm(client, A1, B1, "same")
        data = state(client)
        assert group_of(data, A2) == key_before
        # A merged group naturally gets a membership-derived key.
        assert group_of(data, A1) != key_before
        assert group_of(data, A1) == group_of(data, B1)
