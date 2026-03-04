# 商业帝国 - Telegram 公司经营游戏机器人

基于 Telegram 的多人公司经营模拟游戏。玩家通过 **科研 → 研发树 → 产品 → 利润** 的路径经营虚拟公司，包含股东系统、每日结算、地产投资、分红、AI研发评审、交易所等玩法。

## 技术栈

- **语言**: Python 3.11+
- **Bot框架**: aiogram 3.x（异步Telegram框架）
- **数据库**: PostgreSQL + asyncpg（通过SQLAlchemy 2.0 async ORM，支持高并发）
- **缓存**: Redis（热数据/分布式锁/排行榜/冷却计时/管理员认证/道具Buff）
- **定时任务**: APScheduler（每日结算 + 自动备份）
- **配置**: pydantic-settings（类型安全）
- **部署**: Docker Compose（推荐）/ 本地 Python 运行

## 快速开始（Docker，推荐）

```bash
# 1. 克隆项目
git clone https://github.com/keys-cherish/my_company.git
cd my_company

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填入 BOT_TOKEN、管理员ID/超管ID、群组和话题限制等配置

# 3. 启动（bot + postgres + redis）
docker compose up -d --build

# 4. 查看状态
docker compose ps
docker compose logs -f bot
```

## 快速开始（本地 Python）

```bash
# 1. 克隆项目
git clone https://github.com/keys-cherish/my_company.git
cd my_company

# 2. 安装依赖（需要先安装 uv）
uv sync

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env 填入 BOT_TOKEN、数据库连接、管理员ID等配置

# 4. 确保 PostgreSQL 和 Redis 已启动
# PostgreSQL: 创建数据库和用户
# Redis: redis-server &

# 5. 启动机器人（自动建表）
uv run python bot.py
```

## 核心玩法

| 系统 | 说明 |
|------|------|
| 公司系统 | 8种公司类型（科技/金融/传媒/制造/地产/生物/游戏/咨询），各有专属Buff |
| 股东系统 | 注资获取股份，老板最低持股保护，按股份分红 |
| 科研系统 | 10级研发树，前置解锁，完成后解锁新产品 |
| 产品系统 | 创建/升级/下架产品，每日产生收入 |
| AI研发 | 提交产品方案 → AI评分 → 永久提升产品收入(1-100%) |
| 路演系统 | 消耗积分路演，随机获得资源/声望/积分 |
| 合作系统 | 公司间合作提供营收加成 |
| 地产系统 | 购买地产获取稳定被动收入 |
| 广告系统 | 4档广告方案，临时提升营收 |
| 交易所 | 积分兑换流量，道具商城，黑市特惠 |
| 员工系统 | 招聘/裁员，薪资/社保成本 |
| 经营策略 | 工时/办公/培训/保险/文化/道德/监管，动态影响营收与成本 |
| 税务系统 | 每日营收纳税 |
| 随机事件 | 员工离职/退休/市场波动/PR危机等20+事件 |
| 积分系统 | 多途径获取荣誉点，可兑换积分 |
| 每日结算 | 自动计算收入/分红/生成日报 |

## 命令

| 命令 | 范围 | 说明 |
|------|------|------|
| `/company_start` | 私聊+群组 | 注册/查看个人面板 |
| `/company` | 私聊+群组 | 查看/管理公司 |
| `/company_admin <密钥>` | 私聊 | 管理员认证（需配置ID+密钥） |
| 其他操作 | 私聊+群组 | 通过Inline键盘菜单操作 |

## 配置

所有游戏参数均可通过 `.env` 文件或管理员面板实时调整，详见 `.env.example`。

