"""基于标准库 http.server 的 HTTP 接口。

路由与 CLI 一一对应；服务启动时构造 FeedbackService，会自动补扫逾期升级。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qsl, unquote

from .service import DomainError, FeedbackService, NotFoundError

HandlerFn = Callable[[FeedbackService, re.Match[str], dict[str, Any]], Any]

_ROUTES: list[tuple[str, re.Pattern[str], HandlerFn]] = []


def route(method: str, pattern: str) -> Callable[[HandlerFn], HandlerFn]:
    compiled = re.compile(f"^{pattern}$")

    def decorator(fn: HandlerFn) -> HandlerFn:
        _ROUTES.append((method, compiled, fn))
        return fn

    return decorator


@route("GET", r"/health")
def _health(svc: FeedbackService, m: re.Match[str], body: dict[str, Any]) -> Any:
    return {"ok": True}


@route("POST", r"/feedback")
def _submit(svc: FeedbackService, m: re.Match[str], body: dict[str, Any]) -> Any:
    fb = svc.submit_feedback(
        text=body.get("text", ""),
        scenario=body.get("scenario", ""),
        policy_version=body.get("policy_version", ""),
        regions=body.get("regions", []),
        impact_level=body.get("impact_level", "community"),
        stance=body.get("stance", "problem"),
        reporter=body.get("reporter"),
        anonymous=bool(body.get("anonymous", False)),
    )
    return {"feedback": fb.to_dict(), "issue_id": fb.merged_into}


@route("GET", r"/issues")
def _list(svc: FeedbackService, m: re.Match[str], body: dict[str, Any]) -> Any:
    return {"issues": svc.list_issues(region=body.get("region"), status=body.get("status"))}


@route("GET", r"/issues/(?P<issue_id>[^/]+)")
def _issue(svc: FeedbackService, m: re.Match[str], body: dict[str, Any]) -> Any:
    return svc.issue_view(m.group("issue_id"))


@route("GET", r"/issues/(?P<issue_id>[^/]+)/explanation")
def _explain(svc: FeedbackService, m: re.Match[str], body: dict[str, Any]) -> Any:
    return svc.explain(m.group("issue_id"))


@route("POST", r"/issues/(?P<issue_id>[^/]+)/merge")
def _merge(svc: FeedbackService, m: re.Match[str], body: dict[str, Any]) -> Any:
    issue = svc.merge_feedback(
        body.get("feedback_id", ""),
        m.group("issue_id"),
        actor=body.get("actor", "operator"),
        reason=body.get("reason", ""),
    )
    return issue.to_dict()


@route("POST", r"/issues/(?P<issue_id>[^/]+)/evidence")
def _evidence(svc: FeedbackService, m: re.Match[str], body: dict[str, Any]) -> Any:
    return svc.add_evidence(
        m.group("issue_id"),
        kind=body.get("kind", "link"),
        ref=body.get("ref", ""),
        note=body.get("note", ""),
        actor=body.get("actor", "operator"),
    ).to_dict()


@route("POST", r"/issues/(?P<issue_id>[^/]+)/assign")
def _assign(svc: FeedbackService, m: re.Match[str], body: dict[str, Any]) -> Any:
    return svc.assign(
        m.group("issue_id"),
        owner=body.get("owner", ""),
        team=body.get("team", ""),
        actor=body.get("actor", "operator"),
    ).to_dict()


@route("POST", r"/issues/(?P<issue_id>[^/]+)/commit")
def _commit(svc: FeedbackService, m: re.Match[str], body: dict[str, Any]) -> Any:
    return svc.commit(
        m.group("issue_id"),
        deadline=body.get("deadline", ""),
        note=body.get("note", ""),
        actor=body.get("actor", "operator"),
    ).to_dict()


@route("POST", r"/issues/(?P<issue_id>[^/]+)/conclude")
def _conclude(svc: FeedbackService, m: re.Match[str], body: dict[str, Any]) -> Any:
    return svc.conclude(
        m.group("issue_id"),
        decision=body.get("decision", ""),
        rationale=body.get("rationale", ""),
        supports=body.get("supports", []),
        counterexamples=body.get("counterexamples", []),
        actor=body.get("actor", "operator"),
    ).to_dict()


@route("POST", r"/issues/(?P<issue_id>[^/]+)/policy")
def _apply_policy(svc: FeedbackService, m: re.Match[str], body: dict[str, Any]) -> Any:
    return svc.apply_policy(
        m.group("issue_id"), body.get("version", ""), actor=body.get("actor", "operator")
    ).to_dict()


@route("POST", r"/policies")
def _register_policy(svc: FeedbackService, m: re.Match[str], body: dict[str, Any]) -> Any:
    return svc.register_policy(
        body.get("version", ""),
        note=body.get("note", ""),
        regions=body.get("regions", []),
        actor=body.get("actor", "operator"),
    ).to_dict()


@route("POST", r"/sweep")
def _sweep(svc: FeedbackService, m: re.Match[str], body: dict[str, Any]) -> Any:
    return {"escalations": [e.to_dict() for e in svc.sweep()]}


@route("GET", r"/regions/(?P<region>[^/]+)/report")
def _region_report(svc: FeedbackService, m: re.Match[str], body: dict[str, Any]) -> Any:
    return svc.region_report(m.group("region"))


def make_server(service: FeedbackService, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, obj: Any) -> None:
            payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _dispatch(self, method: str) -> None:
            path, _, query = self.path.partition("?")
            path = unquote(path)
            body: dict[str, Any] = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if raw:
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        return self._send(400, {"error": "请求体不是合法 JSON"})
            else:
                body = dict(parse_qsl(query))
            for route_method, pattern, fn in _ROUTES:
                if route_method != method:
                    continue
                match = pattern.match(path)
                if match is None:
                    continue
                try:
                    result = fn(service, match, body)
                except NotFoundError as exc:
                    return self._send(404, {"error": str(exc)})
                except DomainError as exc:
                    return self._send(409, {"error": str(exc)})
                return self._send(200, result)
            self._send(404, {"error": f"路由不存在: {method} {path}"})

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, format: str, *args: Any) -> None:
            pass  # 静默访问日志，避免污染 CLI/测试输出

    return ThreadingHTTPServer((host, port), Handler)


def serve(store_path: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    service = FeedbackService(store_path)  # 启动即补扫逾期升级
    server = make_server(service, host, port)
    print(f"回访归因服务已启动: http://{host}:{server.server_address[1]} （存储 {store_path}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
