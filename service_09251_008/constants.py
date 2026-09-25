"""标识符与应用常量。"""
from __future__ import annotations

import uuid

# 紧急升级发布后必须补齐双人复核的期限（小时）
EMERGENCY_REVIEW_DEADLINE_HOURS = 24

# 等级
LEVEL_BUSY = "busy"        # 繁忙（三级）
LEVEL_HEAVY = "heavy"      # 严重繁忙（二级）
LEVEL_EXTREME = "extreme"  # 极端繁忙（一级）

LEVELS = (LEVEL_BUSY, LEVEL_HEAVY, LEVEL_EXTREME)
LEVEL_ORDER = {name: i for i, name in enumerate(LEVELS)}

# 规则发布的生命周期状态
RULE_DRAFT = "draft"
RULE_ACTIVE = "active"
RULE_RETIRED = "retired"

# 候选/发布流程状态
BATCH_CANDIDATE = "candidate"      # 内部候选，待复核
BATCH_PUBLISHED = "published"      # 常规复核通过后发布
BATCH_EMERGENCY = "emergency"      # 紧急升级先发布，待补审
BATCH_EXPIRED = "expired"          # 紧急件超期未补审
BATCH_REVOKED = "revoked"          # 被撤销
BATCH_REJECTED = "rejected"        # 复核驳回（未发布的候选）
BATCH_SUPERSEDED = "superseded"    # 被后续发布整体替代（审计保留）

# 审批动作
APPROVE = "approve"
REJECT = "reject"

# 人工例外类型
EXCEPTION_FORCE = "force_up"    # 强制提档
EXCEPTION_HOLD = "hold_down"    # 强制压低/不入选
EXCEPTION_EXEMPT = "exempt"     # 豁免（不参与评估）


def new_id(prefix: str) -> str:
    """生成带前缀的短标识。"""
    return f"{prefix}_{uuid.uuid4().hex[:16]}"
