"""回访归因领域模型：反馈、事项、证据、承诺、结论、政策版本与审计事件。

所有模型均为不可变 dataclass，状态迁移由 service 层通过 dataclasses.replace 完成，
并提供 to_dict / from_dict 以支持 JSON 持久化与接口输出。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

# --- 来源 ---
SOURCE_NAMED = "named"
SOURCE_ANONYMOUS = "anonymous"
SOURCE_KINDS = (SOURCE_NAMED, SOURCE_ANONYMOUS)

# --- 反馈立场：问题 / 反例（地区政策差异等）/ 一般 ---
STANCE_PROBLEM = "problem"
STANCE_COUNTEREXAMPLE = "counterexample"
STANCE_GENERAL = "general"
STANCES = (STANCE_PROBLEM, STANCE_COUNTEREXAMPLE, STANCE_GENERAL)

# --- 反馈状态 ---
FEEDBACK_OPEN = "open"
FEEDBACK_MERGED = "merged"

# --- 事项状态 ---
ISSUE_OPEN = "open"
ISSUE_IN_PROGRESS = "in_progress"
ISSUE_CONCLUDED = "concluded"
ISSUE_STATUSES = (ISSUE_OPEN, ISSUE_IN_PROGRESS, ISSUE_CONCLUDED)

# --- 影响范围等级 ---
IMPACT_LEVELS = ("individual", "community", "city", "multi_city")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    """统一转换为 UTC ISO-8601 字符串。"""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(raw: str) -> datetime:
    """解析 ISO-8601 字符串；缺省时区按 UTC 处理。"""
    moment = datetime.fromisoformat(raw)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Feedback:
    """一条原始回访反馈。合并只改变 state/merged_*，提交者与提交时间永不改写。"""

    id: str
    source_kind: str           # named | anonymous
    source_token: str          # 实名 id，或 "anon:" + HMAC 伪名（原始身份不落盘）
    scenario: str              # 场景，如 办事大厅 / 移动端 / 电话回访
    policy_version: str        # 提交时适用的政策版本
    regions: tuple[str, ...]   # 影响范围（地区编码）
    impact_level: str          # individual | community | city | multi_city
    stance: str                # problem | counterexample | general
    text: str
    submitted_at: str          # 原始提交时间（ISO），合并后仍保留
    state: str = FEEDBACK_OPEN
    merged_into: str | None = None
    merged_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Feedback":
        return Feedback(
            id=data["id"],
            source_kind=data["source_kind"],
            source_token=data["source_token"],
            scenario=data["scenario"],
            policy_version=data["policy_version"],
            regions=tuple(data["regions"]),
            impact_level=data["impact_level"],
            stance=data["stance"],
            text=data["text"],
            submitted_at=data["submitted_at"],
            state=data.get("state", FEEDBACK_OPEN),
            merged_into=data.get("merged_into"),
            merged_at=data.get("merged_at"),
        )


@dataclass(frozen=True, slots=True)
class Evidence:
    id: str
    issue_id: str
    kind: str        # log | screenshot | metric | link | ...
    ref: str         # 证据引用（URI / 工单号 / 指标名）
    note: str
    added_by: str
    added_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Evidence":
        return Evidence(**data)


@dataclass(frozen=True, slots=True)
class Assignment:
    owner: str
    team: str
    assigned_by: str
    assigned_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Assignment":
        return Assignment(**data)


@dataclass(frozen=True, slots=True)
class Commitment:
    """有期限的处理承诺。重新承诺会替换本对象并清零当前升级级别（历史见事件流）。"""

    deadline: str    # ISO 截止时间
    note: str
    made_by: str
    made_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Commitment":
        return Commitment(**data)


@dataclass(frozen=True, slots=True)
class Conclusion:
    """改进结论。记录依据了哪些反馈（supports）与反例（counterexamples），
    并冻结结论作出时的政策版本，后续政策更新不得影响已结论事项。"""

    decision: str
    rationale: str
    supports: tuple[str, ...]          # 支撑结论的反馈 id
    counterexamples: tuple[str, ...]   # 作为反例的反馈 id
    decided_by: str
    decided_at: str
    policy_version_at_decision: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Conclusion":
        return Conclusion(
            decision=data["decision"],
            rationale=data["rationale"],
            supports=tuple(data["supports"]),
            counterexamples=tuple(data["counterexamples"]),
            decided_by=data["decided_by"],
            decided_at=data["decided_at"],
            policy_version_at_decision=data["policy_version_at_decision"],
        )


@dataclass(frozen=True, slots=True)
class Issue:
    """归并后的事项（相似意见簇）。"""

    id: str
    title: str
    scenario: str
    regions: tuple[str, ...]
    policy_version: str
    status: str
    feedback_ids: tuple[str, ...]
    evidence: tuple[Evidence, ...] = ()
    assignment: Assignment | None = None
    commitment: Commitment | None = None
    conclusion: Conclusion | None = None
    escalation_level: int = 0
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Issue":
        return Issue(
            id=data["id"],
            title=data["title"],
            scenario=data["scenario"],
            regions=tuple(data["regions"]),
            policy_version=data["policy_version"],
            status=data["status"],
            feedback_ids=tuple(data["feedback_ids"]),
            evidence=tuple(Evidence.from_dict(e) for e in data.get("evidence", ())),
            assignment=Assignment.from_dict(data["assignment"]) if data.get("assignment") else None,
            commitment=Commitment.from_dict(data["commitment"]) if data.get("commitment") else None,
            conclusion=Conclusion.from_dict(data["conclusion"]) if data.get("conclusion") else None,
            escalation_level=data.get("escalation_level", 0),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


@dataclass(frozen=True, slots=True)
class PolicyVersion:
    version: str
    note: str
    regions: tuple[str, ...]
    registered_at: str
    origin: str      # registered（显式登记）| observed（反馈中首次观察到）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "PolicyVersion":
        return PolicyVersion(
            version=data["version"],
            note=data.get("note", ""),
            regions=tuple(data.get("regions", ())),
            registered_at=data["registered_at"],
            origin=data.get("origin", "registered"),
        )


@dataclass(frozen=True, slots=True)
class Event:
    """追加式审计事件，构成事项的历史决定链。"""

    seq: int
    ts: str
    type: str
    issue_id: str | None
    actor: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Event":
        return Event(
            seq=data["seq"],
            ts=data["ts"],
            type=data["type"],
            issue_id=data.get("issue_id"),
            actor=data.get("actor", "system"),
            data=dict(data.get("data", {})),
        )
