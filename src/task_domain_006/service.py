"""回访归因服务：全部用例与领域规则。

规则要点：
- 相似意见归并：同场景、地区有交集且文本相似度达阈值时自动并入既有未结论事项；
  审核人员也可显式归并。合并不删除反馈，原始提交者与提交时间永久保留。
- 匿名来源：仅持久化 HMAC 伪名（anon:...），原始身份从不落盘，无法反向识别。
- 政策版本：只能应用到未结论事项；结论会冻结作出时的政策版本。
- 逾期升级：按确定性阶梯（L1/L2/L3）幂等推进；服务构造（含重启）时自动补扫。
"""
from __future__ import annotations

import hmac
import secrets
import uuid
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from typing import Any, Callable, Iterable

from .feedback_models import (
    FEEDBACK_MERGED,
    IMPACT_LEVELS,
    ISSUE_CONCLUDED,
    ISSUE_IN_PROGRESS,
    ISSUE_OPEN,
    SOURCE_ANONYMOUS,
    SOURCE_NAMED,
    STANCES,
    Assignment,
    Commitment,
    Conclusion,
    Event,
    Evidence,
    Feedback,
    Issue,
    PolicyVersion,
    parse_iso,
    to_iso,
    utcnow,
)
from .similarity import text_similarity
from .store import State, Store

# (级别, 逾期小时数阈值, 标签) —— 确定性升级阶梯
ESCALATION_STEPS: tuple[tuple[int, float, str], ...] = (
    (1, 0.0, "L1-逾期提醒"),
    (2, 24.0, "L2-团队升级"),
    (3, 72.0, "L3-跨城督办"),
)

AUTO_MERGE_THRESHOLD = 0.4


class DomainError(Exception):
    """领域规则被违反时抛出（如对已结论事项更新政策版本）。"""


