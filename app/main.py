from __future__ import annotations

import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from . import db, service
from .grouping import build_grouping

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_INSTANCE = os.environ.get("APP_INSTANCE") or socket.gethostname()


class PreviewRequest(BaseModel):
    operation: Literal["add", "withdraw"]
    record_a: Optional[int] = None
    record_b: Optional[int] = None
    relation: Optional[Literal["same", "diff"]] = None
    decision_id: Optional[int] = None


class ConfirmRequest(BaseModel):
    snapshot_digest: Optional[str] = None


def create_app(
    db_path: str | os.PathLike[str] | None = None,
    seed_csv: str | os.PathLike[str] | None = None,
    seed_limit: int | None = None,
    instance: str | None = None,
) -> FastAPI:
    resolved_db = str(db_path or db.DEFAULT_DB_PATH)
    resolved_csv = str(seed_csv or db.DEFAULT_SEED_CSV)
    resolved_limit = int(seed_limit if seed_limit is not None else db.DEFAULT_SEED_LIMIT)
    resolved_instance = instance or DEFAULT_INSTANCE

    Path(resolved_db).parent.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        info = db.init_and_seed(
            resolved_db,
            resolved_csv,
            resolved_limit,
            canonical_group_builder=build_grouping,
        )
        app.state.init_info = info
        yield

    app = FastAPI(
        title="供应商主体审校台",
        version="1.0.0",
        description="审核两套采购系统中同名供应商的同一/不同企业关系，并原子保存分组。",
        lifespan=lifespan,
    )
    app.state.instance = resolved_instance

    @app.middleware("http")
    async def instance_header(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-App-Instance"] = resolved_instance
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(service.ApiProblem)
    async def api_problem_handler(request: Request, exc: service.ApiProblem):
        return JSONResponse(status_code=exc.status, content=exc.body())

    def get_conn():
        conn = db.connect(resolved_db)
        try:
            yield conn
        finally:
            conn.close()

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    @app.get("/health")
    def health(conn=Depends(get_conn)):
        conn.execute("SELECT 1").fetchone()
        return {
            "status": "ok",
            "instance": resolved_instance,
            "database": resolved_db,
        }

    @app.get("/api/meta")
    def api_meta(conn=Depends(get_conn)):
        return {
            "ok": True,
            "instance": resolved_instance,
            "init": getattr(app.state, "init_info", {}),
            "previews": service.count_preview_statuses(conn),
        }

    @app.get("/api/state")
    def api_state(conn=Depends(get_conn)):
        return {"ok": True, "state": service.build_state(conn, resolved_instance)}

    @app.post("/api/previews")
    def api_create_preview(body: PreviewRequest, conn=Depends(get_conn)):
        if body.operation == "add":
            if body.record_a is None or body.record_b is None:
                raise service.ApiProblem(
                    400, "missing_records", "add 操作需要 record_a 和 record_b"
                )
            if body.relation is None:
                raise service.ApiProblem(
                    400, "missing_relation", "add 操作需要 relation（same 或 diff）"
                )
            return service.create_preview_add(
                conn, body.record_a, body.record_b, body.relation
            )
        if body.decision_id is None:
            raise service.ApiProblem(
                400, "missing_decision", "withdraw 操作需要 decision_id"
            )
        return service.create_preview_withdraw(conn, body.decision_id)

    @app.post("/api/previews/{preview_id}/confirm")
    def api_confirm(
        preview_id: str, body: ConfirmRequest, conn=Depends(get_conn)
    ):
        status, payload = service.confirm_preview(
            conn, preview_id, body.snapshot_digest
        )
        if status == 200:
            payload["state"] = service.build_state(conn, resolved_instance)
        return JSONResponse(status_code=status, content=payload)

    @app.post("/api/previews/{preview_id}/cancel")
    def api_cancel(preview_id: str, conn=Depends(get_conn)):
        return service.cancel_preview(conn, preview_id)

    return app


app = create_app()
