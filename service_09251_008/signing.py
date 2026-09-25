"""公开版本快照的签名与验签。

签名对象是 *规范化*（键排序、无空白、UTF-8）后的 JSON，
覆盖批次标识、生效时间与简化后的公开条目，防止发布后被篡改。
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

ALGORITHM = "HS256"


def canonical_json(obj: Any) -> bytes:
    """确定性序列化：键排序、紧凑分隔、UTF-8。"""
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sign_payload(payload: dict, secret: str | bytes) -> str:
    """对载荷字典返回 HMAC-SHA256 十六进制摘要。"""
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    return hmac.new(key, canonical_json(payload), hashlib.sha256).hexdigest()


def verify_payload(payload: dict, signature: str, secret: str | bytes) -> bool:
    """常量时间比较验签。"""
    expected = sign_payload(payload, secret)
    return hmac.compare_digest(expected, signature or "")


def seal(payload: dict, secret: str | bytes, signed_at: str) -> dict:
    """把公开快照包装成带签名的信封（签名不覆盖 signature 字段自身）。"""
    body = dict(payload)
    body["alg"] = ALGORITHM
    body["signed_at"] = signed_at
    signature = sign_payload(body, secret)
    body["signature"] = signature
    return body


def unseal(envelope: dict, secret: str | bytes) -> bool:
    """校验信封签名（不抛异常，返回布尔）。"""
    env = dict(envelope)
    signature = env.pop("signature", None)
    return signature is not None and verify_payload(env, signature, secret)