常用配置项：
- `BOT_TOKEN`：机器人 token
- `DATABASE_URL`：数据库连接（生产建议 PostgreSQL）
- `DB_POOL_SIZE` / `DB_MAX_OVERFLOW`：数据库连接池容量（默认 `12/16`，小机更稳）
- `DB_POOL_TIMEOUT_SECONDS` / `DB_POOL_RECYCLE_SECONDS`：连接池获取超时与连接回收时间
- `REDIS_URL`：Redis 连接
- `RUN_MODE`：运行模式（`polling` / `webhook`）
- `USE_UVLOOP`：是否启用 uvloop
- `APP_TIMEZONE`：时区（默认 `Asia/Shanghai`，北京时间）
- `WEBHOOK_BASE_URL` / `WEBHOOK_PATH` / `WEBHOOK_PORT`：Webhook 模式配置
- `REDIS_STREAM_ENABLED` / `REDIS_STREAM_KEY`：Redis Stream 事件通道配置
- `ADMIN_TG_IDS`：管理员 TG ID 列表（逗号分隔）
- `SUPER_ADMIN_TG_ID` / `SUPER_ADMIN_TG_IDS`：超级管理员（支持单个或多个）
- `ALLOWED_CHAT_IDS`：允许的群组 ID（逗号分隔）
- `ALLOWED_CHAT_USERNAMES`：允许的群组用户名（可选，逗号分隔）
- `ALLOWED_TOPIC_THREAD_IDS`：允许的话题 ID 列表（可选，逗号分隔）
- `ALLOWED_TOPIC_THREAD_ID`：单话题兼容配置（旧版，保留可用）
- `BACKUP_ENABLED`：是否启用自动备份
- `BACKUP_INTERVAL_MINUTES`：兼容配置项（当前固定每 3 小时整点备份）
- `BACKUP_KEEP_FILES`：本地保留的备份文件数量
- `BACKUP_NOTIFY_SUPER_ADMIN`：备份结果是否私聊通知超管
- `AI_ENABLED`：是否启用真实 AI 评审（关闭时走本地严格评分）
- `AI_API_BASE_URL` / `AI_API_KEY` / `AI_MODEL`：AI服务地址、密钥与模型
- `AI_TIMEOUT_SECONDS` / `AI_MAX_RETRIES` / `AI_RETRY_BACKOFF_SECONDS`：超时与重试策略
- `AI_TEMPERATURE` / `AI_TOP_P` / `AI_MAX_TOKENS`：模型采样与输出长度
- `AI_SYSTEM_PROMPT`：可覆盖默认评审系统提示词
- `AI_EXTRA_HEADERS_JSON`：可选额外请求头（JSON）

备份说明：
- 固定按北京时间每 3 小时整点执行（00/03/06/09/12/15/18/21）
- 备份文件写入项目根目录，文件名形如 `my_company_backup_YYYYMMDDTHHMMSS+0800.json.gz`
- 该备份是 `my_company` 项目独立文件，不使用 `dice_bot` 的 `backup.db`

AI配置说明：
- 所有 AI 密钥仅写入本地 `.env`，不要提交到 Git。
- `AI_API_BASE_URL` 支持 OpenAI 兼容接口，默认使用 `.../v1/chat/completions`。
- 若供应商需要额外请求头，可在 `AI_EXTRA_HEADERS_JSON` 中配置。

## 项目结构

```
my_company/
├── bot.py                      # 入口：启动bot + 注册router + 中间件
├── config.py                   # pydantic-settings 配置加载
├── commands.py                 # 命令常量定义
├── pyproject.toml              # uv 项目配置
├── db/
│   ├── engine.py               # 数据库连接池 + async_session
│   └── models.py               # SQLAlchemy ORM 模型
├── cache/
│   └── redis_client.py         # Redis 连接 + 排行榜 + Lua 脚本
├── handlers/                   # Telegram 交互层（薄handler，逻辑在services）
│   ├── common.py               # 公共过滤器、中间件、管理员认证
│   ├── company.py              # 公司CRUD/导航/改名/注销/升级
│   ├── company_helpers.py      # 公司共享函数（render_company_detail等）
│   ├── company_ops.py          # 经营策略（工时/办公/培训/保险/文化/道德）
│   ├── company_employees.py    # 员工管理（招聘/裁员/成员查看）
│   ├── shareholder.py          # 股东注资
│   ├── research.py             # 科研/研发树
│   ├── product.py              # 产品管理
│   ├── realestate.py           # 地产投资
│   ├── ad.py                   # 广告系统
│   ├── ai_rd.py                # AI 研发评审
│   ├── ai_chat.py              # AI 聊天/意图识别
│   ├── exchange.py             # 交易所/商店/黑市
│   ├── total_war.py            # 全面商战
│   ├── slot_machine.py         # 老虎机游戏
│   ├── admin.py                # 管理员面板/Buff一览
│   ├── start.py                # 注册/个人面板/排行榜
│   └── ...
├── services/                   # 业务逻辑层（单一真源）
│   ├── settlement_service.py   # 每日结算核心
│   ├── company_service.py      # 公司CRUD/资金/升级
│   ├── operations_service.py   # 经营策略/监管审计
│   ├── product_service.py      # 产品创建/升级
│   ├── research_service.py     # 科研进度/完成
│   ├── shop_service.py         # 商店/黑市/buff管理
│   ├── slot_service.py         # 老虎机业务逻辑
│   ├── battle_service.py       # 商战/全面商战
│   ├── ai_chat_service.py      # AI 对话/工具调用
│   ├── ai_rd_service.py        # AI 研发评分
│   └── ...
├── keyboards/
│   └── menus.py                # Inline 键盘布局
├── scheduler/
│   └── daily_settlement.py     # APScheduler 定时结算
├── game_data/                  # 静态游戏数据（JSON）
│   ├── company_types.json      # 8种公司类型
│   ├── company_levels.json     # 公司等级定义
│   ├── tech_tree.json          # 研发树
│   ├── products.json           # 产品模板
│   ├── shop_items.json         # 商店道具
│   ├── buildings.json          # 地产数据
│   └── weekly_quests.json      # 周任务
├── api/                        # Mini App REST API（Litestar）
│   ├── app.py                  # ASGI 应用
│   ├── routes.py               # /api/miniapp/auth + /api/miniapp/preload
│   ├── security.py             # Telegram initData 签名验证
│   └── preload.py              # 用户数据快照
├── miniapp/                    # Telegram Mini App 前端（Vue 3）
│   ├── src/                    # Vue + TypeScript + Vant
│   └── package.json            # pnpm 依赖
├── utils/                      # 工具函数
│   ├── formatters.py           # 货币/数字格式化
│   ├── validators.py           # 名称校验
│   ├── concurrency.py          # Redis 分布式锁
│   ├── panel_owner.py          # 面板归属追踪
│   └── timezone.py             # 时区转换
└── tests/                      # 单元测试（SQLite + FakeRedis）
    ├── helpers/                 # 测试基类
    └── test_*.py               # 10+ 测试文件
```

