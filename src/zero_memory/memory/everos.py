import httpx

from zero_memory.core.config import is_loopback_url
from zero_memory.core.errors import ServiceError

VERIFIED_VERSION = "1.5.1"
VERIFIED_COMMIT = "5b0979b1b7f3a0cbe235756ff098515ec490753e"


class EverOSAdapter:
    """Official HTTP contract probe. No private table access, implicit upload, or dual writes.

    The v0.1 primary backend remains SQLite until lossless review/delete mapping is validated.
    This explicit boundary prevents reporting an HTTP ingest as an approved ZeroMemory fact.
    """

    def __init__(self, base_url: str):
        if not is_loopback_url(base_url):
            raise ServiceError("everos_local_only", "EverOS 地址必须为本机回环地址")
        self.base_url = base_url.rstrip("/")

    def probe(self) -> dict:
        try:
            with httpx.Client(timeout=2, trust_env=False, follow_redirects=False) as client:
                health = client.get(self.base_url + "/health")
                health.raise_for_status()
                spec = client.get(self.base_url + "/openapi.json")
                spec.raise_for_status()
                paths = spec.json().get("paths", {})
                endpoints = {x: "/api/v2/memory/" + x in paths for x in ("add", "flush", "search")}
                return {
                    "reachable": True,
                    "contract": endpoints,
                    "verified_upstream_version": VERIFIED_VERSION,
                    "primary_write_enabled": False,
                    "reason": "尚未验证审核、完整溯源及删除映射",
                }
        except (httpx.HTTPError, ValueError):
            return {
                "reachable": False,
                "primary_write_enabled": False,
                "reason": "本地 EverOS 未就绪，使用 SQLite FTS5 持久化降级后端",
            }
