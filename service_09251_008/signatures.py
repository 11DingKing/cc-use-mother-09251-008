"""公开版本的签名校验。

签名采用 HMAC-SHA256，覆盖公开版本的规范化 JSON（键排序、紧凑分隔符、
UTF-8 编码）。撤销信息不参与签名——签名固定的是发布时刻的内容，
撤销作为独立事件记录在审计链中。
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any


def canonical_json(payload: Any) -> bytes:
    """生成确定性的规范化 JSON 字节串。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_payload(key: bytes, payload: Any) -> str:
    """对载荷计算 HMAC-SHA256 签名（十六进制）。"""
    return hmac.new(key, canonical_json(payload), hashlib.sha256).hexdigest()


def verify_payload(key: bytes, payload: Any, signature: str) -> bool:
    """校验载荷签名，使用恒定时间比较。"""
    expected = sign_payload(key, payload)
    return hmac.compare_digest(expected, signature)


def audit_hash(prev_hash: str, at: str, actor: str, action: str, subject: str, payload_json: str) -> str:
    """计算审计链上单个事件的哈希。"""
    material = f"{prev_hash}|{at}|{actor}|{action}|{subject}|{payload_json}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


GENESIS_HASH = "0" * 64
