"""SQLite persistence, schema and one-time CSV/JSON seeding.

Records are immutable after the first import: the API never exposes a write
path for them and SQLite triggers reject UPDATE/DELETE on the records table.
The same holds for the ERP orders/invoices loaded once from the bundled JSON.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

DEFAULT_DB_PATH = os.environ.get("DB_PATH", "./data/review.db")
DEFAULT_SEED_CSV = os.environ.get("SEED_CSV", "./data/suppliers.csv")
DEFAULT_SEED_LIMIT = int(os.environ.get("SEED_LIMIT", "100"))
DEFAULT_SEED_DOCS = os.environ.get("SEED_DOCS", "./data/erp_documents.json")
DEFAULT_SEED_DOCS_LIMIT = int(os.environ.get("SEED_DOCS_LIMIT", "20"))

SCHEMA_STATEMENTS: Sequence[str] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS records (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        seq         INTEGER NOT NULL,
        source      TEXT NOT NULL,
        source_code TEXT NOT NULL,
        name        TEXT NOT NULL,
        UNIQUE (source, source_code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decisions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        record_a   INTEGER NOT NULL REFERENCES records(id),
        record_b   INTEGER NOT NULL REFERENCES records(id),
        relation   TEXT NOT NULL CHECK (relation IN ('same', 'diff')),
        created_at TEXT NOT NULL,
        CHECK (record_a < record_b),
        UNIQUE (record_a, record_b)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS groups (
        group_key    TEXT PRIMARY KEY,
        member_count INTEGER NOT NULL,
        updated_at   TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS group_members (
        record_id INTEGER PRIMARY KEY REFERENCES records(id),
        group_key TEXT NOT NULL REFERENCES groups(group_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS previews (
        id            TEXT PRIMARY KEY,
        created_at    TEXT NOT NULL,
        action_json   TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        digest        TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'pending',
        confirmed_at  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS erp_orders (
        id           TEXT PRIMARY KEY,
        seq          INTEGER NOT NULL,
        record_id    INTEGER NOT NULL REFERENCES records(id),
        amount_cents INTEGER NOT NULL CHECK (amount_cents > 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS erp_invoices (
        id           TEXT PRIMARY KEY,
        seq          INTEGER NOT NULL,
        record_id    INTEGER NOT NULL REFERENCES records(id),
        amount_cents INTEGER NOT NULL CHECK (amount_cents > 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schemes (
        scheme_id  TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS writeoffs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        scheme_id    TEXT NOT NULL REFERENCES schemes(scheme_id) ON DELETE CASCADE,
        order_id     TEXT NOT NULL REFERENCES erp_orders(id),
        invoice_id   TEXT NOT NULL REFERENCES erp_invoices(id),
        amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
        created_at   TEXT NOT NULL,
        UNIQUE (scheme_id, order_id, invoice_id)
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS records_are_immutable_update
    BEFORE UPDATE ON records
    BEGIN
        SELECT RAISE(ABORT, 'records are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS records_are_immutable_delete
    BEFORE DELETE ON records
    BEGIN
        SELECT RAISE(ABORT, 'records are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS erp_orders_are_immutable_update
    BEFORE UPDATE ON erp_orders
    BEGIN
        SELECT RAISE(ABORT, 'erp_orders are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS erp_orders_are_immutable_delete
    BEFORE DELETE ON erp_orders
    BEGIN
        SELECT RAISE(ABORT, 'erp_orders are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS erp_invoices_are_immutable_update
    BEFORE UPDATE ON erp_invoices
    BEGIN
        SELECT RAISE(ABORT, 'erp_invoices are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS erp_invoices_are_immutable_delete
    BEFORE DELETE ON erp_invoices
    BEGIN
        SELECT RAISE(ABORT, 'erp_invoices are immutable');
    END
    """,
)

# Accept both the shipped Chinese headers and common English spellings.
HEADER_ALIASES = {
    "source": {"来源", "source", "系统", "采购系统"},
    "source_code": {"源编号", "source_code", "code", "编号"},
    "name": {"企业名称", "name", "企业名", "供应商名称"},
}


