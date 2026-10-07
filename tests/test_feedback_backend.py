"""回访归因后端的领域规则测试。"""
import io
import json
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from task_domain_006.cli import main as cli_main
from task_domain_006.api import make_server
from task_domain_006.service import DomainError, FeedbackService

T0 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store_path = str(Path(self.tmp.name) / "store.json")
        self.now = T0
        self.service = FeedbackService(self.store_path, clock=lambda: self.now)

    def submit(self, text, **kwargs):
        defaults = dict(
            scenario="移动端", policy_version="P-2026.1", regions=["杭州"],
            reporter="citizen-01",
        )
        defaults.update(kwargs)
        return self.service.submit_feedback(text=text, **defaults)


class IntakeAndMergeTests(ServiceCase):
    def test_first_feedback_opens_issue(self):
        fb = self.submit("公交扫码经常失败")
        self.assertEqual(fb.state, "merged")
        issue = self.service.issue_view(fb.merged_into)
        self.assertEqual(issue["status"], "open")
        self.assertEqual(issue["policy_version"], "P-2026.1")
        self.assertEqual(issue["feedback_count"], 1)

    def test_similar_feedback_auto_merges(self):
        fb1 = self.submit("公交扫码经常失败")
        fb2 = self.submit("公交扫码失败问题严重", reporter="citizen-02")
        self.assertEqual(fb1.merged_into, fb2.merged_into)
        issue = self.service.issue_view(fb1.merged_into)
        self.assertEqual(issue["feedback_count"], 2)

    def test_different_scenario_or_region_opens_new_issue(self):
        fb1 = self.submit("公交扫码经常失败")
        fb2 = self.submit("公交扫码经常失败", scenario="办事大厅")
        fb3 = self.submit("公交扫码经常失败", regions=["宁波"])
        self.assertNotEqual(fb1.merged_into, fb2.merged_into)
        self.assertNotEqual(fb1.merged_into, fb3.merged_into)

    def test_manual_merge_moves_feedback_and_absorbs_empty_shell(self):
        fb1 = self.submit("公交扫码经常失败")
        fb2 = self.submit("地铁闸机无法识别乘车码", reporter="citizen-02")
        source_id = fb2.merged_into
        self.assertNotEqual(fb1.merged_into, source_id)
        issue = self.service.merge_feedback(
            fb2.id, fb1.merged_into, actor="reviewer", reason="同属扫码通行问题"
        )
        self.assertEqual(set(issue.feedback_ids), {fb1.id, fb2.id})
        # 空壳事项被吸收
        self.assertNotIn(source_id, self.service.state.issues)
        moved = self.service.state.feedback[fb2.id]
        self.assertEqual(moved.merged_into, fb1.merged_into)

    def test_merge_preserves_original_submitter_and_time(self):
        submitted_at = datetime(2026, 9, 20, 8, 30, tzinfo=timezone.utc)
        fb1 = self.submit("公交扫码经常失败")
        fb2 = self.submit("地铁闸机无法识别乘车码", reporter="citizen-王五",
                          submitted_at=submitted_at)
        self.service.merge_feedback(fb2.id, fb1.merged_into, actor="reviewer")
        view = self.service.issue_view(fb1.merged_into)
        members = {m["feedback_id"]: m for m in view["members"]}
        self.assertEqual(members[fb2.id]["submitter"], "citizen-王五")
        self.assertEqual(members[fb2.id]["submitted_at"], "2026-09-20T08:30:00+00:00")
        # 归并事件留痕
        types = [e.type for e in self.service.state.events]
        self.assertIn("feedback_merged", types)

    def test_suggest_merges_ranks_candidates(self):
        fb1 = self.submit("公交扫码经常失败")
        # 相似度低于自动阈值：进入独立事项，等待人工归并
        fb2 = self.submit("扫码失败导致上班迟到", reporter="citizen-02")
        self.assertNotEqual(fb1.merged_into, fb2.merged_into)
        candidates = self.service.suggest_merges(fb2.id)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0]["issue_id"], fb1.merged_into)