class NotFoundError(DomainError):
    """引用的对象不存在。"""


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class FeedbackService:
    def __init__(
        self,
        store_path: str,
        clock: Callable[[], datetime] = utcnow,
        auto_merge_threshold: float = AUTO_MERGE_THRESHOLD,
    ):
        self.store = Store(store_path)
        self.state: State = self.store.load()
        self.clock = clock
        self.auto_merge_threshold = auto_merge_threshold
        # 服务启动（含重启）即补扫逾期升级，保证停机期间产生的逾期继续推进
        self.startup_escalations: list[Event] = self.sweep()

    # ------------------------------------------------------------------ 内部

    def _now_iso(self) -> str:
        return to_iso(self.clock())

    def _save(self) -> None:
        self.store.save(self.state)

    def _event(self, type_: str, issue_id: str | None, actor: str, data: dict[str, Any]) -> Event:
        event = Event(
            seq=len(self.state.events) + 1,
            ts=self._now_iso(),
            type=type_,
            issue_id=issue_id,
            actor=actor,
            data=data,
        )
        self.state.events.append(event)
        return event

    def _issue(self, issue_id: str) -> Issue:
        issue = self.state.issues.get(issue_id)
        if issue is None:
            raise NotFoundError(f"事项不存在: {issue_id}")
        return issue

    def _feedback(self, feedback_id: str) -> Feedback:
        fb = self.state.feedback.get(feedback_id)
        if fb is None:
            raise NotFoundError(f"反馈不存在: {feedback_id}")
        return fb

    @staticmethod
    def _require_open(issue: Issue, action: str) -> None:
        if issue.status == ISSUE_CONCLUDED:
            raise DomainError(f"事项 {issue.id} 已结论，不能{action}")

    def _source_token(self, reporter: str | None, anonymous: bool) -> tuple[str, str]:
        if anonymous:
            if reporter:
                # 单向伪名化：原始身份只参与 HMAC 计算，绝不落盘
                digest = hmac.new(
                    self.state.anon_secret.encode("utf-8"),
                    reporter.encode("utf-8"),
                    sha256,
                ).hexdigest()
            else:
                digest = secrets.token_hex(16)  # 完全不提供身份：随机伪名
            return SOURCE_ANONYMOUS, f"anon:{digest}"
        if not reporter:
            raise DomainError("实名反馈必须提供 reporter；匿名反馈请设置 anonymous=True")
        return SOURCE_NAMED, reporter

    def _register_observed_policy(self, version: str) -> None:
        if version not in self.state.policies:
            self.state.policies[version] = PolicyVersion(
                version=version,
                note="反馈中首次观察到",
                regions=(),
                registered_at=self._now_iso(),
                origin="observed",
            )

    # ------------------------------------------------------------------ 接收与归并

    def submit_feedback(
        self,
        *,
        text: str,
        scenario: str,
        policy_version: str,
        regions: Iterable[str],
        impact_level: str = "community",
        stance: str = "problem",
        reporter: str | None = None,
        anonymous: bool = False,
        submitted_at: datetime | None = None,
    ) -> Feedback:
        text = (text or "").strip()
        if not text:
            raise DomainError("反馈内容不能为空")
        scenario = (scenario or "").strip()
        if not scenario:
            raise DomainError("场景不能为空")
        policy_version = (policy_version or "").strip()
        if not policy_version:
            raise DomainError("政策版本不能为空")
        region_tuple = tuple(dict.fromkeys(r.strip() for r in regions if r and r.strip()))
        if not region_tuple:
            raise DomainError("影响范围至少需要一个地区")
        if impact_level not in IMPACT_LEVELS:
            raise DomainError(f"影响范围等级无效: {impact_level}，可选 {IMPACT_LEVELS}")
        if stance not in STANCES:
            raise DomainError(f"立场无效: {stance}，可选 {STANCES}")

        source_kind, token = self._source_token(reporter, anonymous)
        fb = Feedback(
            id=_new_id("fb"),
            source_kind=source_kind,
            source_token=token,
            scenario=scenario,
            policy_version=policy_version,
            regions=region_tuple,
            impact_level=impact_level,
            stance=stance,
            text=text,
            submitted_at=to_iso(submitted_at) if submitted_at else self._now_iso(),
        )
        self.state.feedback[fb.id] = fb
        self._register_observed_policy(policy_version)

        match = self._best_match(fb)
        if match is not None:
            issue, score = match
            self._merge_into(fb, issue, actor=token, reason=f"自动归并（相似度 {score:.2f}）", score=score)
        else:
            self._open_issue_for(fb, actor=token)
        self._save()
        return self.state.feedback[fb.id]

    def _open_issue_for(self, fb: Feedback, actor: str) -> Issue:
        title = fb.text.replace("\n", " ").strip()
        title = title[:24] + ("…" if len(title) > 24 else "")
        issue = Issue(
            id=_new_id("iss"),
            title=title or "未命名事项",
            scenario=fb.scenario,
            regions=fb.regions,
            policy_version=fb.policy_version,
            status=ISSUE_OPEN,
            feedback_ids=(fb.id,),
            created_at=self._now_iso(),
            updated_at=self._now_iso(),
        )
        self.state.issues[issue.id] = issue
        merged = replace(fb, state=FEEDBACK_MERGED, merged_into=issue.id, merged_at=self._now_iso())
        self.state.feedback[fb.id] = merged
        self._event("issue_created", issue.id, actor, {"title": issue.title, "feedback_id": fb.id})
        return issue

    def _candidate_issues(self, fb: Feedback) -> Iterable[Issue]:
        for issue in self.state.issues.values():
            if issue.status == ISSUE_CONCLUDED:
                continue
            if issue.id == fb.merged_into:
                continue
            if issue.scenario != fb.scenario:
                continue
            if not set(issue.regions) & set(fb.regions):
                continue
            yield issue

    def _similarity_to_issue(self, fb: Feedback, issue: Issue) -> float:
        texts = [issue.title]
        texts += [
            self.state.feedback[fid].text
            for fid in issue.feedback_ids
            if fid in self.state.feedback
        ]
        return max((text_similarity(fb.text, t) for t in texts), default=0.0)

    def _best_match(self, fb: Feedback) -> tuple[Issue, float] | None:
        best: tuple[Issue, float] | None = None
        for issue in self._candidate_issues(fb):
            score = self._similarity_to_issue(fb, issue)
            if score >= self.auto_merge_threshold and (best is None or score > best[1]):
                best = (issue, score)
        return best

    def suggest_merges(self, feedback_id: str, limit: int = 5) -> list[dict[str, Any]]:
        """为审核人员列出候选归并目标（含低于自动阈值的）。"""
        fb = self._feedback(feedback_id)
        scored = [
            (self._similarity_to_issue(fb, issue), issue)
            for issue in self._candidate_issues(fb)
        ]
        scored.sort(key=lambda item: (-item[0], item[1].created_at, item[1].id))
        return [
            {"issue_id": issue.id, "title": issue.title, "score": round(score, 4)}
            for score, issue in scored[:limit]
            if score > 0
        ]

    def merge_feedback(
        self, feedback_id: str, issue_id: str, *, actor: str, reason: str = ""
    ) -> Issue:
        """显式归并（移动语义）：反馈从当前事项移入目标事项。

        反馈本体（提交者、提交时间、原文）永不改写；若来源事项被抽空且
        尚无处理进展，则吸收该空壳事项（历史仍保留在事件流中）。
        """
        fb = self._feedback(feedback_id)
        target = self._issue(issue_id)
        self._require_open(target, "并入新反馈")
        if fb.merged_into == target.id:
            raise DomainError(f"反馈 {fb.id} 已属于事项 {target.id}")
        source = self.state.issues.get(fb.merged_into) if fb.merged_into else None
        if source is not None and source.status == ISSUE_CONCLUDED:
            raise DomainError(f"反馈 {fb.id} 所在事项 {source.id} 已结论，不能移出")

        score = self._similarity_to_issue(fb, target)
        if source is not None:
            remaining = tuple(fid for fid in source.feedback_ids if fid != fb.id)
            if remaining:
                self.state.issues[source.id] = replace(
                    source, feedback_ids=remaining, updated_at=self._now_iso()
                )
            else:
                if source.evidence or source.assignment or source.commitment or source.conclusion:
                    raise DomainError(
                        f"来源事项 {source.id} 已有处理进展，不能抽空；请先清理其证据/分派/承诺"
                    )
                del self.state.issues[source.id]
                self._event(
                    "issue_absorbed", target.id, actor,
                    {"absorbed_issue": source.id, "feedback_id": fb.id},
                )
        updated = self._merge_into(fb, target, actor=actor, reason=reason or "人工归并", score=score)
        self._save()
        return updated

    def _merge_into(
        self, fb: Feedback, issue: Issue, *, actor: str, reason: str, score: float
    ) -> Issue:
        now = self._now_iso()
        self.state.feedback[fb.id] = replace(
            fb, state=FEEDBACK_MERGED, merged_into=issue.id, merged_at=now
        )
        regions = tuple(dict.fromkeys(issue.regions + fb.regions))
        updated = replace(
            issue,
            feedback_ids=issue.feedback_ids + (fb.id,),
            regions=regions,
            updated_at=now,
        )
        self.state.issues[issue.id] = updated
        self._event(
            "feedback_merged",
            issue.id,
            actor,
            {"feedback_id": fb.id, "reason": reason, "score": round(score, 4)},
        )
        return updated

    # ------------------------------------------------------------------ 证据 / 分派 / 承诺

    def add_evidence(
        self, issue_id: str, *, kind: str, ref: str, note: str = "", actor: str
    ) -> Evidence:
        issue = self._issue(issue_id)
        self._require_open(issue, "补充证据")
        if not ref:
            raise DomainError("证据引用不能为空")
        evidence = Evidence(
            id=_new_id("ev"),
            issue_id=issue.id,
            kind=kind or "link",
            ref=ref,
            note=note,
            added_by=actor,
            added_at=self._now_iso(),
        )
        self.state.issues[issue.id] = replace(
            issue, evidence=issue.evidence + (evidence,), updated_at=self._now_iso()
        )
        self._event("evidence_added", issue.id, actor, {"evidence_id": evidence.id, "ref": ref})
        self._save()
        return evidence

    def assign(self, issue_id: str, *, owner: str, team: str, actor: str) -> Issue:
        issue = self._issue(issue_id)
        self._require_open(issue, "分派")
        if not owner or not team:
            raise DomainError("责任分派需要 owner 与 team")
        assignment = Assignment(
            owner=owner, team=team, assigned_by=actor, assigned_at=self._now_iso()
        )
        updated = replace(
            issue,
            assignment=assignment,
            status=ISSUE_IN_PROGRESS,
            updated_at=self._now_iso(),
        )
        self.state.issues[issue.id] = updated
        self._event("assigned", issue.id, actor, {"owner": owner, "team": team})
        self._save()
        return updated

    def commit(self, issue_id: str, *, deadline: str, note: str, actor: str) -> Issue:
        """作出有期限的处理承诺；重新承诺会清零当前升级级别（历史保留在事件流）。"""
        issue = self._issue(issue_id)
        self._require_open(issue, "作出承诺")
        try:
            parsed = parse_iso(deadline)
        except ValueError as exc:
            raise DomainError(f"承诺期限不是合法时间: {deadline}") from exc
        commitment = Commitment(
            deadline=to_iso(parsed), note=note, made_by=actor, made_at=self._now_iso()
        )
        updated = replace(
            issue,
            commitment=commitment,
            status=ISSUE_IN_PROGRESS,
            escalation_level=0,
            updated_at=self._now_iso(),
        )
        self.state.issues[issue.id] = updated
        self._event(
            "commitment_made", issue.id, actor, {"deadline": commitment.deadline, "note": note}
        )
        self._save()
        return updated

    # ------------------------------------------------------------------ 政策版本

    def register_policy(
        self, version: str, *, note: str = "", regions: Iterable[str] = (), actor: str = "system"
    ) -> PolicyVersion:
        version = (version or "").strip()
        if not version:
            raise DomainError("政策版本号不能为空")
        existing = self.state.policies.get(version)
        if existing is not None:
            raise DomainError(f"政策版本已存在: {version}")
        policy = PolicyVersion(
            version=version,
            note=note,
            regions=tuple(regions),
            registered_at=self._now_iso(),
            origin="registered",
        )
        self.state.policies[version] = policy
        self._event("policy_registered", None, actor, {"version": version})
        self._save()
        return policy

    def apply_policy(self, issue_id: str, version: str, *, actor: str) -> Issue:
        """政策版本更新只影响未结论事项；已结论事项的政策版本在结论中冻结。"""
        issue = self._issue(issue_id)
        if issue.status == ISSUE_CONCLUDED:
            raise DomainError(
                f"政策版本更新只能影响未结论事项：{issue.id} 已于 "
                f"{issue.conclusion.decided_at if issue.conclusion else '?'} 结论"
            )
        if version not in self.state.policies:
            raise DomainError(f"政策版本未登记: {version}")
        updated = replace(issue, policy_version=version, updated_at=self._now_iso())
        self.state.issues[issue.id] = updated
        self._event(
            "policy_updated", issue.id, actor, {"from": issue.policy_version, "to": version}
        )
        self._save()
        return updated

    # ------------------------------------------------------------------ 结论与解释

    def conclude(
        self,
        issue_id: str,
        *,
        decision: str,
        rationale: str,
        supports: Iterable[str],
        counterexamples: Iterable[str],
        actor: str,
    ) -> Issue:
        issue = self._issue(issue_id)
        self._require_open(issue, "再次结论")
        if not decision.strip():
            raise DomainError("结论决定不能为空")
        if not rationale.strip():
            raise DomainError("结论理由不能为空")
        support_ids = tuple(dict.fromkeys(supports))
        counter_ids = tuple(dict.fromkeys(counterexamples))
        if not support_ids and not counter_ids:
            raise DomainError("结论必须至少引用一条支撑反馈或反例")
        overlap = set(support_ids) & set(counter_ids)
        if overlap:
            raise DomainError(f"同一反馈不能既是支撑又是反例: {sorted(overlap)}")
        members = set(issue.feedback_ids)
        unknown = [fid for fid in support_ids + counter_ids if fid not in members]
        if unknown:
            raise DomainError(f"结论引用了不属于该事项的反馈: {unknown}")
        conclusion = Conclusion(
            decision=decision.strip(),
            rationale=rationale.strip(),
            supports=support_ids,
            counterexamples=counter_ids,
            decided_by=actor,
            decided_at=self._now_iso(),
            policy_version_at_decision=issue.policy_version,
        )
        updated = replace(
            issue,
            conclusion=conclusion,
            status=ISSUE_CONCLUDED,
            updated_at=self._now_iso(),
        )
        self.state.issues[issue.id] = updated
        self._event(
            "concluded",
            issue.id,
            actor,
            {
                "decision": conclusion.decision,
                "supports": list(support_ids),
                "counterexamples": list(counter_ids),
                "policy_version_at_decision": conclusion.policy_version_at_decision,
            },
        )
        self._save()
        return updated

    def explain(self, issue_id: str) -> dict[str, Any]:
        """审核视角：一条改进结论依据了哪些反馈与反例，以及完整历史。"""
        issue = self._issue(issue_id)

        def fb_view(fid: str) -> dict[str, Any]:
            fb = self.state.feedback[fid]
            return {
                "feedback_id": fb.id,
                "source_kind": fb.source_kind,
                "submitter": fb.source_token,  # 匿名为不可逆伪名
                "submitted_at": fb.submitted_at,
                "stance": fb.stance,
                "scenario": fb.scenario,
                "regions": list(fb.regions),
                "policy_version": fb.policy_version,
                "text": fb.text,
            }

        conclusion = issue.conclusion
        return {
            "issue_id": issue.id,
            "title": issue.title,
            "status": issue.status,
            "policy_version": issue.policy_version,
            "conclusion": conclusion.to_dict() if conclusion else None,
            "supporting_feedback": [fb_view(f) for f in conclusion.supports] if conclusion else [],
            "counterexamples": [fb_view(f) for f in conclusion.counterexamples] if conclusion else [],
            "evidence": [e.to_dict() for e in issue.evidence],
            "history": [e.to_dict() for e in self.state.events if e.issue_id == issue.id],
        }

    # ------------------------------------------------------------------ 逾期升级

    def sweep(self) -> list[Event]:
        """推进逾期升级。幂等：只为新跨过的级别补记事件，同水平重复调用为空。"""
        now = self.clock()
        emitted: list[Event] = []
        for issue in sorted(self.state.issues.values(), key=lambda i: (i.created_at, i.id)):
            if issue.status == ISSUE_CONCLUDED or issue.commitment is None:
                continue
            overdue_hours = (
                now - parse_iso(issue.commitment.deadline)
            ).total_seconds() / 3600.0
            if overdue_hours < 0:
                continue
            for level, threshold, label in ESCALATION_STEPS:
                current = self.state.issues[issue.id].escalation_level
                if overdue_hours >= threshold and level > current:
                    self.state.issues[issue.id] = replace(
                        self.state.issues[issue.id],
                        escalation_level=level,
                        updated_at=to_iso(now),
                    )
                    emitted.append(
                        self._event(
                            "escalated",
                            issue.id,
                            "system",
                            {
                                "level": level,
                                "label": label,
                                "overdue_hours": round(overdue_hours, 2),
                                "deadline": issue.commitment.deadline,
                            },
                        )
                    )
        if emitted:
            self._save()
        return emitted

    # ------------------------------------------------------------------ 查询与报告

    def _issue_summary(self, issue: Issue) -> dict[str, Any]:
        now = self.clock()
        overdue = False
        if issue.commitment is not None and issue.status != ISSUE_CONCLUDED:
            overdue = now > parse_iso(issue.commitment.deadline)
        return {
            "issue_id": issue.id,
            "title": issue.title,
            "scenario": issue.scenario,
            "status": issue.status,
            "policy_version": issue.policy_version,
            "regions": list(issue.regions),
            "feedback_count": len(issue.feedback_ids),
            "owner": issue.assignment.owner if issue.assignment else None,
            "team": issue.assignment.team if issue.assignment else None,
            "deadline": issue.commitment.deadline if issue.commitment else None,
            "overdue": overdue,
            "escalation_level": issue.escalation_level,
            "updated_at": issue.updated_at,
        }

    def issue_view(self, issue_id: str) -> dict[str, Any]:
        issue = self._issue(issue_id)
        view = self._issue_summary(issue)
        view["members"] = [
            {
                "feedback_id": fb.id,
                "source_kind": fb.source_kind,
                "submitter": fb.source_token,
                "submitted_at": fb.submitted_at,
                "stance": fb.stance,
                "text": fb.text,
            }
            for fid in issue.feedback_ids
            if (fb := self.state.feedback.get(fid)) is not None
        ]
        view["evidence"] = [e.to_dict() for e in issue.evidence]
        view["assignment"] = issue.assignment.to_dict() if issue.assignment else None
        view["commitment"] = issue.commitment.to_dict() if issue.commitment else None
        view["conclusion"] = issue.conclusion.to_dict() if issue.conclusion else None
        return view

    def list_issues(
        self, *, region: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        issues = sorted(self.state.issues.values(), key=lambda i: (i.created_at, i.id))
        return [
            self._issue_summary(i)
            for i in issues
            if (region is None or region in i.regions) and (status is None or i.status == status)
        ]

    def region_report(self, region: str) -> dict[str, Any]:
        """某地区问题的当前状态与历史决定。"""
        related = [
            i
            for i in sorted(self.state.issues.values(), key=lambda x: (x.created_at, x.id))
            if region in i.regions
        ]
        related_ids = {i.id for i in related}
        decisions = [
            e.to_dict()
            for e in self.state.events
            if e.type == "concluded" and e.issue_id in related_ids
        ]
        return {
            "region": region,
            "generated_at": self._now_iso(),
            "current_issues": [
                self._issue_summary(i) for i in related if i.status != ISSUE_CONCLUDED
            ],
            "concluded_issues": [
                {
                    **self._issue_summary(i),
                    "decision": i.conclusion.decision if i.conclusion else None,
                    "decided_at": i.conclusion.decided_at if i.conclusion else None,
                    "policy_version_at_decision": (
                        i.conclusion.policy_version_at_decision if i.conclusion else None
                    ),
                }
                for i in related
                if i.status == ISSUE_CONCLUDED
            ],
            "historical_decisions": decisions,
        }