def connect(db_path: str | os.PathLike[str]) -> sqlite3.Connection:
    # One connection per request.  FastAPI may run a sync dependency and the
    # sync endpoint on different worker threads, so do not pin the connection
    # to its creating thread.
    conn = sqlite3.connect(
        str(db_path),
        timeout=20.0,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    # Set the busy policy before touching journal mode: two Web processes may
    # open a brand-new database at exactly the same time.
    conn.execute("PRAGMA busy_timeout=20000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def write_tx(conn: sqlite3.Connection, attempts: int = 10) -> Iterator[None]:
    """BEGIN IMMEDIATE with bounded retry, then commit or roll back."""
    for attempt in range(attempts):
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            if attempt == attempts - 1:
                raise
            time.sleep(0.15 * (attempt + 1))
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_tx(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _read_seed_rows(csv_path: str | os.PathLike[str], limit: int) -> list[dict[str, str]]:
    path = Path(csv_path)
    if not path.exists():
        raise RuntimeError(f"seed CSV not found: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:  # empty file
            raise RuntimeError(f"seed CSV is empty: {path}") from exc

        indices: dict[str, int] = {}
        for index, column in enumerate(header):
            normalized = column.strip()
            for field, aliases in HEADER_ALIASES.items():
                if normalized in aliases:
                    indices[field] = index
                    break
        missing = {"source", "source_code", "name"} - set(indices)
        if missing:
            raise RuntimeError(
                f"seed CSV header is missing fields {sorted(missing)}; "
                f"expected 来源/源编号/企业名称 (source/source_code/name)"
            )

        rows: list[dict[str, str]] = []
        for line_no, raw in enumerate(reader, start=2):
            if not any(cell.strip() for cell in raw):
                continue
            try:
                source = raw[indices["source"]].strip()
                source_code = raw[indices["source_code"]].strip()
                name = raw[indices["name"]].strip()
            except IndexError as exc:
                raise RuntimeError(f"malformed row {line_no} in {path}") from exc
            if not source or not source_code or not name:
                raise RuntimeError(
                    f"row {line_no} in {path} has an empty 来源/源编号/企业名称"
                )
            rows.append(
                {"seq": len(rows) + 1, "source": source, "source_code": source_code, "name": name}
            )
            if len(rows) >= limit:
                break

    if not rows:
        raise RuntimeError(f"seed CSV has no data rows: {path}")
    return rows


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _read_seed_docs(
    json_path: str | os.PathLike[str], limit: int
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Read orders/invoices from the bundled JSON, at most ``limit`` of each.

    Every document carries its document id, the supplier's original record id
    and a positive integer amount in cents.  The file is strict-validated so a
    malformed seed fails loudly at startup instead of silently seeding less.
    """
    path = Path(json_path)
    if not path.exists():
        raise RuntimeError(f"seed documents JSON not found: {path}")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"seed documents JSON is invalid: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"seed documents JSON must be an object: {path}")

    def read_kind(kind: str, id_field: str) -> list[dict[str, object]]:
        raw_list = payload.get(kind)
        if not isinstance(raw_list, list):
            raise RuntimeError(f"seed documents JSON is missing a list '{kind}': {path}")
        docs: list[dict[str, object]] = []
        seen_ids: set[str] = set()
        for index, raw in enumerate(raw_list):
            where = f"{kind}[{index}]"
            if not isinstance(raw, dict):
                raise RuntimeError(f"{where} in {path} must be an object")
            doc_id = raw.get(id_field)
            record_id = raw.get("record_id")
            amount = raw.get("amount_cents")
            if not isinstance(doc_id, str) or not doc_id.strip():
                raise RuntimeError(f"{where} in {path} has an invalid {id_field}")
            doc_id = doc_id.strip()
            if doc_id in seen_ids:
                raise RuntimeError(f"{where} in {path} duplicates id {doc_id!r}")
            if isinstance(record_id, bool) or not isinstance(record_id, int):
                raise RuntimeError(f"{where} in {path} has an invalid record_id")
            if not _is_positive_int(amount):
                raise RuntimeError(
                    f"{where} in {path} must have a positive integer amount_cents"
                )
            seen_ids.add(doc_id)
            docs.append(
                {
                    "seq": len(docs) + 1,
                    "id": doc_id,
                    "record_id": record_id,
                    "amount_cents": amount,
                }
            )
            if len(docs) >= limit:
                break
        return docs

    orders = read_kind("orders", "order_id")
    invoices = read_kind("invoices", "invoice_id")
    if not orders and not invoices:
        raise RuntimeError(f"seed documents JSON has no orders or invoices: {path}")
    return orders, invoices


def init_and_seed(
    db_path: str | os.PathLike[str],
    csv_path: str | os.PathLike[str],
    seed_limit: int,
    canonical_group_builder=None,
    docs_path: str | os.PathLike[str] | None = None,
    docs_limit: int = DEFAULT_SEED_DOCS_LIMIT,
) -> dict[str, object]:
    """Create the schema, seed once, and canonicalise persisted groups.

    The whole operation is one IMMEDIATE transaction so two web processes
    starting at the same time cannot double-seed the CSV or the ERP documents.
    Documents are seeded only when ``docs_path`` is given and the file exists;
    an already-populated documents table is never touched again.
    """
    conn = connect(db_path)
    try:
        with write_tx(conn):
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)

            existing = int(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0])
            seeded = 0
            if existing == 0:
                rows = _read_seed_rows(csv_path, seed_limit)
                conn.executemany(
                    "INSERT INTO records (seq, source, source_code, name) VALUES (?, ?, ?, ?)",
                    [(r["seq"], r["source"], r["source_code"], r["name"]) for r in rows],
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('seed_csv', ?)",
                    (str(Path(csv_path).resolve()),),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('seed_count', ?)",
                    (str(len(rows)),),
                )
                seeded = len(rows)
                existing = len(rows)

            docs_seeded = {"orders": 0, "invoices": 0}
            docs_present = int(
                conn.execute("SELECT COUNT(*) FROM erp_orders").fetchone()[0]
            ) + int(conn.execute("SELECT COUNT(*) FROM erp_invoices").fetchone()[0])
            if docs_present == 0 and docs_path is not None and Path(docs_path).exists():
                orders, invoices = _read_seed_docs(docs_path, docs_limit)
                known_records = {
                    int(row[0]) for row in conn.execute("SELECT id FROM records")
                }
                for kind, docs in (("order", orders), ("invoice", invoices)):
                    for doc in docs:
                        if int(doc["record_id"]) not in known_records:
                            raise RuntimeError(
                                f"seed documents JSON references unknown "
                                f"record_id {doc['record_id']} ({kind} {doc['id']!r})"
                            )
                conn.executemany(
                    "INSERT INTO erp_orders (id, seq, record_id, amount_cents) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (d["id"], d["seq"], d["record_id"], d["amount_cents"])
                        for d in orders
                    ],
                )
                conn.executemany(
                    "INSERT INTO erp_invoices (id, seq, record_id, amount_cents) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (d["id"], d["seq"], d["record_id"], d["amount_cents"])
                        for d in invoices
                    ],
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('seed_docs', ?)",
                    (str(Path(docs_path).resolve()),),
                )
                docs_seeded = {"orders": len(orders), "invoices": len(invoices)}

            if canonical_group_builder is not None and existing:
                record_rows = conn.execute("SELECT id FROM records ORDER BY id").fetchall()
                decision_rows = conn.execute(
                    "SELECT id, record_a, record_b, relation FROM decisions ORDER BY id"
                ).fetchall()
                grouping = canonical_group_builder(
                    [int(r["id"]) for r in record_rows],
                    [dict(r) for r in decision_rows],
                )
                stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                conn.execute("DELETE FROM group_members")
                conn.execute("DELETE FROM groups")
                conn.executemany(
                    "INSERT INTO groups (group_key, member_count, updated_at) VALUES (?, ?, ?)",
                    [(key, len(ids), stamp) for key, ids in grouping.by_key.items()],
                )
                conn.executemany(
                    "INSERT INTO group_members (record_id, group_key) VALUES (?, ?)",
                    [(rid, key) for key, ids in grouping.by_key.items() for rid in ids],
                )

        return {
            "records": existing,
            "seeded_now": seeded,
            "documents": docs_seeded,
            "db_path": str(Path(db_path).resolve()),
        }
    finally:
        conn.close()