class AnonymityTests(ServiceCase):
    def test_anonymous_identity_never_persisted(self):
        raw_identity = "citizen-张三-330106"
        fb = self.submit("大厅排队系统卡顿", reporter=raw_identity, anonymous=True)
        self.assertTrue(fb.source_token.startswith("anon:"))
        self.assertEqual(fb.source_kind, "anonymous")
        on_disk = Path(self.store_path).read_text(encoding="utf-8")
        self.assertNotIn(raw_identity, on_disk)
        self.assertNotIn("张三", on_disk)

    def test_same_anonymous_reporter_gets_stable_pseudonym(self):
        fb1 = self.submit("问题一：公交扫码失败", reporter="citizen-x", anonymous=True)
        fb2 = self.submit("问题二：大厅网络中断", reporter="citizen-x", anonymous=True)
        self.assertEqual(fb1.source_token, fb2.source_token)

    def test_fully_anonymous_without_identity(self):
        fb = self.submit("电话回访无人接听", anonymous=True, reporter=None)
        self.assertTrue(fb.source_token.startswith("anon:"))

    def test_named_reporter_is_stored(self):
        fb = self.submit("自助终端无法打印凭证", reporter="citizen-01")
        self.assertEqual(fb.source_token, "citizen-01")


class PolicyVersionTests(ServiceCase):
    def _concluded_issue(self):
        fb = self.submit("公交扫码经常失败")
        return self.service.conclude(
            fb.merged_into, decision="confirmed", rationale="复现确认",
            supports=[fb.id], counterexamples=[], actor="auditor",
        )

    def test_policy_update_applies_to_open_issue(self):
        fb = self.submit("公交扫码经常失败")
        self.service.register_policy("P-2026.2", note="十月修订")
        issue = self.service.apply_policy(fb.merged_into, "P-2026.2", actor="pm")
        self.assertEqual(issue.policy_version, "P-2026.2")

    def test_policy_update_rejected_after_conclusion(self):
        issue = self._concluded_issue()
        self.service.register_policy("P-2026.2", note="十月修订")
        with self.assertRaises(DomainError) as ctx:
            self.service.apply_policy(issue.id, "P-2026.2", actor="pm")
        self.assertIn("未结论", str(ctx.exception))
        # 结论冻结了作出时的政策版本
        self.assertEqual(issue.conclusion.policy_version_at_decision, "P-2026.1")
        self.assertEqual(self.service.state.issues[issue.id].policy_version, "P-2026.1")

    def test_apply_unregistered_policy_rejected(self):
        fb = self.submit("公交扫码经常失败")
        with self.assertRaises(DomainError):
            self.service.apply_policy(fb.merged_into, "P-9999", actor="pm")

    def test_concluded_issue_is_frozen_for_other_mutations(self):
        issue = self._concluded_issue()
        with self.assertRaises(DomainError):
            self.service.assign(issue.id, owner="张", team="出行组", actor="pm")
        with self.assertRaises(DomainError):
            self.service.add_evidence(issue.id, kind="log", ref="ticket-1", actor="pm")


class CommitmentEscalationTests(ServiceCase):
    def _committed_issue(self, deadline):
        fb = self.submit("公交扫码经常失败")
        self.service.assign(fb.merged_into, owner="李工", team="出行组", actor="pm")
        self.service.commit(
            fb.merged_into, deadline=deadline, note="两周内修复", actor="pm"
        )
        return fb.merged_into

    def test_overdue_escalation_ladder(self):
        issue_id = self._committed_issue("2026-10-02T09:00:00+00:00")
        self.now = T0 + timedelta(hours=26)  # 逾期 1 小时 → L1
        events = self.service.sweep()
        self.assertEqual(self.service.state.issues[issue_id].escalation_level, 1)
        self.assertEqual(events[-1].data["label"], "L1-逾期提醒")
        self.now = T0 + timedelta(hours=50)  # 逾期 25 小时 → L2
        self.service.sweep()
        self.assertEqual(self.service.state.issues[issue_id].escalation_level, 2)
        # 幂等：同水平不再产生事件
        self.assertEqual(self.service.sweep(), [])

    def test_escalation_continues_after_restart(self):
        issue_id = self._committed_issue("2026-10-02T09:00:00+00:00")
        self.now = T0 + timedelta(hours=50)  # 逾期 26 小时 → L2
        self.service.sweep()
        level_before = self.service.state.issues[issue_id].escalation_level
        self.assertEqual(level_before, 2)
        # 模拟服务重启：同一存储、更晚的时钟，构造时自动补扫
        self.now = T0 + timedelta(hours=100)  # 逾期 75 小时 → L3
        restarted = FeedbackService(self.store_path, clock=lambda: self.now)
        self.assertEqual(restarted.state.issues[issue_id].escalation_level, 3)
        labels = [
            e.data["label"] for e in restarted.state.events
            if e.type == "escalated" and e.issue_id == issue_id
        ]
        self.assertEqual(labels, ["L1-逾期提醒", "L2-团队升级", "L3-跨城督办"])

    def test_concluded_issue_not_escalated(self):
        issue_id = self._committed_issue("2026-10-02T09:00:00+00:00")
        issue = self.service.state.issues[issue_id]
        self.service.conclude(
            issue_id, decision="fixed", rationale="已修复",
            supports=[issue.feedback_ids[0]], counterexamples=[], actor="auditor",
        )
        self.now = T0 + timedelta(hours=200)
        self.service.sweep()
        self.assertEqual(self.service.state.issues[issue_id].escalation_level, 0)


