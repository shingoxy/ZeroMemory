from typing import Literal

from pydantic import Field

from zero_memory.schemas.models import Scope, Soul, Style

GoalStatus = Literal[
    "proposed", "ready", "running", "paused", "waiting_approval", "blocked", "completed", "cancelled"
]


class GoalInput(Scope):
    title: str = Field(min_length=1, max_length=300)
    why: str = Field(min_length=1, max_length=2000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=10)
    next_step: str = Field(min_length=1, max_length=2000)
    owner_kind: Literal["agent", "user"] = "agent"
    sensitivity: Literal["private", "sensitive"] = "private"
    due_at: str | None = None


class GoalPatch(Scope):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)
    status: GoalStatus | None = None
    next_step: str | None = Field(default=None, min_length=1, max_length=2000)
    checkpoint: dict = Field(default_factory=dict)
    acceptance_confirmed: bool = False


class RunInput(Scope):
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[\w.-]+$")
    action: Literal["memory_review", "source_review"] = "memory_review"
    query: str = Field(default="", max_length=500)
    source_id: str | None = None
    include_pending: bool = False


class SourceInput(Scope):
    label: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=64000)
    source_id: str = Field(min_length=1, max_length=1000)


class EpisodeInput(Scope):
    goal_id: str
    source_id: str = Field(min_length=1, max_length=300)
    kind: Literal["observation", "interpretation", "plan", "user_confirmation"] = "observation"
    observations: str = Field(min_length=1, max_length=4000)
    interpretation: str = Field(default="", max_length=4000)
    occurred_at: str | None = None


class InterestInput(Scope):
    topic: str = Field(min_length=1, max_length=150)
    evidence_episode_ids: list[str] = Field(min_length=1, max_length=30)


class MethodInput(Scope):
    title: str = Field(min_length=1, max_length=300)
    applicability: str = Field(min_length=1, max_length=2000)
    limitations: str = Field(min_length=1, max_length=2000)
    steps: list[str] = Field(min_length=1, max_length=6)
    source_goal_id: str


class MethodRevision(MethodInput):
    expected_version: int = Field(ge=1)


class MethodAttemptInput(Scope):
    goal_id: str


class MethodResultInput(Scope):
    success: bool
    reason: str = Field(min_length=1, max_length=2000)


class PersonaEvent(Scope):
    event: Literal["engaged", "routine", "rest", "reset"] = "routine"
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[\w.-]+$")
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)


class PersonaProposal(Scope):
    description: str = Field(min_length=1, max_length=2000)
    soul: Soul | None = None
    style: Style | None = None
