"""HTTP 接口边界：内部与公众两套权限隔离的 API。

- /internal/**  需要内部令牌（Bearer），返回完整细节（含触发指标）；
- /public/**    接受公众令牌或内部令牌，只返回适合发布的简化结论；
- 无令牌 / 未知令牌 -> 401；公众令牌访问内部接口 -> 403。

基于标准库 http.server，路由、鉴权与错误映射都集中在本模块，
应用服务不感知 HTTP 细节。
"""
from __future__ import annotations

import json
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .config import Config
from .errors import DomainError, unauthorized, validation
from .services import AppService

Handler = Callable[[Any], Any]

#: 路由作用域：open 无需令牌；internal 仅内部令牌；public 公众或内部令牌。
SCOPE_OPEN = "open"
SCOPE_INTERNAL = "internal"
SCOPE_PUBLIC = "public"


def _first(query: dict[str, list[str]], name: str, default: Any = None) -> Any:
    values = query.get(name)
    return values[0] if values else default


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise validation(f"版本号必须是整数: {value!r}")


def build_routes(service: AppService) -> list[tuple[str, str, str, Handler]]:
    """路由表：(方法, 路径模式, 作用域, 处理器)。路径参数形如 {name}。"""
    svc = service
    return [
        ("GET", "/health", SCOPE_OPEN, lambda c: {"status": "ok"}),
        # ---- 内部 API -------------------------------------------------
        ("POST", "/internal/metric-versions", SCOPE_INTERNAL,
         lambda c: svc.create_metric_version(c.identity, c.body.get("metrics"))),
        ("GET", "/internal/metric-versions", SCOPE_INTERNAL,
         lambda c: svc.list_metric_versions(c.identity)),
        ("POST", "/internal/metric-versions/{vid}/activate", SCOPE_INTERNAL,
         lambda c: svc.activate_metric_version(c.identity, c.params["vid"])),
        ("POST", "/internal/rule-versions", SCOPE_INTERNAL,
         lambda c: svc.create_rule_version(c.identity, c.body.get("metric_version_id"), c.body.get("rules"))),
        ("GET", "/internal/rule-versions", SCOPE_INTERNAL,
         lambda c: svc.list_rule_versions(c.identity)),
        ("POST", "/internal/rule-versions/{vid}/activate", SCOPE_INTERNAL,
         lambda c: svc.activate_rule_version(c.identity, c.params["vid"])),
        ("POST", "/internal/periods", SCOPE_INTERNAL,
         lambda c: svc.create_period(c.identity, c.body.get("name"), c.body.get("start"), c.body.get("end"))),
        ("GET", "/internal/periods", SCOPE_INTERNAL,
         lambda c: svc.list_periods(c.identity)),
        ("POST", "/internal/periods/{pid}/observations", SCOPE_INTERNAL,
         lambda c: svc.record_observations(c.identity, c.params["pid"], c.body.get("items"))),
        ("POST", "/internal/exceptions", SCOPE_INTERNAL,
         lambda c: svc.create_exception(
             c.identity,
             c.body.get("area_code"),
             c.body.get("action"),
             c.body.get("grade"),
             c.body.get("valid_from"),
             c.body.get("valid_to"),
             c.body.get("reason"),
         )),
        ("GET", "/internal/exceptions", SCOPE_INTERNAL,
         lambda c: svc.list_exceptions(c.identity)),
        ("POST", "/internal/exceptions/{eid}/revoke", SCOPE_INTERNAL,
         lambda c: svc.revoke_exception(c.identity, c.params["eid"])),
        ("POST", "/internal/candidate-lists/generate", SCOPE_INTERNAL,
         lambda c: svc.generate_candidate_list(c.identity, c.body.get("period_id"))),
        ("GET", "/internal/candidate-lists", SCOPE_INTERNAL,
         lambda c: svc.list_candidate_lists(c.identity)),
        ("GET", "/internal/candidate-lists/{lid}", SCOPE_INTERNAL,
         lambda c: svc.get_candidate_list(c.identity, c.params["lid"])),
        ("POST", "/internal/candidate-lists/{lid}/submit", SCOPE_INTERNAL,
         lambda c: svc.submit_candidate_list(c.identity, c.params["lid"])),
        ("POST", "/internal/candidate-lists/{lid}/reviews", SCOPE_INTERNAL,
         lambda c: svc.review_candidate_list(c.identity, c.params["lid"], c.body.get("decision"))),
        ("POST", "/internal/candidate-lists/{lid}/publish", SCOPE_INTERNAL,
         lambda c: svc.publish_candidate_list(
             c.identity, c.params["lid"], c.body.get("valid_from"), c.body.get("valid_to"))),
        ("POST", "/internal/emergency-upgrades", SCOPE_INTERNAL,
         lambda c: svc.emergency_upgrade(c.identity, c.body.get("reason"), c.body.get("entries"))),
        ("GET", "/internal/emergency-upgrades", SCOPE_INTERNAL,
         lambda c: svc.list_emergencies(c.identity)),
        ("POST", "/internal/emergency-upgrades/{eid}/reviews", SCOPE_INTERNAL,
         lambda c: svc.review_emergency(c.identity, c.params["eid"], c.body.get("decision"))),
        ("POST", "/internal/emergency-upgrades/sweep", SCOPE_INTERNAL,
         lambda c: svc.sweep_emergencies(c.identity)),
        ("GET", "/internal/public-versions", SCOPE_INTERNAL,
         lambda c: svc.list_public_versions(c.identity)),
        ("GET", "/internal/public-versions/{vno}", SCOPE_INTERNAL,
         lambda c: svc.get_public_version(c.identity, _as_int(c.params["vno"]))),
        ("POST", "/internal/public-versions/{vno}/entries/{area}/revoke", SCOPE_INTERNAL,
         lambda c: svc.revoke_public_entry(c.identity, _as_int(c.params["vno"]), c.params["area"])),
        ("GET", "/internal/public-versions/{vno}/verify", SCOPE_INTERNAL,
         lambda c: svc.verify_version_signature(_as_int(c.params["vno"]))),
        ("GET", "/internal/history/reconstruct", SCOPE_INTERNAL,
         lambda c: svc.reconstruct(_first(c.query, "at"))),
        ("GET", "/internal/audit/verify", SCOPE_INTERNAL,
         lambda c: svc.verify_audit_chain()),
        # ---- 公众 API（只暴露简化结论） --------------------------------
        ("GET", "/public/current", SCOPE_PUBLIC, lambda c: svc.public_current()),
        ("GET", "/public/versions/{vno}", SCOPE_PUBLIC,
         lambda c: svc.public_version_view(_as_int(c.params["vno"]))),
        ("GET", "/public/versions/{vno}/verify", SCOPE_PUBLIC,
         lambda c: svc.verify_version_signature(_as_int(c.params["vno"]))),
    ]


