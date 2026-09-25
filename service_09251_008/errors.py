"""领域错误类型。

所有业务规则违背都抛出对应子类，接口层据此映射为 HTTP 状态码。
"""
from __future__ import annotations


class DomainError(Exception):
    """业务错误基类。"""

    code = "domain_error"
    http_status = 400


class ValidationError(DomainError):
    """输入数据不合法。"""

    code = "validation_error"
    http_status = 400


class AuthError(DomainError):
    """缺少或携带了无法识别的凭证。"""

    code = "unauthorized"
    http_status = 401


class PermissionError(DomainError):  # noqa: A001 - 领域内有意遮蔽内置名
    """凭证有效但无权执行该操作，或越界访问另一套 API。"""

    code = "forbidden"
    http_status = 403


class NotFoundError(DomainError):
    """资源不存在。"""

    code = "not_found"
    http_status = 404


class ConflictError(DomainError):
    """状态冲突：重复审批、重复发布、并发竞争等。"""

    code = "conflict"
    http_status = 409


class DuplicateApprovalError(ConflictError):
    """同一复核人对同一阶段重复提交审批。"""

    code = "duplicate_approval"


class DeadlinePassedError(ConflictError):
    """紧急升级的补审期限已过。"""

    code = "deadline_passed"
