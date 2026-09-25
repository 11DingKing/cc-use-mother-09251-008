"""命令行入口：服务启动、进程恢复、期限追踪、历史重建与签名校验。

用法示例：
    python3 -m service_09251_008 serve
    python3 -m service_09251_008 recover
    python3 -m service_09251_008 sweep
    python3 -m service_09251_008 reconstruct --at 2026-10-01T23:00:00Z
    python3 -m service_09251_008 verify-signature --version 1
    python3 -m service_09251_008 verify-audit
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import build_service, load_config


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="service_09251_008", description="繁忙服务区分级发布 服务端")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认取环境变量或用户数据目录）")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="启动 HTTP 服务（启动前自动执行进程恢复）")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)

    commands.add_parser("recover", help="执行进程恢复（期限追踪 + 审计链校验）")
    commands.add_parser("sweep", help="扫描并标记超过补审期限的紧急升级")

    reconstruct = commands.add_parser("reconstruct", help="重建指定时刻的公开清单")
    reconstruct.add_argument("--at", required=True, help="ISO 8601 时间，如 2026-10-01T23:00:00Z")

    verify_signature = commands.add_parser("verify-signature", help="校验公开版本签名")
    verify_signature.add_argument("--version", type=int, required=True, help="公开版本号")

    commands.add_parser("verify-audit", help="校验审计哈希链")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(data_dir=args.data_dir)
    if getattr(args, "host", None):
        config.host = args.host
    if getattr(args, "port", None):
        config.port = args.port

    if args.command == "serve":
        from .httpapi import run_server

        run_server(config)
        return 0

    service = build_service(config)
    if args.command == "recover":
        report = service.recover()
        _print(report)
        return 0 if report["audit_valid"] else 1
    if args.command == "sweep":
        _print(service.sweep_emergencies())
        return 0
    if args.command == "reconstruct":
        _print(service.reconstruct(args.at))
        return 0
    if args.command == "verify-signature":
        result = service.verify_version_signature(args.version)
        _print(result)
        return 0 if result["valid"] else 1
    if args.command == "verify-audit":
        result = service.verify_audit_chain()
        _print(result)
        return 0 if result["valid"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
