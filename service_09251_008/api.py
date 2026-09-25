"""HTTP 接口边界。

两套权限隔离的 API：
  * ``/internal/*`` —— 仅 internal 作用域密钥可访问，暴露触发痕迹与管理操作；
  * ``/public/*``   —— 仅 public 作用域密钥可访问，只给简化结论与签名信封。
密钥作用域与路径不匹配一律 403（权限越界），无凭证/坏凭证 401。

使用标准库 ThreadingHTTPServer：并发请求下由应用层的跨进程锁与
``BEGIN IMMEDIATE`` 事务保证安全；启动即执行一次补审期限追踪（进程恢复），
随后后台线程周期追踪。
"""
from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .errors import AuthError, DomainError, PermissionError
from .services import AppService
from .storage import Repository
from .timeutil import Clock

INTERNAL_PREFIX = "/internal/"
PUBLIC_PREFIX = "/public/"


class _Router:
    def __init__(self) -> None:
        # (method, prefix) -> handler; 支持 {param} 段
        self.routes: list[tuple[str, int, list[str], Callable]] = []

    def add(self, method: str, pattern: str, handler: Callable) -> None:
        segs = [s for s in pattern.strip("/").split("/") if s]
        self.routes.append((method, len(segs), segs, handler))

    def match(self, method: str, path: str):
        segs = [s for s in path.strip("/").split("/") if s]
        for m, n, pattern, handler in self.routes:
            if m != method or n != len(segs):
                continue
            params: dict[str, str] = {}
            for pat, seg in zip(pattern, segs):
                if pat.startswith("{") and pat.endswith("}"):
                    params[pat[1:-1]] = seg
                elif pat != seg:
                    break
            else:
                return handler, params
        return None, None