class ConclusionExplanationTests(ServiceCase):
    def test_explain_lists_supports_and_counterexamples_with_attribution(self):
        fb1 = self.submit("公交扫码经常失败")
        fb2 = self.submit("公交扫码失败问题严重", reporter="citizen-02")
        fb3 = self.submit("在杭州没有遇到这个问题，扫码很顺畅",
                          reporter="citizen-03", stance="counterexample", regions=["杭州"])
        issue_id = fb1.merged_into
        self.assertEqual(fb2.merged_into, issue_id)
        self.service.merge_feedback(fb3.id, issue_id, actor="reviewer")

        self.service.conclude(
            issue_id, decision="confirmed",
            rationale="两名用户复现扫码失败；一例反例来自不同线路，判定为部分车队问题",
            supports=[fb1.id, fb2.id], counterexamples=[fb3.id], actor="auditor",
        )
        explanation = self.service.explain(issue_id)
        self.assertEqual(explanation["conclusion"]["decision"], "confirmed")
        self.assertEqual(
            {f["submitter"] for f in explanation["supporting_feedback"]},
            {"citizen-01", "citizen-02"},
        )
        counters = explanation["counterexamples"]
        self.assertEqual(len(counters), 1)
        self.assertEqual(counters[0]["submitter"], "citizen-03")
        self.assertEqual(counters[0]["stance"], "counterexample")
        self.assertTrue(counters[0]["submitted_at"])
        # 历史事件链完整
        types = [e["type"] for e in explanation["history"]]
        self.assertEqual(types[0], "issue_created")
        self.assertIn("feedback_merged", types)
        self.assertEqual(types[-1], "concluded")

    def test_conclude_validates_feedback_references(self):
        fb1 = self.submit("公交扫码经常失败")
        fb2 = self.submit("大厅网络中断", scenario="办事大厅")
        with self.assertRaises(DomainError):
            self.service.conclude(
                fb1.merged_into, decision="x", rationale="y",
                supports=[fb2.id], counterexamples=[], actor="auditor",
            )
        with self.assertRaises(DomainError):
            self.service.conclude(
                fb1.merged_into, decision="x", rationale="y",
                supports=[fb1.id], counterexamples=[fb1.id], actor="auditor",
            )
        with self.assertRaises(DomainError):
            self.service.conclude(
                fb1.merged_into, decision="x", rationale="y",
                supports=[], counterexamples=[], actor="auditor",
            )


class RegionReportTests(ServiceCase):
    def test_region_report_shows_current_status_and_history(self):
        fb1 = self.submit("公交扫码经常失败", regions=["杭州"])
        fb2 = self.submit("图书馆预约页面打不开", regions=["杭州"], scenario="移动端")
        fb3 = self.submit("宁波地铁乘车码正常", regions=["宁波"], stance="counterexample")
        self.service.conclude(
            fb1.merged_into, decision="confirmed", rationale="复现确认",
            supports=[fb1.id], counterexamples=[], actor="auditor",
        )
        report = self.service.region_report("杭州")
        self.assertEqual(report["region"], "杭州")
        self.assertEqual(len(report["current_issues"]), 1)
        self.assertEqual(report["current_issues"][0]["issue_id"], fb2.merged_into)
        self.assertEqual(len(report["concluded_issues"]), 1)
        self.assertEqual(report["concluded_issues"][0]["decision"], "confirmed")
        self.assertEqual(
            report["concluded_issues"][0]["policy_version_at_decision"], "P-2026.1"
        )
        self.assertEqual(len(report["historical_decisions"]), 1)
        # 宁波的反例不进入杭州报告
        self.assertNotIn(fb3.merged_into, json.dumps(report))


