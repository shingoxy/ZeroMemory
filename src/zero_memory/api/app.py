import json
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from zero_memory import __version__
from zero_memory.activity.service import Activity
from zero_memory.api.activity import activity_router
from zero_memory.api.limits import BodyLimitMiddleware, RequestTooLarge
from zero_memory.api.observatory import observatory_router
from zero_memory.api.raw_history import raw_history_router
from zero_memory.core.auth import Auth, Principal
from zero_memory.core.backup import Backups
from zero_memory.core.config import Settings
from zero_memory.core.context import ContextBuilder
from zero_memory.core.errors import ServiceError
from zero_memory.core.identity import Identity
from zero_memory.core.store import Store, now
from zero_memory.extraction.service import Extractor
from zero_memory.importers.raw_history import recover_batches
from zero_memory.importers.service import Importer
from zero_memory.llm.provider import Models
from zero_memory.memory.everos import EverOSAdapter
from zero_memory.memory.sqlite import SQLiteBackend
from zero_memory.observatory.service import MemoryObservatoryAdapter
from zero_memory.schemas.models import (
    ChatRequest,
    IdentityPatch,
    ModelConfig,
    Proposal,
    Scope,
    Search,
    StrictModel,
)


class ProposeRequest(Proposal, Scope):
    pass


class ApproveRequest(Scope):
    supersedes: str | None = None
    confirm_external: bool = False


class EditRequest(ProposeRequest):
    expected_version: int = Field(ge=1)


class StartImport(StrictModel):
    conversation_ids: list[str] | None = Field(default=None, max_length=10000)
    since: str | None = None
    until: str | None = None
    skip_sensitive: bool = True


class ClientRequest(Scope):
    name: str = Field(min_length=1, max_length=100)
    writable: bool = False


class ExtractRequest(Scope):
    limit: int = Field(default=10, ge=1, le=30)
    consent: bool = False
    preview_hash: str | None = None


class MergeRequest(Scope):
    memory_ids: list[str] = Field(min_length=2, max_length=10)
    content: str = Field(min_length=1, max_length=12000)


