"""命令行入口。

子命令：
  issue-key   签发 internal/public 作用域的 Bearer 密钥
  serve       启动 HTTP 服务（启动时执行补审期限追踪，即进程恢复）
  rebuild     历史重建：候选重算（可指定历史规则版本）或某日公开快照
  verify      对公开清单信封做签名校验
  sign        （运维）对一个公开清单 JSON 文件签名，输出带签名信封
  demo-init   写入演示用的服务区、读数与两套路密钥，便于快速体验

运行数据默认放在 ./data（可用 BUSY_AREA_DB 覆盖），不写入源码目录。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys

from . import PROJECT_CODE
from .api import ApiServer
from .services import AppService
from .signing import unseal
from .storage import Repository
from .timeutil import Clock, iso

DEFAULT_DB = os.environ.get(
    "BUSY_AREA_DB", os.path.join(os.getcwd(), "data", "busy_area.db")
)
DEFAULT_SECRET = os.environ.get("BUSY_AREA_SIGNING_SECRET", "")


def _repo(args: argparse.Namespace) -> Repository:
    return Repository(args.db)


def _secret(args: argparse.Namespace) -> str:
    secret = args.secret if args.secret is not None else DEFAULT_SECRET
    if not secret:
        raise SystemExit(
            "缺少签名密钥：请用 --secret 或设置 BUSY_AREA_SIGNING_SECRET"
        )
    return secret


def cmd_issue_key(args: argparse.Namespace) -> None:
    repo = _repo(args)
    token = f"{args.scope}_{secrets.token_urlsafe(32)}"
    repo.insert_api_key(token, args.scope, args.label, iso(Clock().now()))
    out = {
        "token": token,
        "scope": args.scope,
        "label": args.label,
        "usage": f"Authorization: Bearer {token}",
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if args.scope == "internal":
        print("# internal 密钥：候选/审批/发布/规则/例外/重建/审计", file=sys.stderr)
    else:
        print("# public 密钥：仅可读取简化公开结论", file=sys.stderr)


def cmd_serve(args: argparse.Namespace) -> None:
    repo = _repo(args)
    server = ApiServer(
        repo,
        signing_secret=_secret(args),
        sweep_interval_seconds=args.sweep_interval,
    )
    httpd = server.start(args.host, args.port)
    print(f"[{PROJECT_CODE}] 监听 http://{args.host}:{args.port} "
          f"(db={args.db})", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


def cmd_rebuild(args: argparse.Namespace) -> None:
    repo = _repo(args)
    app = AppService(repo, signing_secret=_secret(args))
    if args.mode == "snapshot":
        result = app.rebuild_snapshot(args.day)
    else:
        result = app.rebuild_candidate(args.day, args.rule_version)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_verify(args: argparse.Namespace) -> None:
    envelope = json.load(args.file)
    ok = unseal(envelope, _secret(args))
    print(json.dumps({"valid_signature": ok}, ensure_ascii=False))
    if not ok:
        sys.exit(1)


def cmd_sign(args: argparse.Namespace) -> None:
    from .signing import seal

    payload = json.load(args.file)
    print(json.dumps(
        seal(payload, _secret(args), iso(Clock().now())),
        ensure_ascii=False, indent=2,
    ))


def cmd_demo_init(args: argparse.Namespace) -> None:
    """灌入演示数据并签发两套密钥，输出调用提示。"""
    repo = _repo(args)
    secret = _secret(args)
    app = AppService(repo, signing_secret=secret)
    app.ensure_default_rule()
    seed = [
        ("SA001", "阳澄湖服务区"),
        ("SA002", "梅村服务区"),
        ("SA003", "芳茂山服务区"),
    ]
    for aid, name in seed:
        app.register_area(aid, name)
    # 三天读数：SA001 触发 heavy，SA002 触发 busy，SA003 无数据
    readings = [
        ("SA001", "flow_saturation", 0.82),
        ("SA001", "flow_saturation", 0.91),
        ("SA001", "flow_saturation", 0.93),
        ("SA001", "queue_minutes", 45.0),
        ("SA001", "avg_speed_kmh", 22.0),
        ("SA002", "flow_saturation", 0.81),
        ("SA002", "queue_minutes", 10.0),
        ("SA002", "avg_speed_kmh", 55.0),
    ]
    from datetime import date, timedelta

    today = date.today()
    for i, (aid, metric, value) in enumerate(readings):
        day = (today - timedelta(days=i % 3)).isoformat()
        app.add_reading(aid, metric, day, value)

    keys = {}
    for scope, label in (("internal", "值班员-演示"), ("public", "发布渠道-演示")):
        token = f"{scope}_{secrets.token_urlsafe(24)}"
        repo.insert_api_key(token, scope, label, iso(Clock().now()))
        keys[scope] = token
    print(json.dumps({
        "db": args.db,
        "keys": keys,
        "hint": "POST /internal/candidates 生成候选；两不同 label 密钥分别提交 "
                "first/second 审批；GET /public/current 查看公开结论",
    }, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="busy-area", description="繁忙服务区分级发布服务"
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite 数据库路径")
    parser.add_argument("--secret", default=None,
                        help="签名密钥（默认读 BUSY_AREA_SIGNING_SECRET）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("issue-key", help="签发 API 密钥")
    p.add_argument("--scope", choices=("internal", "public"), required=True)
    p.add_argument("--label", required=True, help="持有人标识（审批身份）")
    p.set_defaults(func=cmd_issue_key)

    p = sub.add_parser("serve", help="启动 HTTP 服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--sweep-interval", type=float, default=60.0,
                   dest="sweep_interval", help="补审期限追踪周期秒数")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("rebuild", help="历史重建")
    p.add_argument("--day", required=True)
    p.add_argument("--mode", choices=("candidate", "snapshot"),
                   default="candidate")
    p.add_argument("--rule-version", default=None, dest="rule_version")
    p.set_defaults(func=cmd_rebuild)

    p = sub.add_parser("verify", help="校验公开清单签名")
    p.add_argument("file", type=argparse.FileType("r"),
                   help="清单信封 JSON 文件（- 表示标准输入）")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("sign", help="对公开清单 JSON 签名")
    p.add_argument("file", type=argparse.FileType("r"))
    p.set_defaults(func=cmd_sign)

    p = sub.add_parser("demo-init", help="初始化演示数据与密钥")
    p.set_defaults(func=cmd_demo_init)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
