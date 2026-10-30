import json
import re
from pathlib import Path
from uuid import uuid4

from zero_memory.core.errors import ServiceError
from zero_memory.core.store import Store, digest, dumps, now
from zero_memory.importers.chatgpt import records, timestamp
from zero_memory.schemas.models import Scope


class Importer:
    def __init__(self, store: Store, settings):
        self.store, self.settings = store, settings
        with store.write() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS conversations(
                user_id TEXT, agent_id TEXT, project_id TEXT, id TEXT, body TEXT,
                PRIMARY KEY(user_id,agent_id,project_id,id))""")

    def _save(self, db, job):
        db.execute("INSERT OR REPLACE INTO imports VALUES(?,?)", (job["id"], dumps(job)))

    def get(self, job_id: str, scope: Scope):
        with self.store.read() as db:
            row = db.execute("SELECT body FROM imports WHERE id=?", (job_id,)).fetchone()
            if not row or json.loads(row[0])["scope"] != scope.model_dump():
                raise ServiceError("not_found", "导入任务不存在", 404)
            return json.loads(row[0])

    @staticmethod
    def key(record, scope):
        # A converter's source filename is transport metadata, not a new message version.
        raw = record["raw"]
        if raw.get("record_type") == "conversation_message":
            raw = {k: raw.get(k) for k in ("role", "content", "created_at", "parent_id")}
        content_hash = digest(
            dumps({**{k: record[k] for k in ("role", "content", "timestamp", "parent_id", "node_id")},
                   "raw": raw})
        )
        return digest(
            dumps([*scope.sql(), record["conversation_id"], record["message_id"], content_hash])
        ), content_hash

    def preview(self, stream, filename: str, scope: Scope):
        suffix = Path(filename).suffix.lower()
        if suffix not in {".zip", ".json", ".jsonl"}:
            raise ServiceError("file_type", "仅接受 ZIP、JSON、JSONL")
        temp = self.store.home / "sources" / (str(uuid4()) + ".upload")
        size = 0
        try:
            with temp.open("wb") as target:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.settings.max_upload_bytes:
                        raise ServiceError("upload_limit", "上传文件超过 64 MiB 限制", 413)
                    target.write(chunk)
            import hashlib

            hasher = hashlib.sha256()
            with temp.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    hasher.update(chunk)
            source_hash = hasher.hexdigest()
            source = temp.with_name(source_hash + suffix)
            with self.store.lock():
                if source.exists():
                    temp.unlink()
                else:
                    temp.replace(source)
            counts, total, fresh = {}, 0, 0
            with self.store.read() as db:
                seen = set()
                for record in records(source, self.settings):
                    cid = record["conversation_id"]
                    counts.setdefault(cid, {"id": cid, "title": record.get("title", cid), "messages": 0})
                    if record["kind"] == "message":
                        counts[cid]["messages"] += 1
                        total += 1
                        key, _ = self.key(record, scope)
                        if (
                            key not in seen
                            and not db.execute("SELECT 1 FROM messages WHERE key=?", (key,)).fetchone()
                        ):
                            fresh += 1
                        seen.add(key)
            job = {
                "id": str(uuid4()),
                "scope": scope.model_dump(),
                "source": source.name,
                "source_hash": source_hash,
                "filename": Path(filename).name,
                "size": size,
                "created_at": now(),
                "status": "preview",
                "conversations": list(counts.values()),
                "total_messages": total,
                "estimated_new": fresh,
                "processed": 0,
                "inserted": 0,
                "duplicates": 0,
                "failed": 0,
                "errors": [],
                "cursor": 0,
                "waiting_extraction": 0,
                "selection": {},
                "skip_sensitive": True,
            }
            with self.store.write() as db:
                self._save(db, job)
            return job
        finally:
            temp.unlink(missing_ok=True)

    def start(self, job_id, scope, conversation_ids=None, since=None, until=None, skip_sensitive=True):
        since, until = timestamp(since), timestamp(until)
        if since and until and since > until:
            raise ServiceError("time_range", "起始时间不能晚于结束时间")
        with self.store.write() as db:
            job = self.get(job_id, scope)
            if job["status"] in {"queued", "running"}:
                raise ServiceError("import_running", "任务正在运行", 409)
            selection = {"conversation_ids": conversation_ids, "since": since, "until": until}
            if job["cursor"] and job["selection"] != selection:
                raise ServiceError("import_selection", "断点续跑不能更改选择范围，请重新预览", 409)
            job.update(
                status="queued", selection=selection, skip_sensitive=skip_sensitive, errors=[], failed=0
            )
            self._save(db, job)
        return job

    def run(self, job_id: str, scope: Scope):
        # A job-specific file lock avoids duplicate workers across processes, without blocking UI reads.
        import portalocker

        with portalocker.Lock(str(self.store.home / "sources" / (job_id + ".lock")), timeout=1):
            job = self.get(job_id, scope)
            if job["status"] != "queued":
                return
            job["status"] = "running"
            with self.store.write() as db:
                self._save(db, job)
            batch = []
            try:
                if not re.fullmatch(r"[0-9a-f]{64}\.(json|jsonl|zip)", job["source"]):
                    raise ServiceError("source_path", "导入来源路径无效，拒绝读取")
                for index, record in enumerate(
                    records(self.store.home / "sources" / job["source"], self.settings)
                ):
                    if index < job["cursor"]:
                        continue
                    batch.append((index, record))
                    if len(batch) >= 200:
                        self._batch(job, scope, batch)
                        batch.clear()
                if batch:
                    self._batch(job, scope, batch)
                job["status"] = "completed"
            except Exception as exc:
                job["status"], job["failed"] = "failed", job["failed"] + 1
                job["errors"] = [
                    {
                        "cursor": job["cursor"],
                        "code": getattr(exc, "code", "import_error"),
                        "message": getattr(exc, "message", "导入中断，可从已提交的批次重试"),
                    }
                ]
            with self.store.write() as db:
                self._save(db, job)

    def _batch(self, original_job, scope, batch):
        job = json.loads(dumps(original_job))
        with self.store.write() as db:
            for index, record in batch:
                cid = record["conversation_id"]
                chosen = job["selection"]
                keep = not chosen.get("conversation_ids") or cid in chosen["conversation_ids"]
                if record["kind"] == "conversation":
                    if keep:
                        db.execute(
                            "INSERT OR REPLACE INTO conversations VALUES(?,?,?,?,?)",
                            (*scope.sql(), cid, dumps(record)),
                        )
                else:
                    stamp = record["timestamp"]
                    if chosen.get("since") and (not stamp or stamp < chosen["since"]):
                        keep = False
                    if chosen.get("until") and (not stamp or stamp > chosen["until"]):
                        keep = False
                    if keep:
                        key, content_hash = self.key(record, scope)
                        record.update(
                            source_file=job["source"], source_hash=job["source_hash"], import_id=job["id"]
                        )
                        inserted = db.execute(
                            "INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                key,
                                *scope.sql(),
                                cid,
                                record["message_id"],
                                content_hash,
                                record["role"],
                                stamp,
                                record["content"],
                                dumps(record),
                            ),
                        ).rowcount
                        job["processed"] += 1
                        job["inserted"] += inserted
                        job["duplicates"] += 1 - inserted
                        if inserted and record["role"] == "user" and record["content"].strip():
                            sensitive = any(
                                x in record["content"].lower()
                                for x in ("密码", "病历", "账户余额", "password", "api_key", "诊断", "银行卡")
                            )
                            status = (
                                "skipped_sensitive" if job["skip_sensitive"] and sensitive else "waiting_llm"
                            )
                            db.execute(
                                "INSERT OR IGNORE INTO extraction_tasks VALUES(?,?,NULL)", (key, status)
                            )
                            job["waiting_extraction"] += status == "waiting_llm"
                job["cursor"] = index + 1
            self._save(db, job)
        original_job.update(job)

    def recover(self):
        with self.store.write() as db:
            for row in db.execute("SELECT body FROM imports").fetchall():
                job = json.loads(row[0])
                if job["status"] in {"running", "queued"}:
                    job["status"] = "interrupted"
                    self._save(db, job)

    def messages(self, scope, conversation_id=None, offset=0, limit=100, query=""):
        with self.store.read() as db:
            rows = db.execute(
                "SELECT key,body FROM messages WHERE user_id=? AND agent_id=? AND project_id=?"
                + (" AND conversation_id=?" if conversation_id else "")
                + (" AND instr(content,?)>0" if query else "")
                + " ORDER BY conversation_id,timestamp IS NULL,timestamp,rowid LIMIT ? OFFSET ?",
                (*scope.sql(), *((conversation_id,) if conversation_id else ()),
                 *((query,) if query else ()), limit, offset),
            )
            return [{"key": r[0], **json.loads(r[1])} for r in rows]
