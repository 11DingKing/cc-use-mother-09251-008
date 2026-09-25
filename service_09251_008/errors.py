"""统一的领域错误类型。

应用服务抛出 :class:`DomainError`，接口边界（HTTP / CLI）把它映射为
对应的状态码与错误码，避免在各层重复编写异常处理。
"""
from __future__ import annotations


class DomainError(Exception):
    """携带错误码与 HTTP 状态码的领域错误。"""

    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


def not_found(message: str) -> DomainError:
    return DomainError("NOT_FOUND", message, 404)


def validation(message: str) -> DomainError:
    return DomainError("VALIDATION", message, 400)


def conflict(message: str, code: str = "STATE_CONFLICT") -> DomainError:
    return DomainError(code, message, 409)


def forbidden(message: str, code: str = "FORBIDDEN") -> DomainError:
    return DomainError(code, message, 403)


def unauthorized(message: str = "缺少或无效的访问令牌") -> DomainError:
    return DomainError("UNAUTHORIZED", message, 401)