def _match(pattern: str, path: str) -> dict[str, str] | None:
    pattern_parts = [part for part in pattern.strip("/").split("/") if part]
    path_parts = [part for part in path.strip("/").split("/") if part]
    if len(pattern_parts) != len(path_parts):
        return None
    params: dict[str, str] = {}
    for expected, actual in zip(pattern_parts, path_parts):
        if expected.startswith("{") and expected.endswith("}"):
            params[expected[1:-1]] = actual
        elif expected != actual:
            return None
    return params


def make_handler(service: AppService, config: Config) -> type[BaseHTTPRequestHandler]:
    routes = build_routes(service)

    class Handler(BaseHTTPRequestHandler):
        server_version = "BusyAreaService/1.0"

        # -- 鉴权 ------------------------------------------------------
        def _authenticate(self, scope: str) -> str:
            if scope == SCOPE_OPEN:
                return "anonymous"
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                raise unauthorized()
            token = header[len("Bearer "):].strip()
            if token in config.internal_tokens:
                return config.internal_tokens[token]
            if token in config.public_tokens:
                if scope == SCOPE_INTERNAL:
                    raise DomainError("FORBIDDEN", "公众令牌无权访问内部接口", 403)
                return f"public:{token}"
            raise unauthorized()

        # -- 请求/响应 --------------------------------------------------
        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise DomainError("VALIDATION", "请求体不是合法的 JSON", 400)
            if not isinstance(body, dict):
                raise DomainError("VALIDATION", "请求体必须是 JSON 对象", 400)
            return body

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method: str) -> None:
            try:
                split = urlsplit(self.path)
                path = split.path.rstrip("/") or "/"
                query = parse_qs(split.query)
                route = None
                params: dict[str, str] = {}
                for route_method, pattern, scope, handler in routes:
                    if route_method != method:
                        continue
                    matched = _match(pattern, path)
                    if matched is not None:
                        route = (scope, handler)
                        params = matched
                        break
                if route is None:
                    raise DomainError("NOT_FOUND", f"接口不存在: {method} {path}", 404)
                scope, handler = route
                identity = self._authenticate(scope)
                body = self._read_body() if method in ("POST", "PUT", "PATCH") else {}
                ctx = SimpleNamespace(identity=identity, body=body, query=query, params=params)
                result = handler(ctx)
                self._send(200, {"data": result})
            except DomainError as err:
                self._send(err.http_status, {"error": err.to_dict()})
            except Exception:  # noqa: BLE001 - 边界兜底，避免泄露内部细节
                traceback.print_exc()
                self._send(500, {"error": {"code": "INTERNAL", "message": "内部错误"}})

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, fmt: str, *args: Any) -> None:
            pass  # 静默访问日志，避免污染 CLI 输出

    return Handler


def run_server(config: Config, service: AppService | None = None) -> None:
    """启动 HTTP 服务；启动前执行进程恢复。"""
    from .config import build_service

    app = service or build_service(config)
    report = app.recover()
    handler = make_handler(app, config)
    server = ThreadingHTTPServer((config.host, config.port), handler)
    server.daemon_threads = True
    print(
        json.dumps(
            {
                "listening": f"http://{config.host}:{config.port}",
                "recovery": report,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def start_test_server(config: Config, service: AppService) -> tuple[ThreadingHTTPServer, threading.Thread, int]:
    """测试辅助：在随机端口启动服务，返回 (server, thread, port)。"""
    handler = make_handler(service, config)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, server.server_address[1]