class PersistenceTests(ServiceCase):
    def test_state_survives_reload(self):
        fb = self.submit("公交扫码经常失败")
        self.service.add_evidence(
            fb.merged_into, kind="log", ref="ticket-42", note="网关超时", actor="李工"
        )
        reloaded = FeedbackService(self.store_path, clock=lambda: self.now)
        view = reloaded.issue_view(fb.merged_into)
        self.assertEqual(view["feedback_count"], 1)
        self.assertEqual(view["evidence"][0]["ref"], "ticket-42")
        self.assertEqual(view["members"][0]["submitter"], "citizen-01")


class CliTests(ServiceCase):
    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(["--store", self.store_path, *argv])
        self.assertEqual(code, 0)
        return json.loads(buf.getvalue())

    def test_cli_submit_and_region_report(self):
        out = self.run_cli(
            "submit", "--text", "公交扫码经常失败", "--scenario", "移动端",
            "--policy-version", "P-2026.1", "--region", "杭州",
            "--reporter", "citizen-01",
        )
        issue_id = out["issue_id"]
        report = self.run_cli("region", "杭州")
        self.assertEqual(report["current_issues"][0]["issue_id"], issue_id)

    def test_cli_full_workflow(self):
        out = self.run_cli(
            "submit", "--text", "公交扫码经常失败", "--scenario", "移动端",
            "--policy-version", "P-2026.1", "--region", "杭州", "--reporter", "c1",
        )
        issue_id = out["issue_id"]
        fb_id = out["feedback"]["id"]
        self.run_cli("assign", issue_id, "--owner", "李工", "--team", "出行组")
        self.run_cli("commit", issue_id, "--deadline", "2026-10-20T18:00:00+08:00")
        self.run_cli("evidence", issue_id, "--kind", "log", "--ref", "ticket-7")
        self.run_cli("conclude", issue_id, "--decision", "confirmed",
                     "--rationale", "复现确认", "--support", fb_id)
        explained = self.run_cli("explain", issue_id)
        self.assertEqual(explained["conclusion"]["decision"], "confirmed")
        self.assertEqual(explained["supporting_feedback"][0]["submitter"], "c1")


class ApiTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.service, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def call(self, method, path, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_api_submit_and_region_report(self):
        status, body = self.call("POST", "/feedback", {
            "text": "公交扫码经常失败", "scenario": "移动端",
            "policy_version": "P-2026.1", "regions": ["杭州"],
            "reporter": "citizen-01",
        })
        self.assertEqual(status, 200)
        issue_id = body["issue_id"]
        status, report = self.call("GET", "/regions/%E6%9D%AD%E5%B7%9E/report")
        self.assertEqual(status, 200)
        self.assertEqual(report["current_issues"][0]["issue_id"], issue_id)

    def test_api_policy_gate_returns_409(self):
        status, body = self.call("POST", "/feedback", {
            "text": "公交扫码经常失败", "scenario": "移动端",
            "policy_version": "P-2026.1", "regions": ["杭州"], "reporter": "c1",
        })
        issue_id = body["issue_id"]
        fb_id = body["feedback"]["id"]
        self.call("POST", f"/issues/{issue_id}/conclude", {
            "decision": "confirmed", "rationale": "复现", "supports": [fb_id],
        })
        self.call("POST", "/policies", {"version": "P-2026.2"})
        status, body = self.call("POST", f"/issues/{issue_id}/policy",
                                 {"version": "P-2026.2"})
        self.assertEqual(status, 409)
        self.assertIn("未结论", body["error"])

    def test_api_unknown_route_404(self):
        status, _ = self.call("GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
