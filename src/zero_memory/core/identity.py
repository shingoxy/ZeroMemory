import json

import yaml

from zero_memory.core.errors import ServiceError
from zero_memory.core.store import Store, atomic_json, now
from zero_memory.schemas.models import IdentityPatch, Soul, Style


class Identity:
    def __init__(self, store: Store):
        self.store = store
        self.root = store.home / "identity"
        with store.lock():
            if not (self.root / "current.json").exists():
                self._save(
                    {
                        "version": 1,
                        "soul": Soul().model_dump(),
                        "style": Style().model_dump(),
                        "updated_at": now(),
                        "reason": "initial",
                    }
                )
            if not (self.root / "state.json").exists():
                atomic_json(
                    self.root / "state.json",
                    {"mood": "calm", "patience_current": 65, "engagement": 50, "updated_at": now()},
                )
            self._project(self.get())

    def _project(self, value):
        for key in ("soul", "style"):
            path = self.root / f"{key}.yaml"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(yaml.safe_dump(value[key], allow_unicode=True, sort_keys=False), encoding="utf-8")
            tmp.replace(path)

    def _save(self, value):
        atomic_json(self.root / "versions" / f"{value['version']:06}.json", value)
        atomic_json(self.root / "current.json", {"version": value["version"]})
        self._project(value)

    def get(self):
        version = json.loads((self.root / "current.json").read_text(encoding="utf-8"))["version"]
        return json.loads((self.root / "versions" / f"{version:06}.json").read_text(encoding="utf-8"))

    def versions(self):
        return [
            {"version": v["version"], "updated_at": v["updated_at"], "reason": v["reason"]}
            for p in sorted((self.root / "versions").glob("*.json"), reverse=True)
            for v in [json.loads(p.read_text(encoding="utf-8"))]
        ]

    def update(self, patch: IdentityPatch, actor="admin", reason="manual"):
        if not patch.authorized:
            raise ServiceError("identity_authorization", "修改核心人格必须明确授权", 403)
        with self.store.lock():
            old = self.get()
            if old["version"] != patch.expected_version:
                raise ServiceError("version_conflict", "人格已更新，请刷新后再保存", 409)
            value = {
                **old,
                "version": old["version"] + 1,
                "soul": patch.soul.model_dump(),
                "style": patch.style.model_dump(),
                "updated_at": now(),
                "reason": reason,
            }
            self._save(value)
            with self.store.write() as db:
                self.store.audit(db, "identity_update", actor, str(value["version"]))
            return value

    def rollback(self, version: int, expected_version: int, authorized: bool):
        path = self.root / "versions" / f"{version:06}.json"
        if not path.exists():
            raise ServiceError("not_found", "身份版本不存在", 404)
        value = json.loads(path.read_text(encoding="utf-8"))
        return self.update(
            IdentityPatch(
                authorized=authorized,
                expected_version=expected_version,
                soul=value["soul"],
                style=value["style"],
            ),
            reason=f"rollback:{version}",
        )

    def state(self):
        with self.store.lock():
            return json.loads((self.root / "state.json").read_text(encoding="utf-8"))

    def advance(self, event: str):
        if event not in {"engaged", "routine", "reset"}:
            raise ServiceError("state_event", "未知状态事件")
        with self.store.lock():
            state = self.state()
            base = self.get()["soul"]["traits"]["patience"]
            state["patience_current"] = (
                base
                if event == "reset"
                else min(100, max(0, state["patience_current"] + (1 if event == "engaged" else -1)))
            )
            state["engagement"] = (
                50
                if event == "reset"
                else min(100, max(0, state["engagement"] + (3 if event == "engaged" else -2)))
            )
            state["mood"] = "curious" if state["engagement"] >= 65 else "calm"
            state["updated_at"] = now()
            atomic_json(self.root / "state.json", state)
            return state
