"""运行配置：全部来自环境变量，运行数据与本地配置不写入源码目录。

环境变量（前缀 S09251_008_）：
- DATA_DIR               数据目录（默认 $XDG_DATA_HOME/service_09251_008
                         或 ~/.local/share/service_09251_008）
- HOST / PORT            监听地址（默认 127.0.0.1:9251）
- INTERNAL_TOKENS        内部令牌，逗号分隔的 "token:identity"（默认开发令牌）
- PUBLIC_TOKENS          公众令牌，逗号分隔（默认开发令牌）
- SIGNING_KEY_FILE       签名密钥文件（默认 DATA_DIR/signing.key）
- EMERGENCY_REVIEW_HOURS 紧急升级补审期限（默认 24 小时）
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .ports import Clock, FileKeyProvider, IdGenerator, SystemClock, UuidIds
from .services import AppService
from .storage import Repository

ENV_PREFIX = "S09251_008_"

#: 本地开发默认令牌；生产部署必须通过环境变量覆盖。
DEFAULT_INTERNAL_TOKENS = {
    "dev-internal-alice": "alice",
    "dev-internal-bob": "bob",
    "dev-internal-carol": "carol",
}
DEFAULT_PUBLIC_TOKENS = {"dev-public"}


def _default_data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "service_09251_008"


def _parse_internal_tokens(raw: str) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        token, _, identity = pair.partition(":")
        tokens[token.strip()] = identity.strip() or token.strip()
    return tokens


@dataclass
class Config:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 9251
    internal_tokens: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_INTERNAL_TOKENS))
    public_tokens: set[str] = field(default_factory=lambda: set(DEFAULT_PUBLIC_TOKENS))
    signing_key_file: Path | None = None
    emergency_review_hours: int = 24

    @property
    def db_path(self) -> Path:
        return self.data_dir / "service_09251_008.db"

    @property
    def resolved_signing_key_file(self) -> Path:
        return self.signing_key_file or (self.data_dir / "signing.key")


def load_config(env: Mapping[str, str] | None = None, *, data_dir: str | None = None) -> Config:
    env = env if env is not None else os.environ

    def get(name: str, default: str | None = None) -> str | None:
        return env.get(ENV_PREFIX + name, default)

    config = Config(
        data_dir=Path(data_dir or get("DATA_DIR") or _default_data_dir()),
        host=get("HOST", "127.0.0.1") or "127.0.0.1",
        port=int(get("PORT", "9251") or "9251"),
        emergency_review_hours=int(get("EMERGENCY_REVIEW_HOURS", "24") or "24"),
    )
    if get("INTERNAL_TOKENS"):
        config.internal_tokens = _parse_internal_tokens(get("INTERNAL_TOKENS") or "")
    if get("PUBLIC_TOKENS"):
        config.public_tokens = {token.strip() for token in (get("PUBLIC_TOKENS") or "").split(",") if token.strip()}
    if get("SIGNING_KEY_FILE"):
        config.signing_key_file = Path(get("SIGNING_KEY_FILE") or "")
    return config


def build_service(
    config: Config,
    clock: Clock | None = None,
    ids: IdGenerator | None = None,
) -> AppService:
    """按配置装配应用服务（serve 与 CLI 共用）。"""
    config.data_dir.mkdir(parents=True, exist_ok=True)
    repo = Repository(config.db_path)
    signing_key = FileKeyProvider(config.resolved_signing_key_file).signing_key()
    return AppService(
        repo,
        clock or SystemClock(),
        ids or UuidIds(),
        signing_key,
        emergency_review_hours=config.emergency_review_hours,
    )