class ApiServer:
    def __init__(
        self,
        repo: Repository,
        signing_secret: str = "",
        clock: Clock | None = None,
        sweep_interval_seconds: float = 60.0,
    ) -> None:
        self.app = AppService(repo, clock=clock, signing_secret=signing_secret)
        self.app.ensure_default_rule()
        self.sweep_interval = sweep_interval_seconds
        self.router = _Router()
        self._register_routes()
        # 这些 POST 创建新资源，返回 201；其余 POST 为动作，返回 200
        self._creators = {
            self._register_area, self._add_reading, self._create_rule,
            self._add_exception, self._generate_candidates,
            self._emergency_publish,
        }
        self._httpd: ThreadingHTTPServer | None = None
        self._sweeper: threading.Thread | None = None
        self._sweeper_stop = threading.Event()

    # -- 路由表 -----------------------------------------------------------

    def _register_routes(self) -> None:
        r = self.router
        # 内部
        r.add("POST", "/internal/areas", self._register_area)
        r.add("POST", "/internal/readings", self._add_reading)
        r.add("GET", "/internal/rules", self._list_rules)
        r.add("POST", "/internal/rules", self._create_rule)
        r.add("POST", "/internal/rules/{version}/activate", self._activate_rule)
        r.add("POST", "/internal/exceptions", self._add_exception)
        r.add("DELETE", "/internal/exceptions/{exc_id}", self._revoke_exception)
        r.add("POST", "/internal/candidates", self._generate_candidates)
        r.add("GET", "/internal/batches", self._list_batches)
        r.add("GET", "/internal/batches/{batch_id}", self._get_batch)
        r.add("POST", "/internal/batches/{batch_id}/review", self._review)
        r.add("POST", "/internal/batches/{batch_id}/revoke", self._revoke)
        r.add("POST", "/internal/emergency-publish", self._emergency_publish)
        r.add("GET", "/internal/rebuild", self._rebuild)
        r.add("GET", "/internal/audit", self._audit)
        # 公众
        r.add("GET", "/public/current", self._public_current)
        r.add("GET", "/public/batches/{batch_id}", self._public_batch)
        r.add("GET", "/public/health", self._health)

    # -- 生命周期 ---------------------------------------------------------

    def start(self, host: str, port: int) -> ThreadingHTTPServer:
        # 进程恢复：启动时先追踪一次补审期限
        self.app.run_deadline_sweep()

        handler_cls = self._make_handler()
        self._httpd = ThreadingHTTPServer((host, port), handler_cls)

        if self.sweep_interval > 0:
            self._sweeper = threading.Thread(
                target=self._sweep_loop, name="deadline-sweeper", daemon=True
            )
            self._sweeper.start()
        return self._httpd

    def shutdown(self) -> None:
        self._sweeper_stop.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

    def _sweep_loop(self) -> None:
        while not self._sweeper_stop.wait(self.sweep_interval):
            try:
                self.app.run_deadline_sweep()
            except Exception:  # noqa: BLE001 - 追踪线程不能因单次异常退出
                pass

    # -- 处理器 -----------------------------------------------------------

    def _make_handler(self) -> type:
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "BusyAreaService/1.0"

            def log_message(self, fmt: str, *args: Any) -> None:
                pass  # 静默；需要访问日志时可在此挂钩

            def _send_json(self, status: int, body: Any) -> None:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authenticate(self, required_scope: str):
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer "):
                    raise AuthError("缺少 Bearer 凭证")
                token = auth[len("Bearer "):].strip()
                row = server.app.repo.get_api_key(token)
                if row is None:
                    raise AuthError("凭证无效或已停用")
                if row["scope"] != required_scope:
                    # 凭证本身有效，但作用域越界
                    raise PermissionError(
                        f"该密钥无权访问 {required_scope} 接口"
                    )
                return row["label"]

            def _read_json(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    from .errors import ValidationError

                    raise ValidationError("请求体不是合法 JSON") from exc
                if not isinstance(body, dict):
                    from .errors import ValidationError

                    raise ValidationError("请求体必须是 JSON 对象")
                return body

            def _dispatch(self, method: str) -> None:
                parsed = urlparse(self.path)
                path = parsed.path
                query = {
                    k: v[0] for k, v in parse_qs(parsed.query).items()
                }
                try:
                    if path.startswith(INTERNAL_PREFIX):
                        scope = "internal"
                    elif path.startswith(PUBLIC_PREFIX):
                        scope = "public"
                    elif path in ("/", "/health"):
                        self._send_json(HTTPStatus.OK, {
                            "service": "busy-area-grading", "status": "ok"
                        })
                        return
                    else:
                        self._send_json(HTTPStatus.NOT_FOUND,
                                        {"error": "not_found"})
                        return

                    actor = self._authenticate(scope)
                    handler, params = server.router.match(method, path)
                    if handler is None:
                        self._send_json(HTTPStatus.NOT_FOUND,
                                        {"error": "not_found", "path": path})
                        return
                    body = self._read_json() if method in ("POST", "PUT", "DELETE") \
                        else {}
                    result = handler(actor, body, query, params)
                    if isinstance(result, tuple):
                        status, result = result
                    elif result is None:
                        status = HTTPStatus.OK
                        result = {"ok": True}
                    else:
                        # 资源创建类 POST 用 201，其余动作默认 200
                        status = (HTTPStatus.CREATED if method == "POST"
                                  and handler in server._creators
                                  else HTTPStatus.OK)
                    self._send_json(status, result)
                except DomainError as exc:
                    self._send_json(exc.http_status,
                                    {"error": exc.code, "message": str(exc)})
                except KeyError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST,
                                    {"error": "missing_field",
                                     "message": f"缺少字段 {exc.args[0]}"})

            def do_GET(self) -> None:
                self._dispatch("GET")

            def do_POST(self) -> None:
                self._dispatch("POST")

            def do_DELETE(self) -> None:
                self._dispatch("DELETE")

        return Handler

    # -- 内部处理函数 -----------------------------------------------------

    def _register_area(self, actor, body, query, params):
        self.app.register_area(body["area_id"], body["name"])
        return {"area_id": body["area_id"]}

    def _add_reading(self, actor, body, query, params):
        self.app.add_reading(
            body["area_id"], body["metric_key"], body["day"], body["value"]
        )
        return {"accepted": True}

    def _list_rules(self, actor, body, query, params):
        return {"rules": self.app.list_rule_versions()}

    def _create_rule(self, actor, body, query, params):
        version = self.app.create_rule_version(body, actor)
        return {"version": version}

    def _activate_rule(self, actor, body, query, params):
        self.app.activate_rule(params["version"], actor)
        return {"active": params["version"]}

    def _add_exception(self, actor, body, query, params):
        data = dict(body)
        data.setdefault("area_id", body.get("area_id"))
        exc_id = self.app.add_exception(data, actor)
        return {"exc_id": exc_id}

    def _revoke_exception(self, actor, body, query, params):
        self.app.revoke_exception(params["exc_id"])
        return {"revoked": params["exc_id"]}

    def _generate_candidates(self, actor, body, query, params):
        day = body.get("day") or query.get("day")
        return self.app.generate_candidates(actor, day)

    def _list_batches(self, actor, body, query, params):
        return {"batches": self.app.list_batches()}

    def _get_batch(self, actor, body, query, params):
        return self.app.batch_view(params["batch_id"])

    def _review(self, actor, body, query, params):
        return self.app.review(
            params["batch_id"],
            actor,  # 复核人身份取自密钥，无法冒用他人
            body["stage"],
            body["decision"],
            body.get("comment"),
        )

    def _revoke(self, actor, body, query, params):
        return self.app.revoke(params["batch_id"], actor, body["reason"])

    def _emergency_publish(self, actor, body, query, params):
        day = body.get("day") or query.get("day")
        return self.app.emergency_publish(actor, body["reason"], day)

    def _rebuild(self, actor, body, query, params):
        if query.get("mode") == "snapshot":
            return self.app.rebuild_snapshot(query["day"])
        return self.app.rebuild_candidate(
            query["day"], query.get("rule_version")
        )

    def _audit(self, actor, body, query, params):
        limit = int(query.get("limit", "100"))
        return {"entries": self.app.audit_tail(limit)}

    # -- 公众处理函数 -----------------------------------------------------

    def _public_current(self, actor, body, query, params):
        return self.app.public_current(query.get("day"))

    def _public_batch(self, actor, body, query, params):
        # 只返回经过签名校验的简化信封，内部痕迹不会外泄
        return self.app.public_manifest(params["batch_id"])

    def _health(self, actor, body, query, params):
        return {"status": "ok"}
