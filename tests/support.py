"""测试公共辅助：确定性时钟、标识与密钥，以及常用编排。"""
from __future__ import annotations

from pathlib import Path

from service_09251_008.domain import parse_ts
from service_09251_008.ports import ManualClock, SequentialIds, UuidIds
from service_09251_008.services import AppService
from service_09251_008.storage import Repository

SIGNING_KEY = bytes.fromhex("ab" * 32)

DEFAULT_RULES = [
    {"grade": "L3", "all": [{"metric": "occupancy", "op": ">=", "value": 90}]},
    {"grade": "L2", "all": [{"metric": "occupancy", "op": ">=", "value": 75}]},
    {"grade": "L1", "all": [{"metric": "occupancy", "op": ">=", "value": 60}]},
]

DEFAULT_METRICS = [
    {"code": "occupancy", "name": "泊位占用率", "unit": "%"},
    {"code": "avg_speed", "name": "平均车速", "unit": "km/h"},
]


def make_service(tmp_dir: str, start: str = "2026-09-30T08:00:00Z", emergency_review_hours: int = 24):
    """在临时目录上构建全新服务实例（文件库，支持模拟重启）。"""
    clock = ManualClock(parse_ts(start))
    repo = Repository(str(Path(tmp_dir) / "test.db"))
    service = AppService(repo, clock, SequentialIds(), SIGNING_KEY, emergency_review_hours=emergency_review_hours)
    return service, clock


def reopen_service(service: AppService, tmp_dir: str, clock: ManualClock) -> AppService:
    """模拟进程重启：关闭旧连接，在同一数据库上构建新实例。"""
    service.repo.close()
    repo = Repository(str(Path(tmp_dir) / "test.db"))
    return AppService(
        repo,
        clock,
        UuidIds(),  # 新进程使用新的标识序列，避免与已持久化的标识冲突
        SIGNING_KEY,
        emergency_review_hours=service.emergency_review_hours,
    )


def seed_metrics_and_rules(service: AppService, actor: str = "ops", rules=None):
    metric_version = service.create_metric_version(actor, DEFAULT_METRICS)
    service.activate_metric_version(actor, metric_version["id"])
    rule_version = service.create_rule_version(actor, metric_version["id"], rules or DEFAULT_RULES)
    service.activate_rule_version(actor, rule_version["id"])
    return metric_version, rule_version


def seed_period(service: AppService, start: str, end: str, name: str = "国庆高峰", actor: str = "ops") -> dict:
    return service.create_period(actor, name, start, end)


def observe(service: AppService, period_id: str, area: str, occupancy: float, at: str, actor: str = "ops"):
    return service.record_observations(
        actor,
        period_id,
        [{"area_code": area, "metric": "occupancy", "value": occupancy, "observed_at": at}],
    )


def drive_to_approved(
    service: AppService,
    period_id: str,
    creator: str = "ops",
    reviewers: tuple[str, str] = ("alice", "bob"),
) -> dict:
    """生成候选清单并完成双人复核。"""
    candidate = service.generate_candidate_list(creator, period_id)
    service.submit_candidate_list(creator, candidate["id"])
    for reviewer in reviewers:
        service.review_candidate_list(reviewer, candidate["id"], "APPROVE")
    return service.get_candidate_list(creator, candidate["id"])


def drive_to_published(
    service: AppService,
    period_id: str,
    creator: str = "ops",
    reviewers: tuple[str, str] = ("alice", "bob"),
    valid_from: str | None = None,
    valid_to: str | None = None,
) -> dict:
    approved = drive_to_approved(service, period_id, creator, reviewers)
    return service.publish_candidate_list(creator, approved["id"], valid_from, valid_to)