def as_scope(value) -> Scope:
    return Scope(**{k: getattr(value, k) for k in Scope.model_fields})


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    store = Store(settings.home)
    identity = Identity(store)
    backend = SQLiteBackend(store)
    auth = Auth(store)
    models = Models(store, settings)
    importer = Importer(store, settings)
    extractor = Extractor(store, backend, models)
    backups = Backups(store, settings)
    activity = None
    if settings.activity_enabled:
        with store.lock(), store.read() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] == 0 and db.execute("SELECT count(*) FROM memories").fetchone()[0]:
                backups.create()
            activity = Activity(store, backend, identity)
    context = ContextBuilder(identity, backend, store, activity)

    @asynccontextmanager
    async def lifespan(app):
        importer.recover()
        recover_batches(store)
        if activity:
            activity.recover()
        yield

    app = FastAPI(
        title="零的记忆 / ZeroMemory", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.state.store, app.state.backend, app.state.identity = store, backend, identity
    app.state.models, app.state.importer, app.state.auth = models, importer, auth
    app.state.extractor, app.state.context = extractor, context
    app.state.activity = activity
    app.state.observatory = MemoryObservatoryAdapter(backend)
    if settings.observatory_enabled:
        app.include_router(observatory_router(app.state.observatory, auth))
    if activity:
        app.include_router(activity_router(activity, auth))
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    app.add_middleware(
        BodyLimitMiddleware,
        upload_limit=settings.max_upload_bytes + 1024 * 1024,
        restore_limit=settings.max_expanded_bytes + 1024 * 1024,
    )

    @app.middleware("http")
    async def local_boundary(request, call_next):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"error": {"code": "origin_denied", "message": "拒绝跨站请求"}}, 403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if (
                not request.headers.get("authorization", "").startswith("Bearer ")
                and request.headers.get("x-zero-request") != "1"
            ):
                return JSONResponse({"error": {"code": "csrf", "message": "缺少本地请求标识"}}, 403)
        try:
            limit = (
                settings.max_expanded_bytes + 1024 * 1024
                if request.url.path == "/v1/restore"
                else settings.max_upload_bytes + 1024 * 1024
            )
            if int(request.headers.get("content-length", 0)) > limit:
                return JSONResponse({"error": {"code": "upload_limit", "message": "请求体过大"}}, 413)
        except ValueError:
            return JSONResponse({"error": {"code": "content_length", "message": "无效的请求长度"}}, 400)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        )
        return response

    @app.exception_handler(ServiceError)
    async def service_error(request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, exc.status)

    @app.exception_handler(RequestTooLarge)
    async def oversized_body(request, exc):
        return JSONResponse({"error": {"code": "upload_limit", "message": "请求体过大"}}, 413)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            {
                "error": {
                    "code": "validation",
                    "message": "参数格式不正确",
                    "fields": [".".join(str(x) for x in e["loc"]) for e in exc.errors()],
                }
            },
            422,
        )

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request, exc):
        return JSONResponse(
            {"error": {"code": "storage", "message": "本地数据库不可用，请检查数据目录权限和磁盘空间"}}, 503
        )

    @app.exception_handler(OSError)
    @app.exception_handler(ValueError)
    async def local_data_error(request, exc):
        return JSONResponse(
            {
                "error": {
                    "code": "local_data",
                    "message": "本地数据或配置不可用，请检查数据目录权限、格式和磁盘空间",
                }
            },
            503,
        )

    def principal(request: Request):
        return auth.principal(request)

    def admin(p: Principal = Depends(principal)):
        p.require_admin()
        return p

    def web_admin(request: Request, p: Principal = Depends(admin)):
        if request.headers.get("authorization") or not request.cookies.get("zero_session"):
            raise ServiceError("web_session_required", "需要本机 Web 管理会话", 403)
        return p

    app.include_router(raw_history_router(store, web_admin))

    def selected_scope(p, user_id=None, project_id=None):
        scope = Scope(user_id=user_id or p.scope.user_id, project_id=project_id or p.scope.project_id)
        p.check(scope)
        return scope

    source_web = Path(__file__).resolve().parents[3] / "web"
    web = source_web if source_web.exists() else Path(__file__).resolve().parents[1] / "web"
    app.mount("/static", StaticFiles(directory=web), name="static")

    @app.get("/", include_in_schema=False)
    def homepage():
        response = FileResponse(web / "index.html")
        response.set_cookie(
            "zero_session", auth.admin_token, httponly=True, samesite="strict", max_age=8 * 3600
        )
        return response

    @app.get("/docs", include_in_schema=False)
    def local_api_docs():
        return FileResponse(web / "api.html")

    @app.get("/health")
    def health():
        with store.read() as db:
            db.execute("SELECT count(*) FROM memories").fetchone()
        result = {
            "status": "ok",
            "process_id": os.getpid(),
            "version": __version__,
            "agent_id": "zero-001",
            "memory": {
                "ready": True,
                "requested": settings.memory_backend,
                "active": "sqlite_fts5",
                "degraded": settings.memory_backend == "everos",
            },
            "llm": models.status(probe=True),
            "embedding": {"ready": False, "mode": "keyword_only"},
            "capabilities": {"observatory": settings.observatory_enabled, "activity": settings.activity_enabled},
        }
        # Public health omits endpoint/model details; full config requires administrator access.
        result["llm"] = {k: result["llm"][k] for k in ("ready", "reason", "local", "provider")}
        if settings.memory_backend == "everos":
            result["everos"] = EverOSAdapter(settings.everos_base_url).probe()
        return result

    @app.get("/v1/identity")
    def get_identity(user_id: str | None = None, project_id: str | None = None, p=Depends(principal)):
        scope = selected_scope(p, user_id, project_id)
        return {**identity.get(), "state": activity.persona.state(scope) if activity else identity.state()}

    @app.patch("/v1/identity")
    def patch_identity(body: IdentityPatch, p=Depends(admin)):
        return identity.update(body)

    @app.get("/v1/identity/avatar")
    def identity_avatar(p=Depends(principal)):
        avatar = identity.get().get("migration", {}).get("avatar")
        if avatar:
            root = store.home / "identity" / "assets"
            path = (store.home / "identity" / avatar).resolve()
            if path.parent == root.resolve() and path.suffix == ".png" and path.exists():
                return FileResponse(path, media_type="image/png")
        return FileResponse(web / "public" / "zero-avatar.png", media_type="image/png")

    @app.get("/v1/identity/versions")
    def versions(p=Depends(admin)):
        return identity.versions()

    @app.post("/v1/identity/rollback")
    def rollback(body: dict, p=Depends(admin)):
        try:
            return identity.rollback(
                int(body["version"]), int(body["expected_version"]), body.get("authorized") is True
            )
        except (KeyError, ValueError, TypeError):
            raise ServiceError("validation", "需要有效的 version、expected_version 和明确授权") from None

    @app.get("/v1/persona-state")
    def get_state(user_id: str | None = None, project_id: str | None = None, p=Depends(principal)):
        scope = selected_scope(p, user_id, project_id)
        return activity.persona.state(scope) if activity else {**identity.state(), "simulated": True}

    @app.post("/v1/persona-state")
    def update_state(body: dict, p=Depends(admin)):
        if activity:
            scope = Scope(**{k: body[k] for k in Scope.model_fields if k in body})
            state = activity.persona.state(scope)
            return activity.persona.advance(scope, body.get("event", "routine"), str(uuid4()), state["version"], "用户手动更新模拟状态")
        return identity.advance(body.get("event", "routine"))

    @app.post("/v1/memories/search")
    def search_memories(body: Search, p=Depends(principal)):
        p.check(as_scope(body))
        if body.review_origin:
            p.require_admin()
        if not p.admin and body.status != "approved":
            raise ServiceError("review_private", "客户端只能读取已批准记忆", 403)
        return {"items": backend.search(body), "offset": body.offset, "limit": body.limit}

    @app.post("/v1/memories/propose")
    def propose_memory(body: ProposeRequest, p=Depends(principal)):
        scope = as_scope(body)
        p.check(scope, write=True)
        proposal = Proposal(**{k: getattr(body, k) for k in Proposal.model_fields})
        if not p.admin:
            proposal = proposal.model_copy(
                update={
                    "source_type": "mcp_client",
                    "source_role": "assistant",
                    "inference_type": "assistant_inference",
                    "source_message_ids": [],
                    "source_message_keys": [],
                    "source_conversation_id": None,
                    "activity_goal_id": None,
                    "activity_episode_ids": [],
                    "activity_artifact_ids": [],
                }
            )
        return backend.propose(proposal, scope, p.name)

    @app.post("/v1/memories/merge")
    def merge_memories(body: MergeRequest, p=Depends(admin)):
        return backend.merge(as_scope(body), body.memory_ids, body.content)

    @app.post("/v1/memories/{memory_id}/approve")
    def approve_memory(memory_id: str, body: ApproveRequest, p=Depends(admin)):
        return backend.approve(
            memory_id, as_scope(body), supersedes=body.supersedes, confirm_external=body.confirm_external
        )

    @app.patch("/v1/memories/{memory_id}")
    def edit_memory(memory_id: str, body: EditRequest, p=Depends(admin)):
        return backend.edit(
            memory_id,
            as_scope(body),
            Proposal(**{k: getattr(body, k) for k in Proposal.model_fields}),
            body.expected_version,
        )

    @app.post("/v1/memories/{memory_id}/revoke")
    def revoke_memory(memory_id: str, body: Scope, p=Depends(admin)):
        return backend.transition(memory_id, body, "revoked")

    @app.post("/v1/memories/{memory_id}/reject")
    def reject_memory(memory_id: str, body: Scope, p=Depends(admin)):
        return backend.transition(memory_id, body, "rejected")

    @app.get("/v1/memories/{memory_id}/versions")
    def memory_versions(
        memory_id: str, user_id: str | None = None, project_id: str | None = None, p=Depends(admin)
    ):
        scope = selected_scope(p, user_id, project_id)
        backend.get(memory_id, scope)
        with store.read() as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT body FROM revisions WHERE memory_id=? ORDER BY version DESC", (memory_id,)
                )
            ]

    @app.post("/v1/memories/{memory_id}/undo")
    def undo(memory_id: str, body: dict, p=Depends(admin)):
        try:
            scope = Scope(**{k: body[k] for k in Scope.model_fields if k in body})
            return backend.undo(memory_id, scope, int(body["version"]))
        except (ValueError, KeyError, TypeError):
            raise ServiceError("validation", "请选择有效记忆版本") from None

    @app.delete("/v1/memories/{memory_id}")
    def delete_memory(memory_id: str, body: Scope, p=Depends(admin)):
        backend.delete(memory_id, body)
        return {
            "deleted": True,
            "note": "已清理活动记忆、修订和检索索引；原始来源与旧备份仍保留，彻底遗忘需另行清理",
        }

    @app.post("/v1/imports")
    def preview_import(file: UploadFile = File(...), scope: str = Form("{}"), p=Depends(admin)):
        try:
            parsed = Scope.model_validate_json(scope)
        except ValueError:
            raise ServiceError("scope", "作用域格式错误", 422) from None
        return importer.preview(file.file, file.filename or "input.json", parsed)

    @app.get("/v1/imports")
    def list_imports(user_id: str | None = None, project_id: str | None = None, p=Depends(web_admin)):
        scope = selected_scope(p, user_id, project_id)
        with store.read() as db:
            return [
                j
                for r in db.execute("SELECT body FROM imports ORDER BY rowid DESC LIMIT 200")
                for j in [json.loads(r[0])]
                if j["scope"] == scope.model_dump()
            ]

    @app.get("/v1/imports/{job_id}")
    def get_import(job_id: str, user_id: str | None = None, project_id: str | None = None, p=Depends(web_admin)):
        return importer.get(job_id, selected_scope(p, user_id, project_id))

    @app.post("/v1/imports/{job_id}/start")
    def start_import(
        job_id: str,
        body: StartImport,
        tasks: BackgroundTasks,
        user_id: str | None = None,
        project_id: str | None = None,
        p=Depends(admin),
    ):
        scope = selected_scope(p, user_id, project_id)
        job = importer.start(job_id, scope, **body.model_dump())
        tasks.add_task(importer.run, job_id, scope)
        return job

    @app.get("/v1/messages")
    def messages(
        user_id: str | None = None,
        project_id: str | None = None,
        conversation_id: str | None = None,
        query: str = Query("", max_length=500),
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
        p=Depends(web_admin),
    ):
        return {
            "items": importer.messages(
                selected_scope(p, user_id, project_id), conversation_id, offset, limit, query
            )
        }

    @app.post("/v1/extractions/quote/{message_key}")
    def quote(message_key: str, body: Scope, p=Depends(web_admin)):
        return extractor.quote(body, message_key)

    @app.post("/v1/extractions/preview")
    def extraction_preview(body: ExtractRequest, p=Depends(web_admin)):
        return extractor.preview(as_scope(body), body.limit)

    @app.post("/v1/extractions/run")
    def extraction_run(body: ExtractRequest, p=Depends(web_admin)):
        return extractor.run(as_scope(body), body.limit, body.consent, body.preview_hash)

    @app.get("/v1/context")
    def get_context(
        query: str = Query("", max_length=12000),
        budget: int = Query(4000, ge=1000, le=16000),
        user_id: str | None = None,
        project_id: str | None = None,
        p=Depends(principal),
    ):
        return context.build(selected_scope(p, user_id, project_id), query, budget)

    def chat_context(body, p):
        scope = as_scope(body)
        p.check(scope)
        return scope, context.build(scope, body.message, body.budget, body.session_id)

    @app.post("/v1/chat/preview")
    def chat_preview(body: ChatRequest, p=Depends(admin)):
        scope, built = chat_context(body, p)
        return {
            **built,
            "destination": models.get().model_dump(),
            "preview_hash": models.preview_hash(built["messages"], scope, "chat"),
        }

    @app.post("/v1/chat")
    def chat(body: ChatRequest, p=Depends(admin)):
        scope, built = chat_context(body, p)
        with store.lock():
            models.authorize_send(built["messages"], scope, "chat", body.consent, body.preview_hash)
            provider = models.provider()
        answer = provider.complete(built["messages"])
        with store.write() as db:
            for role, text in (("user", body.message), ("assistant", answer)):
                db.execute(
                    "INSERT INTO chat_messages VALUES(?,?,?,?,?,?,?,?)",
                    (str(uuid4()), *scope.sql(), body.session_id, role, text, now()),
                )
        if activity:
            state = activity.persona.state(scope)
            activity.persona.advance(scope, "engaged", str(uuid4()), state["version"], "本机对话已完成")
        else:
            identity.advance("engaged")
        return {
            "answer": answer,
            "agent_id": "zero-001",
            "identity_version": built["identity_version"],
            "memory_ids": [m["memory_id"] for m in built["memories"]],
            "session_id": body.session_id,
        }

    @app.get("/v1/model")
    def get_model(p=Depends(admin)):
        return models.status(probe=True)

    @app.patch("/v1/model")
    def set_model(body: ModelConfig, p=Depends(admin)):
        return models.save(body)

    @app.post("/v1/clients")
    def create_client(body: ClientRequest, p=Depends(admin)):
        return auth.create_client(body.name, as_scope(body), body.writable)

    @app.get("/v1/clients")
    def list_clients(p=Depends(admin)):
        with store.read() as db:
            return [
                {**json.loads(r["body"]), "token_id": r["hash"][:12]}
                for r in db.execute("SELECT * FROM clients")
            ]

    @app.delete("/v1/clients/{token_id}")
    def revoke_client(token_id: str, p=Depends(admin)):
        if len(token_id) != 12 or not all(x in "0123456789abcdef" for x in token_id):
            raise ServiceError("token_id", "无效令牌标识")
        with store.write() as db:
            db.execute("DELETE FROM clients WHERE substr(hash,1,12)=?", (token_id,))
        return {"revoked": True}

    @app.post("/v1/backup")
    def backup(p=Depends(admin)):
        return backups.create()

    @app.get("/v1/backups/{backup_id}")
    def download_backup(backup_id: UUID, p=Depends(admin)):
        path = store.home / "backups" / (str(backup_id) + ".zip")
        if not path.exists():
            raise ServiceError("not_found", "备份不存在", 404)
        return FileResponse(path, filename=path.name, media_type="application/zip")

    @app.post("/v1/restore")
    def restore(file: UploadFile = File(...), p=Depends(admin)):
        return backups.restore(file.file)

    @app.post("/v1/index/rebuild")
    def rebuild(p=Depends(admin)):
        backend.rebuild()
        return {"rebuilt": True}

    @app.get("/v1/system")
    def system(user_id: str | None = None, project_id: str | None = None, p=Depends(admin)):
        scope = selected_scope(p, user_id, project_id)
        with store.read() as db:
            counts = {
                "messages": db.execute(
                    "SELECT count(*) FROM messages WHERE user_id=? AND agent_id=? AND project_id=?",
                    scope.sql(),
                ).fetchone()[0],
                "memories": db.execute(
                    "SELECT count(*) FROM memories WHERE user_id=? AND agent_id=? AND project_id=?",
                    scope.sql(),
                ).fetchone()[0],
            }
            counts["extractions"] = {
                r[0]: r[1]
                for r in db.execute(
                    "SELECT t.status,count(*) FROM extraction_tasks t JOIN messages m ON m.key=t.message_key WHERE m.user_id=? AND m.agent_id=? AND m.project_id=? GROUP BY t.status",
                    scope.sql(),
                )
            }
            counts["conversations"] = db.execute(
                "SELECT count(DISTINCT conversation_id) FROM messages "
                "WHERE user_id=? AND agent_id=? AND project_id=?", scope.sql(),
            ).fetchone()[0]
        return {
            "home": str(store.home),
            "database": str(store.path),
            "scope": scope.model_dump(),
            "counts": counts,
            "backend": "sqlite_fts5",
            "cloud_enabled": models.get().allow_cloud,
        }

    return app
