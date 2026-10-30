from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MemoryType = Literal[
    "user_profile",
    "agent_experience",
    "relationship",
    "episodic",
    "semantic",
    "procedural",
    "working_context",
]
Status = Literal["pending_review", "approved", "rejected", "revoked", "superseded"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scope(StrictModel):
    user_id: str = Field(default="default-user", min_length=1, max_length=100, pattern=r"^[\w.-]+$")
    agent_id: Literal["zero-001"] = "zero-001"
    project_id: str = Field(default="default", min_length=1, max_length=100, pattern=r"^[\w.-]+$")

    def sql(self):
        return self.user_id, self.agent_id, self.project_id


class SourceArtifact(StrictModel):
    source_file: str = Field(pattern=r"^[0-9a-f]{64}\.jsonl$")
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    line: int = Field(ge=1)
    record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    bundle_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    member: str


class Proposal(StrictModel):
    memory_type: MemoryType = "episodic"
    content: str = Field(min_length=1, max_length=12000)
    source_type: Literal[
        "manual",
        "chatgpt_export",
        "generic_import",
        "mcp_client",
        "local_chat",
        "chatgpt_conversation_summary",
        "agent_activity",
    ] = "manual"
    source_conversation_id: str | None = None
    source_message_ids: list[str] = Field(default_factory=list, max_length=30)
    source_message_keys: list[str] = Field(default_factory=list, max_length=30)
    source_role: Literal["user", "assistant", "system", "tool", "third_party", "mixed"] = "user"
    evidence: str = Field(default="", max_length=16000)
    event_time: str | None = None
    event_time_original: str | None = Field(default=None, max_length=100)
    event_time_precision: Literal["year", "month", "day", "instant"] | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    confidence: float = Field(default=0.6, ge=0, le=1)
    inference_type: Literal[
        "user_statement", "third_party_statement", "assistant_inference", "system_summary"
    ] = "user_statement"
    sensitivity: Literal["public", "private", "sensitive"] = "private"
    fact_key: str | None = Field(default=None, max_length=150)
    claim_kind: Literal["user_reported", "user_plan", "user_estimate", "assistant_proposal"] | None = None
    origin_quality: Literal["summary_only", "verbatim_export_text"] | None = None
    topic: str | None = Field(default=None, max_length=150)
    review_note: str = Field(default="", max_length=2000)
    dedup_key: str | None = Field(default=None, min_length=1, max_length=300)
    source_artifact: SourceArtifact | None = None
    summary_links: list[str] = Field(default_factory=list, max_length=10)
    activity_goal_id: str | None = None
    activity_episode_ids: list[str] = Field(default_factory=list, max_length=30)
    activity_artifact_ids: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("event_time", "valid_from", "valid_to")
    @classmethod
    def validate_time(cls, value):
        if value is not None:
            from datetime import UTC, datetime

            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("时间必须带时区")
            return dt.astimezone(UTC).isoformat()
        return value


class Memory(Proposal, Scope):
    memory_id: str
    extracted_at: str
    status: Status = "pending_review"
    version: int = 1
    supersedes: str | None = None
    conflicts: list[str] = Field(default_factory=list)
    duplicate_candidates: list[str] = Field(default_factory=list)
    evidence_verified: bool = False
    merged_from: list[str] = Field(default_factory=list)
    merged_evidence: list[dict] = Field(default_factory=list)


class Search(Scope):
    query: str = Field(default="", max_length=500)
    limit: int = Field(default=30, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=1000000)
    memory_type: MemoryType | None = None
    status: Status | None = "approved"
    source_type: str | None = None
    since: str | None = None
    until: str | None = None
    exclude_sensitive: bool = False


class Traits(StrictModel):
    curiosity: int = Field(default=90, ge=0, le=100)
    independence: int = Field(default=95, ge=0, le=100)
    directness: int = Field(default=85, ge=0, le=100)
    patience: int = Field(default=65, ge=0, le=100)
    humor: int = Field(default=55, ge=0, le=100)


class Soul(StrictModel):
    agent_id: Literal["zero-001"] = "zero-001"
    name: str = Field(default="零", min_length=1, max_length=30)
    english_name: str = Field(default="Zero", min_length=1, max_length=30)
    age: Literal[18] = 18
    gender_expression: Literal["女性"] = "女性"
    fictional: Literal[True] = True
    traits: Traits = Field(default_factory=Traits)


class Style(StrictModel):
    language: str = Field(default="中文", min_length=1, max_length=40)
    description: str = Field(
        default="聪明、好奇、独立、有主见、直率，略带俏皮和倔强。自然清楚，不机械迎合，不无理由夸奖；适度幽默，不攻击或操纵用户。",
        max_length=2000,
    )


class IdentityPatch(StrictModel):
    authorized: bool = False
    expected_version: int = Field(ge=1)
    soul: Soul
    style: Style


class ModelConfig(StrictModel):
    provider: Literal["none", "ollama", "openai"] = "none"
    model: str = Field(default="", max_length=150)
    base_url: str = Field(default="http://127.0.0.1:11434", max_length=500)
    allow_cloud: bool = False


class ChatRequest(Scope):
    message: str = Field(min_length=1, max_length=12000)
    session_id: str = Field(default="default", min_length=1, max_length=100, pattern=r"^[\w.-]+$")
    budget: int = Field(default=4000, ge=1000, le=16000)
    preview_hash: str | None = None
    consent: bool = False