## 工程规范（敏捷 + 可维护）

项目采用“短迭代 + 小步提交 + 强回归”策略，详细规范见：

- `docs/ENGINEERING_PLAYBOOK.md`

核心要求：

1. 单个需求拆分为可独立测试的小任务（建议 1 天内完成）。
2. 代码变更必须附带测试或更新现有测试。
3. 所有新业务规则必须在 `services/` 层实现，`handlers/` 只做交互编排。
4. 文案、配置、README 必须同步更新。

常用测试命令：

```bash
# 仅跑核心回归
uv run pytest -q tests/test_settlement_logic.py tests/test_company_logic.py

# 跑新增监管与分红回归
uv run pytest -q tests/test_regulation_audit.py tests/test_dividend_distribution.py
```

## 小鸡服务器部署建议（防崩）

低配单机（1C/2G）建议使用以下策略：

1. **单实例**：`RUN_MODE=webhook`，避免 polling 长连接开销。
2. **API 单进程**：Mini App API 保持 `workers=1`（当前已如此）。
3. **保守连接池**：建议 `DB_POOL_SIZE=4~8`，`DB_MAX_OVERFLOW=4~8`。
4. **缓存优先**：高频读取走 Redis（预加载/冷却/排行榜）。
5. **限流必须开启**：AI 对话、AI 迭代、商战都要有冷却与配额。
6. **避免重计算**：Mini App 读接口仅返回投影数据，不做复杂统计。
7. **观测最小闭环**：保留错误日志 + 每日结算日志 + 备份通知。

参考参数（小机）：

```env
RUN_MODE=webhook
DB_POOL_SIZE=6
DB_MAX_OVERFLOW=6
DB_POOL_TIMEOUT_SECONDS=30
DB_POOL_RECYCLE_SECONDS=1200
REDIS_STREAM_MAXLEN=5000
AI_TIMEOUT_SECONDS=20
AI_MAX_RETRIES=1
AI_RD_DAILY_LIMIT=3
AI_RD_PRODUCT_COOLDOWN_SECONDS=21600
AI_RD_COMPANY_COOLDOWN_SECONDS=7200
```

## Mini App 接入路线（先方案）

当前已具备基础 API（认证 + preload），推荐按三阶段推进，避免一次性大改：

1. **阶段1：只读看板**
   - 保持文字机器人为主，Mini App 仅展示：公司状态、结算记录、产品/科研进度。
   - 所有写操作继续走 Bot，先把读性能和鉴权打稳。

2. **阶段2：低风险写操作**
   - 开放幂等且低风险操作（例如切换展示、刷新、查询筛选）。
   - 对写接口统一加会话校验、速率限制、审计日志。

3. **阶段3：核心经营操作**
   - 再逐步开放“有成本的操作”（研发、培训、购买）。
   - 这些接口必须复用现有 `services/` 逻辑，禁止前后端双实现。

设计原则：

1. Mini App 是 UI 层，不是业务规则层。
2. 以 `services/` 为单一业务真源（single source of truth）。
3. 先解决一致性和风控，再追求界面丰富度。
