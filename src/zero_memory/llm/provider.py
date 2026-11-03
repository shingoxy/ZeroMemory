from typing import Protocol
from urllib.parse import urlsplit

import httpx

from zero_memory.core.config import is_loopback_url
from zero_memory.core.errors import ServiceError
from zero_memory.core.store import Store, atomic_json, digest, dumps
from zero_memory.llm.windows_credentials import read_deepseek_key
from zero_memory.schemas.models import ModelConfig


class ModelProvider(Protocol):
    def complete(self, messages: list[dict], *, json_mode: bool = False) -> str: ...


class HTTPProvider:
    def __init__(self, config: ModelConfig, api_key: str = ""):
        self.config, self.api_key = config, api_key

    def complete(self, messages: list[dict], *, json_mode=False) -> str:
        config = self.config
        if config.provider == "none" or not config.model:
            raise ServiceError(
                "llm_unconfigured", "未配置模型：导入、身份、关键词检索仍可用。请配置本地 Ollama 模型。", 503
            )
        if not is_loopback_url(config.base_url) and not config.allow_cloud:
            raise ServiceError("cloud_disabled", "云端发送被禁用", 403)
        body = {"model": config.model, "messages": messages, "stream": False}
        if config.provider == "ollama":
            path = "/api/chat"
            if json_mode:
                body["format"] = "json"
        else:
            path = "/chat/completions"
            if json_mode:
                body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": "Bearer " + self.api_key} if self.api_key else {}
        try:
            with httpx.Client(timeout=90, trust_env=False, follow_redirects=False) as client:
                response = client.post(config.base_url.rstrip("/") + path, json=body, headers=headers)
                response.raise_for_status()
                if len(response.content) > 2_000_000:
                    raise ValueError("response size")
                result = response.json()
                text = (
                    result["message"]["content"]
                    if config.provider == "ollama"
                    else result["choices"][0]["message"]["content"]
                )
                if not isinstance(text, str):
                    raise ValueError("content type")
                return text
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            raise ServiceError(
                "llm_request", "模型请求失败：请检查地址、模型名称和服务状态（未记录请求正文或密钥）", 502
            ) from None


class Models:
    def __init__(self, store: Store, settings):
        self.store, self.settings = store, settings
        self.path = store.home / "model.json"
        if not self.path.exists():
            self.save(
                ModelConfig(
                    provider=settings.llm_provider,
                    model=settings.llm_model,
                    base_url=settings.llm_base_url,
                    allow_cloud=settings.allow_cloud,
                )
            )

    def get(self) -> ModelConfig:
        with self.store.lock():
            return ModelConfig.model_validate_json(self.path.read_text(encoding="utf-8"))

    def save(self, config: ModelConfig):
        parsed = urlsplit(config.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ServiceError("model_url", "模型地址无效；密钥只能来自环境变量")
        if not is_loopback_url(config.base_url) and (not config.allow_cloud or parsed.scheme != "https"):
            raise ServiceError("cloud_disabled", "非本地模型必须使用 HTTPS 并明确启用云端")
        with self.store.lock():
            atomic_json(self.path, config.model_dump())
        return self.status(probe=False)

    def status(self, probe=False):
        config = self.get()
        local = is_loopback_url(config.base_url)
        ready = False
        reason = "未配置模型" if config.provider == "none" or not config.model else "尚未探测"
        if probe and config.provider != "none" and config.model and local:
            try:
                with httpx.Client(timeout=1.5, trust_env=False, follow_redirects=False) as client:
                    response = client.get(
                        config.base_url.rstrip("/")
                        + ("/api/tags" if config.provider == "ollama" else "/models")
                    )
                    response.raise_for_status()
                    payload = response.json()
                    names = (
                        [m.get("name", "") for m in payload.get("models", [])]
                        if config.provider == "ollama"
                        else [m.get("id", "") for m in payload.get("data", [])]
                    )
                    ready = config.model in names or (
                        config.provider == "ollama" and config.model + ":latest" in names
                    )
                    reason = "本地模型就绪" if ready else "服务可达，但未找到配置的模型"
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                reason = "本地模型服务未就绪"
        return {
            **config.model_dump(),
            "local": local,
            "ready": ready,
            "reason": reason,
            "api_key_present": bool(self._api_key(config)),
            "embedding_ready": False,
            "embedding_reason": "v0.1 使用关键词检索，未启用向量索引",
        }

    def _api_key(self, config):
        if self.settings.llm_api_key:
            return self.settings.llm_api_key
        if config.provider == "openai" and config.base_url.rstrip("/") == "https://api.deepseek.com":
            try:
                return read_deepseek_key() or ""
            except (OSError, UnicodeError):
                return ""
        return ""

    def provider(self):
        config = self.get()
        return HTTPProvider(config, self._api_key(config))

    def preview_hash(self, messages, scope, purpose):
        # No preview content is persisted. Hash binds consent to exact messages/config/scope.
        return digest(dumps([self.get().model_dump(), messages, scope.model_dump(), purpose]))

    def authorize_send(self, messages, scope, purpose, consent=False, preview_hash=None):
        config = self.get()
        if config.provider == "none" or not config.model:
            raise ServiceError(
                "llm_unconfigured", "未配置本地模型。可继续导入、审核和检索，提炼任务将等待模型。", 503
            )
        if not is_loopback_url(config.base_url):
            if not config.allow_cloud:
                raise ServiceError("cloud_disabled", "云端连接未启用", 403)
            if not consent or preview_hash != self.preview_hash(messages, scope, purpose):
                raise ServiceError(
                    "context_consent",
                    "须先查看本次完整发送上下文，并明确确认发送；上下文变更后需重新预览",
                    409,
                )
