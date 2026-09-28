"""HTTP API (FastAPI) и раздача собранного фронтенда."""

from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .knowledge import CATEGORIES, MAX_UPLOAD_BYTES
from .service import AnalystService, BadRequest, Conflict, Forbidden, Unauthorized
from .storage import NotFound

log = logging.getLogger("analyst.api")

WEB_DIST = Path(os.environ.get("ANALYST_WEB_DIST", Path(__file__).resolve().parent.parent / "web" / "dist"))


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    context: dict[str, str] = {}


class ProjectPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    context: dict[str, str] | None = None


class MemberIn(BaseModel):
    username: str
    role: str


class TaskIn(BaseModel):
    problem: str = Field(min_length=1, max_length=20000)
    title: str = ""


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


class StepIn(BaseModel):
    instruction: str = Field(default="", max_length=20000)


def create_app(service: AnalystService) -> FastAPI:
    app = FastAPI(title="System Analyst Agent", version=__version__)
    api = "/api/v1"

    def error(status: int, code: str, message: str) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})

    @app.exception_handler(Unauthorized)
    async def _unauthorized(_: Request, e: Unauthorized) -> JSONResponse:
        return error(401, "UNAUTHORIZED", str(e))

    @app.exception_handler(Forbidden)
    async def _forbidden(_: Request, e: Forbidden) -> JSONResponse:
        return error(403, "FORBIDDEN", str(e))

    @app.exception_handler(NotFound)
    async def _not_found(_: Request, e: NotFound) -> JSONResponse:
        return error(404, "NOT_FOUND", f"Не найдено: {e}")

    @app.exception_handler(Conflict)
    async def _conflict(_: Request, e: Conflict) -> JSONResponse:
        return error(409, "CONFLICT", str(e))

    @app.exception_handler(BadRequest)
    async def _bad_request(_: Request, e: BadRequest) -> JSONResponse:
        return error(422, "VALIDATION_ERROR", str(e))

    @app.exception_handler(Exception)
    async def _internal(_: Request, e: Exception) -> JSONResponse:
        log.exception("unhandled error")
        return error(500, "INTERNAL_ERROR", "Внутренняя ошибка сервера")

    @app.middleware("http")
    async def request_log(request: Request, call_next: Any) -> Response:
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        if request.url.path.startswith(api):
            log.info("%s %s %s %.0fms rid=%s", request.method, request.url.path, response.status_code,
                     (time.perf_counter() - start) * 1000, request_id)
        return response

    def current_user(request: Request) -> dict[str, Any]:
        header = request.headers.get("Authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else None
        return service.authenticate(token)

    User = Depends(current_user)

    @app.get(f"{api}/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "model": service.llm.model}

    @app.get(f"{api}/me")
    def me(user: dict[str, Any] = User) -> dict[str, Any]:
        return {"username": user["username"], "is_admin": bool(user["is_admin"])}

    # --- проекты -------------------------------------------------------------------------

    @app.get(f"{api}/projects")
    def list_projects(user: dict[str, Any] = User) -> list[dict[str, Any]]:
        return service.store.projects_for_user(user["id"])

    @app.post(f"{api}/projects", status_code=201)
    def create_project(body: ProjectIn, user: dict[str, Any] = User) -> dict[str, Any]:
        return service.create_project(user, body.name, body.description, body.context)

    @app.get(f"{api}/projects/{{project_id}}")
    def get_project(project_id: str, user: dict[str, Any] = User) -> dict[str, Any]:
        role = service.require(user, project_id)
        return {**service.store.get_project(project_id), "role": role,
                "members": service.store.members(project_id)}

    @app.patch(f"{api}/projects/{{project_id}}")
    def patch_project(project_id: str, body: ProjectPatch, user: dict[str, Any] = User) -> dict[str, Any]:
        return service.update_project(user, project_id, body.name, body.description, body.context)

    @app.post(f"{api}/projects/{{project_id}}/members", status_code=204)
    def add_member(project_id: str, body: MemberIn, user: dict[str, Any] = User) -> Response:
        service.add_member(user, project_id, body.username, body.role)
        return Response(status_code=204)

    # --- документы и база знаний -------------------------------------------------------------

    @app.get(f"{api}/projects/{{project_id}}/documents")
    def list_documents(project_id: str, user: dict[str, Any] = User) -> list[dict[str, Any]]:
        service.require(user, project_id)
        return service.store.documents(project_id)

    @app.post(f"{api}/projects/{{project_id}}/documents", status_code=201)
    async def upload(project_id: str, file: UploadFile = File(...), category: str = Form(""),
                     user: dict[str, Any] = User) -> dict[str, Any]:
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        return service.upload_document(user, project_id, file.filename or "document.txt", data, category or None)

    @app.delete(f"{api}/projects/{{project_id}}/documents/{{document_id}}", status_code=202)
    def delete_document(project_id: str, document_id: str, user: dict[str, Any] = User) -> dict[str, Any]:
        return service.request_document_delete(user, project_id, document_id)

    @app.get(f"{api}/projects/{{project_id}}/knowledge")
    def knowledge(project_id: str, q: str = "", user: dict[str, Any] = User) -> Any:
        if q:
            return service.search(user, project_id, q)
        return {"categories": CATEGORIES, "tree": service.knowledge(user, project_id)}

    # --- задачи ---------------------------------------------------------------------------------

    @app.get(f"{api}/projects/{{project_id}}/tasks")
    def list_tasks(project_id: str, user: dict[str, Any] = User) -> list[dict[str, Any]]:
        service.require(user, project_id)
        return service.store.tasks(project_id)

    @app.post(f"{api}/projects/{{project_id}}/tasks", status_code=202)
    def create_task(project_id: str, body: TaskIn, user: dict[str, Any] = User) -> dict[str, Any]:
        return service.create_task(user, project_id, body.problem, body.title)

    @app.get(f"{api}/projects/{{project_id}}/tasks/{{task_id}}")
    def get_task(project_id: str, task_id: str, user: dict[str, Any] = User) -> dict[str, Any]:
        return service.task_detail(user, project_id, task_id)

    @app.post(f"{api}/projects/{{project_id}}/tasks/{{task_id}}/messages", status_code=202)
    def post_message(project_id: str, task_id: str, body: MessageIn, user: dict[str, Any] = User) -> dict[str, Any]:
        return service.post_message(user, project_id, task_id, body.text)

    @app.post(f"{api}/projects/{{project_id}}/tasks/{{task_id}}/steps/{{step}}", status_code=202)
    def run_step(project_id: str, task_id: str, step: str, body: StepIn | None = None,
                 user: dict[str, Any] = User) -> dict[str, Any]:
        return service.run_step(user, project_id, task_id, step, body.instruction if body else "")

    @app.get(f"{api}/projects/{{project_id}}/tasks/{{task_id}}/artifacts")
    def history(project_id: str, task_id: str, kind: str | None = None,
                user: dict[str, Any] = User) -> list[dict[str, Any]]:
        return service.artifact_history(user, project_id, task_id, kind)

    @app.get(f"{api}/projects/{{project_id}}/artifacts/{{artifact_id}}")
    def get_artifact(project_id: str, artifact_id: str, user: dict[str, Any] = User) -> dict[str, Any]:
        return service.artifact(user, project_id, artifact_id)

    @app.post(f"{api}/projects/{{project_id}}/artifacts/{{artifact_id}}/publish", status_code=202)
    def publish(project_id: str, artifact_id: str, user: dict[str, Any] = User) -> dict[str, Any]:
        return service.request_publish(user, project_id, artifact_id)

    @app.get(f"{api}/projects/{{project_id}}/tasks/{{task_id}}/export")
    def export(project_id: str, task_id: str, format: str = "md", user: dict[str, Any] = User) -> Response:
        data, media_type, filename = service.export(user, project_id, task_id, format)
        return Response(data, media_type=media_type, headers={
            "Content-Disposition": f"attachment; filename=\"export.{filename.rsplit('.', 1)[-1]}\"; "
                                   f"filename*=UTF-8''{quote(filename)}"})

    @app.get(f"{api}/jobs/{{job_id}}")
    def job(job_id: str, user: dict[str, Any] = User) -> dict[str, Any]:
        return service.job(user, job_id)

    # --- подтверждения и аудит ------------------------------------------------------------------

    @app.get(f"{api}/projects/{{project_id}}/actions")
    def actions(project_id: str, status: str | None = "pending", user: dict[str, Any] = User) -> list[dict[str, Any]]:
        return service.actions(user, project_id, status or None)

    @app.post(f"{api}/projects/{{project_id}}/actions/{{action_id}}/confirm")
    def confirm(project_id: str, action_id: str, user: dict[str, Any] = User) -> dict[str, Any]:
        return service.decide(user, project_id, action_id, approve=True)

    @app.post(f"{api}/projects/{{project_id}}/actions/{{action_id}}/reject")
    def reject(project_id: str, action_id: str, user: dict[str, Any] = User) -> dict[str, Any]:
        return service.decide(user, project_id, action_id, approve=False)

    @app.get(f"{api}/projects/{{project_id}}/audit")
    def audit(project_id: str, user: dict[str, Any] = User) -> list[dict[str, Any]]:
        return service.audit_log(user, project_id)

    # --- фронтенд -----------------------------------------------------------------------------------

    if (WEB_DIST / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            if path.startswith("api/"):
                raise NotFound("endpoint")
            file = (WEB_DIST / path).resolve()
            if path and file.is_file() and WEB_DIST.resolve() in file.parents:
                return FileResponse(file)
            return FileResponse(WEB_DIST / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        def no_frontend() -> dict[str, str]:
            return {"message": "Frontend не собран: cd web && npm install && npm run build. API: /docs"}

    return app
