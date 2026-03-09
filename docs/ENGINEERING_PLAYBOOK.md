# Engineering Playbook (Agile + Maintainability)

本文件定义项目的日常开发规范。目标是：功能持续迭代，同时代码长期可维护。

## 1. Agile Working Agreement

1. Sprint 周期：1 周一个迭代，按“计划 -> 开发 -> 验收 -> 复盘”执行。
2. 需求拆分：每个任务应可在 1 天内完成并可独立测试。
3. Definition of Done（DoD）：
   - 代码通过本地测试（至少相关测试）。
   - 新增/修改逻辑有对应测试或回归测试。
   - 文档（README 或 docs）同步更新。
   - 关键异常路径有可读日志，不吞异常。
4. 回滚原则：大改拆成多小步提交，确保每一步可回滚。

## 2. Module Boundaries

1. `handlers/`：仅处理 Telegram 交互、参数解析、权限校验、组装回复。
2. `services/`：纯业务逻辑，不依赖 Telegram 消息对象。
3. `db/`：模型与数据库会话，不承载业务规则。
4. `api/`：Mini App API，仅负责认证、会话、数据投影，不直接堆积业务规则。
5. `utils/`：无状态工具函数，避免反向依赖业务模块。

## 3. Naming Rules

1. 函数名使用动词短语：`create_company`, `run_regulation_audit`。
2. 布尔变量用 `is_`/`has_`/`can_` 前缀。
3. 常量全大写且语义完整：`LEGAL_WORK_HOURS`，避免 `N`, `X`。
4. 避免同义混用（例如“资金/积分”），用户可见单位统一为“积分”。

## 4. Commenting Rules

1. 默认不写“翻译代码”的注释。
2. 仅在以下情况写注释：
   - 业务规则不直观（例如监管抽检/处罚逻辑）。
   - 兼容历史行为（legacy 字段或协议）。
   - 性能相关取舍（小机保护、并发限制）。
3. 注释必须说明“为什么”，不是“做了什么”。

## 5. Error Handling Rules

1. 禁止无差别 `except Exception: pass`（除非明确可忽略并写明原因）。
2. 对外回复友好错误，对内日志保留上下文。
3. 网络调用必须有超时和重试上限。

## 6. Testing Strategy

1. 新增业务规则必须有测试：
   - 正常路径
   - 边界条件
   - 失败/回滚路径
2. 优先单元测试 `services/`，必要时再补 `handlers/` 行为测试。
3. 回归缺陷必须先写失败测试，再修复。

## 7. Small Server Guardrails

1. 单实例优先：固定使用 `polling`，同一 token 仅保留一个 bot 进程。
2. DB 连接池保守配置，避免把连接池开到打满 CPU/内存。
3. 所有高频接口需有冷却/限流（例如 AI 迭代、商战、AI 对话）。
4. 定时任务避免并发重入，耗时任务要可观测（日志 + 指标）。

## 8. Mini App Evolution Rules

1. Mini App 仅做“展示层 + 轻操作”，核心规则仍在 `services/`。
2. API 返回“投影数据”，禁止把业务决策复制到前端。
3. 先做只读看板（preload + refresh），再逐步开放写操作。
4. 任意写操作都必须复用现有服务层逻辑，避免双实现。
