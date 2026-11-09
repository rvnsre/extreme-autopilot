from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

TERMINAL = {"completed", "partial", "blocked", "failed", "cancelled"}
TOOLS = Literal[
    "observe",
    "browser_open",
    "browser_observe",
    "browser_focus",
    "browser_fill",
    "browser_press",
    "browser_click",
    "browser_select",
    "desktop_observe",
    "desktop_focus",
    "desktop_type",
    "desktop_key",
    "desktop_click",
    "desktop_launch",
    "file_read",
    "file_write",
    "file_list",
    "process",
    "http_get",
    "api_request",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobInput(StrictModel):
    prompt: str = Field(max_length=50000)
    budget_usd: float = Field(default=5, gt=0, le=100)
    max_seconds: int = Field(default=86400, ge=60, le=86400)

    @field_validator("prompt")
    @classmethod
    def brief_length(cls, text):
        text = text.strip()
        if len(text) < 200:
            raise ValueError("Tapşırıq ən azı 200 Unicode simvolu olmalıdır")
        return text


class Goal(StrictModel):
    id: str = Field(pattern=r"^g[1-9][0-9]?$", max_length=3)
    description: str = Field(min_length=5, max_length=1000)
    check: str = Field(min_length=5, max_length=1000)


class Plan(StrictModel):
    goals: list[Goal] = Field(min_length=1, max_length=12)
    approach: str = Field(min_length=5, max_length=3000)

    @field_validator("goals")
    @classmethod
    def unique_goals(cls, goals):
        if len({g.id for g in goals}) != len(goals):
            raise ValueError("Duplicate goal identifiers")
        return goals


class Action(StrictModel):
    tool: TOOLS
    goal_id: str = Field(default="", max_length=3)
    args: dict = Field(default_factory=dict)
    intent: Literal["read", "navigate", "local_change", "external_write"] = "read"
    authority_quote: str = Field(default="", max_length=2000)


class Continue(StrictModel):
    kind: Literal["action"]
    note: str = Field(min_length=1, max_length=1000)
    action: Action


class Evidence(StrictModel):
    goal_id: str
    action_id: str
    detail: str = Field(min_length=1, max_length=1000)


class Finish(StrictModel):
    kind: Literal["finish"]
    outcome: Literal["completed", "partial", "blocked"]
    summary: str = Field(min_length=1, max_length=4000)
    evidence: list[Evidence] = Field(max_length=30)
    remaining: list[str] = Field(max_length=20)


class Escalate(StrictModel):
    kind: Literal["escalate"]
    reason: str = Field(min_length=1, max_length=1000)


class Decision(StrictModel):
    decision: Annotated[Continue | Finish | Escalate, Field(discriminator="kind")]


class Verdict(StrictModel):
    verified: bool
    reason: str = Field(min_length=1, max_length=2000)
    satisfied_goals: list[str] = Field(max_length=12)


class ToolResult(StrictModel):
    ok: bool
    data: dict = Field(default_factory=dict)
    error: str = Field(default="", max_length=2000)
    unknown: bool = False


class WorkerResult(StrictModel):
    action_id: str
    result: ToolResult
