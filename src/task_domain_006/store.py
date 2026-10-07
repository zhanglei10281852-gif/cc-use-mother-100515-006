"""JSON 文件持久化：原子写入，重启后完整恢复，支撑逾期升级的续推。

匿名化密钥（anon_secret）在首次初始化时生成并仅用于 HMAC 伪名化，
原始身份从不写入磁盘。生产部署中应将该密钥迁移至独立密钥管理。
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from .feedback_models import Event, Feedback, Issue, PolicyVersion

SCHEMA_VERSION = 1


@dataclass
class State:
    anon_secret: str = ""
    feedback: dict[str, Feedback] = field(default_factory=dict)
    issues: dict[str, Issue] = field(default_factory=dict)
    policies: dict[str, PolicyVersion] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)


class Store:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    def load(self) -> State:
        if not self.path.exists():
            return State(anon_secret=secrets.token_hex(32))
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        meta = raw.get("meta", {})
        return State(
            anon_secret=meta.get("anon_secret") or secrets.token_hex(32),
            feedback={fid: Feedback.from_dict(f) for fid, f in raw.get("feedback", {}).items()},
            issues={iid: Issue.from_dict(i) for iid, i in raw.get("issues", {}).items()},
            policies={v: PolicyVersion.from_dict(p) for v, p in raw.get("policies", {}).items()},
            events=[Event.from_dict(e) for e in raw.get("events", [])],
        )

    def save(self, state: State) -> None:
        payload = {
            "meta": {"schema": SCHEMA_VERSION, "anon_secret": state.anon_secret},
            "feedback": {fid: f.to_dict() for fid, f in state.feedback.items()},
            "issues": {iid: i.to_dict() for iid, i in state.issues.items()},
            "policies": {v: p.to_dict() for v, p in state.policies.items()},
            "events": [e.to_dict() for e in state.events],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # 原子替换，避免半截文件
