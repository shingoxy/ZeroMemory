import hmac
import json
import secrets
from dataclasses import dataclass

from fastapi import Request

from zero_memory.core.errors import ServiceError
from zero_memory.core.store import Store, digest, dumps, now
from zero_memory.schemas.models import Scope


@dataclass
class Principal:
    name: str
    admin: bool
    scope: Scope
    writable: bool = False

    def check(self, scope: Scope, write=False):
        if not self.admin and scope != self.scope:
            raise ServiceError("scope_denied", "客户端无权访问此用户或项目", 403)
        if write and not (self.admin or self.writable):
            raise ServiceError("read_only", "此客户端仅可读取已批准记忆", 403)

    def require_admin(self):
        if not self.admin:
            raise ServiceError("admin_required", "需要本机管理界面权限", 403)


class Auth:
    def __init__(self, store: Store):
        self.store = store
        path = store.home / "admin.token"
        with store.lock():
            if not path.exists():
                path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
            self.admin_token = path.read_text(encoding="utf-8").strip()

    def principal(self, request: Request):
        header = request.headers.get("authorization", "")
        token = header[7:] if header.startswith("Bearer ") else request.cookies.get("zero_session", "")
        if hmac.compare_digest(token.encode(), self.admin_token.encode()):
            return Principal("local-admin", True, Scope(), True)
        with self.store.read() as db:
            row = (
                db.execute("SELECT body FROM clients WHERE hash=?", (digest(token),)).fetchone()
                if token
                else None
            )
        if not row:
            raise ServiceError("unauthorized", "请先打开本地管理页面，或配置有效的作用域令牌", 401)
        client = json.loads(row[0])
        return Principal(client["name"], False, Scope(**client["scope"]), client["writable"])

    def create_client(self, name, scope: Scope, writable=False):
        token = secrets.token_urlsafe(40)
        client = {"name": name, "scope": scope.model_dump(), "writable": writable, "created_at": now()}
        with self.store.write() as db:
            db.execute("INSERT INTO clients VALUES(?,?)", (digest(token), dumps(client)))
            self.store.audit(db, "client_create", "admin", digest(token)[:12], client)
        return {**client, "token": token, "token_id": digest(token)[:12]}
