"""命令行入口：回访归因后端的全部操作均可在此完成。

用法示例：
    python3 run_cli.py --store data/store.json submit --text "公交扫码经常失败" \
        --scenario 移动端 --policy-version P-2026.1 --region 杭州 --reporter citizen-01
    python3 run_cli.py --store data/store.json region 杭州
    python3 run_cli.py --store data/store.json serve --port 8080
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Sequence

from .service import DomainError, FeedbackService

DEFAULT_STORE = os.environ.get("FEEDBACK_STORE", "data/store.json")


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_cli.py", description="公共服务体验回访归因后端")
    parser.add_argument("--store", default=DEFAULT_STORE, help="存储文件路径（默认 %(default)s）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("submit", help="提交一条回访反馈")
    p.add_argument("--text", required=True)
    p.add_argument("--scenario", required=True, help="场景，如 移动端/办事大厅/电话回访")
    p.add_argument("--policy-version", required=True)
    p.add_argument("--region", action="append", required=True, help="影响地区，可多次指定")
    p.add_argument("--impact", default="community",
                   choices=["individual", "community", "city", "multi_city"])
    p.add_argument("--stance", default="problem",
                   choices=["problem", "counterexample", "general"])
    p.add_argument("--reporter", help="提交者标识（与 --anonymous 同用时仅保留不可逆伪名）")
    p.add_argument("--anonymous", action="store_true", help="匿名提交，原始身份不落盘")

    p = sub.add_parser("issues", help="列出事项")
    p.add_argument("--region")
    p.add_argument("--status", choices=["open", "in_progress", "concluded"])

    p = sub.add_parser("issue", help="查看事项详情（含原始提交者与时间）")
    p.add_argument("issue_id")

    p = sub.add_parser("suggest", help="为反馈推荐归并目标")
    p.add_argument("feedback_id")

    p = sub.add_parser("merge", help="将反馈归并到事项")
    p.add_argument("feedback_id")
    p.add_argument("issue_id")
    p.add_argument("--actor", default="operator")
    p.add_argument("--reason", default="")

    p = sub.add_parser("evidence", help="为事项补充证据")
    p.add_argument("issue_id")
    p.add_argument("--kind", default="link")
    p.add_argument("--ref", required=True)
    p.add_argument("--note", default="")
    p.add_argument("--actor", default="operator")

    p = sub.add_parser("assign", help="责任分派")
    p.add_argument("issue_id")
    p.add_argument("--owner", required=True)
    p.add_argument("--team", required=True)
    p.add_argument("--actor", default="operator")

    p = sub.add_parser("commit", help="作出有期限的处理承诺")
    p.add_argument("issue_id")
    p.add_argument("--deadline", required=True, help="ISO-8601，如 2026-10-20T18:00:00+08:00")
    p.add_argument("--note", default="")
    p.add_argument("--actor", default="operator")

    p = sub.add_parser("conclude", help="对事项作出改进结论")
    p.add_argument("issue_id")
    p.add_argument("--decision", required=True)
    p.add_argument("--rationale", required=True)
    p.add_argument("--support", action="append", default=[], help="支撑反馈 id，可多次")
    p.add_argument("--counterexample", action="append", default=[], help="反例反馈 id，可多次")
    p.add_argument("--actor", default="operator")

    p = sub.add_parser("explain", help="审核解释：结论依据了哪些反馈与反例")
    p.add_argument("issue_id")

    p = sub.add_parser("policy-register", help="登记政策版本")
    p.add_argument("version")
    p.add_argument("--note", default="")
    p.add_argument("--region", action="append", default=[])
    p.add_argument("--actor", default="operator")

    p = sub.add_parser("policy-apply", help="将政策版本应用到未结论事项")
    p.add_argument("issue_id")
    p.add_argument("version")
    p.add_argument("--actor", default="operator")

    sub.add_parser("sweep", help="推进逾期升级（幂等）")

    p = sub.add_parser("region", help="输出某地区问题的当前状态与历史决定")
    p.add_argument("region")

    p = sub.add_parser("serve", help="启动 HTTP 接口")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        from .api import serve

        serve(args.store, host=args.host, port=args.port)
        return 0

    service = FeedbackService(args.store)
    try:
        if args.command == "submit":
            fb = service.submit_feedback(
                text=args.text,
                scenario=args.scenario,
                policy_version=args.policy_version,
                regions=args.region,
                impact_level=args.impact,
                stance=args.stance,
                reporter=args.reporter,
                anonymous=args.anonymous,
            )
            _print({"feedback": fb.to_dict(), "issue_id": fb.merged_into})
        elif args.command == "issues":
            _print({"issues": service.list_issues(region=args.region, status=args.status)})
        elif args.command == "issue":
            _print(service.issue_view(args.issue_id))
        elif args.command == "suggest":
            _print({"candidates": service.suggest_merges(args.feedback_id)})
        elif args.command == "merge":
            _print(service.merge_feedback(
                args.feedback_id, args.issue_id, actor=args.actor, reason=args.reason
            ).to_dict())
        elif args.command == "evidence":
            _print(service.add_evidence(
                args.issue_id, kind=args.kind, ref=args.ref, note=args.note, actor=args.actor
            ).to_dict())
        elif args.command == "assign":
            _print(service.assign(
                args.issue_id, owner=args.owner, team=args.team, actor=args.actor
            ).to_dict())
        elif args.command == "commit":
            _print(service.commit(
                args.issue_id, deadline=args.deadline, note=args.note, actor=args.actor
            ).to_dict())
        elif args.command == "conclude":
            _print(service.conclude(
                args.issue_id,
                decision=args.decision,
                rationale=args.rationale,
                supports=args.support,
                counterexamples=args.counterexample,
                actor=args.actor,
            ).to_dict())
        elif args.command == "explain":
            _print(service.explain(args.issue_id))
        elif args.command == "policy-register":
            _print(service.register_policy(
                args.version, note=args.note, regions=args.region, actor=args.actor
            ).to_dict())
        elif args.command == "policy-apply":
            _print(service.apply_policy(args.issue_id, args.version, actor=args.actor).to_dict())
        elif args.command == "sweep":
            # CLI 每次调用都是一次“重启”：启动补扫 + 显式扫描一并报告
            events = service.startup_escalations + service.sweep()
            _print({"escalations": [e.to_dict() for e in events]})
        elif args.command == "region":
            _print(service.region_report(args.region))
        else:  # pragma: no cover - argparse 已保证
            raise DomainError(f"未知命令: {args.command}")
    except DomainError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    return 0
