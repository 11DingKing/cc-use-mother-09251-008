# 繁忙服务区分级发布

节日期间服务区繁忙等级会频繁升降级。本系统维护**指标版本、分级规则、观察期
与人工例外**，由指标证据产生**内部候选清单**（带触发痕迹），经**双人复核**
后形成**公开版本**；**紧急升级可先发布后补审**（默认 24h 补审期限，自动追踪），
**撤销只截断尚未结束的有效区间**。系统提供内部与公众两套权限隔离的 API，
并内置历史重建与签名校验命令。

纯 Python 标准库实现（`sqlite3` + `http.server` + `hmac`），无第三方依赖。

## 目录结构

```
service_09251_008/
  constants.py     # 等级、批次状态、紧急补审期限等常量
  errors.py        # 业务错误 -> HTTP 状态码映射
  timeutil.py      # 可替换时钟（跨日有效期、补审期限测试用）
  rules.py         # 规则版本、指标规格、观察期窗口、人工例外、评估引擎
  signing.py       # 公开清单 HMAC-SHA256 签名/验签
  storage.py       # SQLite：BEGIN IMMEDIATE + 跨进程 flock，schema 初始化
  services.py      # 应用服务：候选/审批/发布/紧急/撤销/重建
  api.py           # 内部 / 公众两套 Bearer 密钥隔离的 HTTP API
  cli.py           # 命令行：issue-key / serve / rebuild / verify / sign
  __main__.py      # python -m 入口
tests/             # 53 个 unittest 用例
```

## 快速体验

```bash
export BUSY_AREA_SIGNING_SECRET="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
DB=./data/demo.db

# 1) 初始化演示数据与两套密钥
python3 -m service_09251_008 --db $DB demo-init

# 2) 签发三个内部身份（提单人 + 两名复核人）与一个公众渠道密钥
python3 -m service_09251_008 --db $DB issue-key --scope internal --label planner   > k1.json
python3 -m service_09251_008 --db $DB issue-key --scope internal --label reviewer_a > k2.json
python3 -m service_09251_008 --db $DB issue-key --scope internal --label reviewer_b > k3.json
python3 -m service_09251_008 --db $DB issue-key --scope public   --label media      > kp.json
IK1=$(grep -oP '"token": "\K[^"]+' k1.json)
# ... 其余密钥同理提取

# 3) 启动服务（启动即做一次紧急补审期限追踪 = 进程恢复）
python3 -m service_09251_008 --db $DB serve --port 8080
```

常规发布流程（以 `$IK1` 提单、`$IK2`/$IK3` 复核为例）：

```bash
# 内部：生成候选清单（响应含每个指标的窗口值、命中等触发痕迹）
curl -s -X POST http://127.0.0.1:8080/internal/candidates \
  -H "Authorization: Bearer $IK1" -d '{}'
# 两阶段、两不同复核人；提单人不得复核（以 `$IK2` / `$IK3` 为例）
curl -s -X POST .../internal/batches/<id>/review -H "Authorization: Bearer $IK2" \
  -d '{"stage":"first","decision":"approve"}'
curl -s -X POST .../internal/batches/<id>/review -H "Authorization: Bearer $IK3" \
  -d '{"stage":"second","decision":"approve"}'

# 公众渠道只能看到简化结论
curl -s "http://127.0.0.1:8080/public/current?day=2026-09-25" \
  -H "Authorization: Bearer $KP"
# 以及带 HMAC 签名的发布信封
curl -s http://127.0.0.1:8080/public/batches/<id> -H "Authorization: Bearer $KP"
```

紧急升级（先发布后补审）：

```bash
curl -s -X POST http://127.0.0.1:8080/internal/emergency-publish \
  -H "Authorization: Bearer $IK1" -d '{"reason":"突增车流"}'
# => status=emergency，公众立即可见；publish_deadline 默认为发布时刻 +24h
# 期限内由两名复核人补齐 first/second 审批 -> status=published
# 超期未补：后台追踪（及服务重启）自动将批次置为 expired 并撤回其有效区间
```

## 核心语义

| 主题 | 设计 |
| --- | --- |
| 规则版本 | 不可变 `RuleVersion`（指标、观察期天数、三档阈值、任一/全部组合策略）；`POST /internal/rules/{v}/activate` 跨进程加锁切换，旧版本自动退役 |
| 观察期 | 每指标独立窗口天数，窗口随评估日滑动；窗口内读数取均值，窗口外/无数据不触发 |
| 人工例外 | `force_up` / `hold_down` / `exempt`，带生效日期闭区间与原因、操作人；例外作用记录进内部痕迹，不进公开结论 |
| 候选 | 内部条目含 `traces`（窗口起讫、样本数、均值、所达档）与 `triggered_by`；公众视图仅 `area_id/name/level` |
| 双人复核 | 两阶段 `first`→`second`，须两名不同复核人且不得与提单人相同；驳回即终止 |
| 并发/重复 | 审批 `UNIQUE(batch_id, stage, reviewer)` + 发布全程持跨进程 `flock` 与 `BEGIN IMMEDIATE`；并发第二审批只有一人成功，区间只合并一次 |
| 紧急升级 | 立即对公众生效并记录 `publish_deadline`；补审通过转正，驳回立即撤回，超期由 sweep 自动失效并撤回 |
| 进程恢复 | 服务启动先跑一次 deadline sweep；全部状态在 SQLite，崩溃后凭库恢复 |
| 撤销 | 仅关闭该批次名下 `valid_to IS NULL` 的区间；已结束区间与其它批次的区间不受影响 |
| 有效期 | 半开 `[valid_from, valid_to)` 按日组织；每次发布按批次分段，相邻段无缝衔接，跨日查询/历史重建可还原任意一天 |
| 历史重建 | `GET /internal/rebuild?day=&mode=candidate&rule_version=`（按任意规则版本重算候选）或 `mode=snapshot`（还原当日公开版本） |
| 签名 | 公开信封对规范化 JSON 做 HMAC-SHA256；`python -m service_09251_008 verify env.json`，篡改即验签失败 |

## 权限隔离

* `/internal/*` 仅接受 `scope=internal` 的 Bearer 密钥：数据录入、规则、例外、
  候选、审批、发布、撤销、重建、审计。
* `/public/*` 仅接受 `scope=public` 的 Bearer 密钥：当日简化结论与已签名发布信封。
* 密钥作用域与路径不匹配一律 **403**；无凭证/坏凭证 **401**。
  复核人身份取自密钥 label，无法冒用他人名义审批。

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q service_09251_008 tests
```

覆盖：阈值与滑动观察期、规则版本切换（任一/全部）、人工例外三类、
候选痕迹与公开简化、审批顺序/同人限制/提单人回避/**重复审批**、
**并发发布**（双线程竞争第二审批只成功一次）、紧急立即生效/补审/驳回撤回/
**超期自动失效**、**进程恢复**、撤销只及于开放区间、**跨日有效期**
（连续、换段、换级、生效前不可见）、历史规则重算、权限**越界** 401/403、
签名篡改失败，以及 CLI 全命令。

## 运行数据

数据库默认 `./data/busy_area.db`（可用 `--db` 或 `BUSY_AREA_DB` 覆盖），
已在 `.gitignore` 中排除；签名密钥通过 `--secret` 或
`BUSY_AREA_SIGNING_SECRET` 提供，不入库、不写源码目录。
