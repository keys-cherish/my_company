"""Configuration management using pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    bot_token: str = ""
    proxy_url: str = ""  # HTTP代理，如 http://127.0.0.1:7890
    use_uvloop: bool = True
    app_timezone: str = "Asia/Shanghai"
    api_host: str = "0.0.0.0"
    api_port: int = 8090
    # Comma-separated list of allowed chat_ids (group/subchannel) where commands work.
    # Empty means all groups are allowed.
    allowed_chat_ids: str = ""
    # Comma-separated list of allowed chat usernames (without @).
    # Example: "Anyincubation,my_company_group"
    allowed_chat_usernames: str = ""
    # Comma-separated list of allowed topic(thread) IDs in forum supergroups.
    # Example: "18833,20001"
    allowed_topic_thread_ids: str = ""
    # 兼容旧配置：单个论坛话题ID（message_thread_id）。0 表示不限制。
    allowed_topic_thread_id: int = 0
    # 话题命令限制：格式 topic_id:cmd1+cmd2,topic_id2:cmd3
    # 例如 "48629:cp_demon,13434:cp_checkin+cp_start"
    # 该话题只允许指定命令和对应的回调按钮
    topic_command_restrictions: str = ""

    # Database
    database_url: str = "postgresql+asyncpg://mycompany:mycompany@localhost:5432/mycompany"
    postgres_db: str = ""
    postgres_user: str = ""
    postgres_password: str = ""
    db_pool_size: int = 12
    db_max_overflow: int = 16
    db_pool_timeout_seconds: int = 30
    db_pool_recycle_seconds: int = 1800

    # Mini App API
    miniapp_auth_max_age_seconds: int = 300
    miniapp_session_ttl_seconds: int = 1800
    miniapp_preload_ttl_seconds: int = 5
    miniapp_allowed_origins: str = ""

    # Redis
    redis_password: str = ""
    redis_url: str = "redis://localhost:6379/0"
    # 共享积分Redis（可对接 dice_bot / clans_bot）。留空则使用本地 REDIS_URL。
    points_redis_host: str = ""
    points_redis_port: int = 6379
    points_redis_db: int = 0
    points_redis_password: str = ""
    redis_stream_enabled: bool = True
    redis_stream_key: str = "my_company:events"
    redis_stream_maxlen: int = 20000

    # Game constants
    shared_initial_points: int = 20_000  # 跨bot共享积分：首次注册赠送
    initial_points: int = 30_000
    company_creation_cost: int = 15_000
    min_owner_share_pct: int = 30  # owner must hold >= 30%
    valuation_fund_coeff: float = 1.0
    valuation_income_days: int = 30
    daily_operating_cost_pct: float = 0.07
    dividend_pct: float = 0.70
    max_company_funds: int = 10_000_000  # 公司积分上限: 1000万
    max_self_points: int = 1_000_000  # 个人积分上限基础值: 100万
    max_self_points_per_level: int = 50_000  # 每级公司提升个人积分上限: 5万
    max_daily_dividend: int = 200_000  # 每日分红上限: 20万
    max_total_estates: int = 8  # 每家公司最多拥有的地产总数

    # Research
    base_research_cost: int = 800
    base_research_seconds: int = 3600  # 1 hour default

    # Product
    product_create_cost: int = 500
    product_upgrade_cost_base: int = 300
    product_upgrade_income_pct: float = 0.20  # +20% per upgrade
    product_min_investment: int = 500  # 创建产品最低投资额
    product_max_investment: int = 200_000  # 创建产品最高投资额
    product_ai_income_rate: float = 0.08  # 日收入 = 投资额 * 品质/100 * 此系数

    # AI iterate (AI研发迭代)
    ai_rd_daily_limit: int = 5  # per company/day
    ai_rd_product_cooldown_seconds: int = 4 * 3600  # cooldown per product
    ai_rd_company_cooldown_seconds: int = 3600  # global cooldown per company

    # Roadshow
    roadshow_cost: int = 200
    roadshow_daily_once: bool = True
    roadshow_cooldown_seconds: int = 7200  # legacy: used only when roadshow_daily_once = false
    roadshow_satire_chance: float = 0.18
    roadshow_satire_penalty_rate: float = 0.20

    # Reputation buff
    max_reputation_buff_pct: float = 0.50  # max 50% revenue buff
    reputation_per_research: int = 5
    reputation_per_dividend: int = 3

    # Settlement
    settlement_hour: int = 0  # midnight Beijing time
    settlement_minute: int = 0
    backup_enabled: bool = True
    backup_interval_minutes: int = 180
    backup_keep_files: int = 72
    backup_notify_super_admin: bool = True

    # WebDAV remote backup
    webdav_backup_url: str = ""
    webdav_backup_username: str = ""
    webdav_backup_password: str = ""

    # Tax system (统一税率体系)
    # 所有税费使用统一税率：日结算税、分红税、交易税等
    tax_rate: float = 0.10  # 统一税率 10%
    social_insurance_rate: float = 0.02

    # Employee system
    base_employee_limit: int = 10  # initial company employees / minimum limit
    employee_limit_per_level: int = 3  # legacy linear growth term (kept for compatibility)
    max_employee_limit: int = 500  # hard cap for any company
    employee_limit_growth_exponent: float = 2.2  # level->employee limit curve
    employee_effective_cap_for_progress: int = 200  # soft cap to avoid revenue inflation
    employee_salary_base: int = 50  # base daily salary per employee

    # Random events
    event_chance: float = 0.35  # 35% chance per company per day

    # Daily check-in (每日打卡)
    checkin_base_reward: int = 100  # Day-1 reward
    checkin_streak_rewards: str = "100,150,200,300,400,500,1000"  # Day 1-7 rewards, comma-separated
    checkin_streak_cycle: int = 7  # streak resets after this many days (宝箱循环)
    checkin_streak_bonus_pool: str = "1500,2500,4000,6000,15000"  # 7-day chest random pick

    # Red packet (公司红包)
    redpacket_min_amount: int = 100  # minimum total amount
    redpacket_max_amount: int = 100_000  # maximum total amount
    redpacket_max_count: int = 50  # max number of splits
    redpacket_expire_seconds: int = 86400  # 24h expiry
    redpacket_lucky_bonus_pct: float = 0.10  # 手气最佳额外10%奖金(系统出)

    # 恶魔轮盘赌
    roulette_min_bet: int = 1_000
    roulette_max_bet_pct: float = 1.00  # 最高赌注=公司资金的100%
    roulette_cooldown_seconds: int = 120  # 冷却2分钟
    roulette_room_ttl_seconds: int = 600  # 房间10分钟过期

    # 悬赏令
    bounty_reputation_cost: int = 30
    bounty_ttl_seconds: int = 86400
    bounty_cooldown_seconds: int = 7200

    # AI API（AI研发评审）
    ai_enabled: bool = True
    ai_provider: str = "openai_compatible"  # openai_compatible / deepseek / custom
    ai_api_key: str = ""
    ai_api_base_url: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4o-mini"
    ai_timeout_seconds: int = 30
    ai_max_retries: int = 2
    ai_retry_backoff_seconds: float = 1.5
    ai_temperature: float = 0.2
    ai_top_p: float = 1.0
    ai_max_tokens: int = 500
    ai_system_prompt: str = ""
    ai_chat_system_prompt: str = ""
    # 额外请求头，JSON格式；例如 {"X-Api-Version":"2024-01-01"}
    ai_extra_headers_json: str = ""

    # 管理员（仅超级管理员，通过 TG ID 判断）
    super_admin_tg_id: int = 0  # 兼容旧配置：单个超级管理员TG ID
    super_admin_tg_ids: str = ""  # 逗号分隔的超级管理员TG ID列表

    @property
    def admin_tg_id_set(self) -> set[int]:
        return self.super_admin_tg_id_set

    @property
    def super_admin_tg_id_set(self) -> set[int]:
        ids: set[int] = set()
        if self.super_admin_tg_ids.strip():
            ids.update({int(x.strip()) for x in self.super_admin_tg_ids.split(",") if x.strip()})
        if self.super_admin_tg_id > 0:
            ids.add(self.super_admin_tg_id)
        return ids

    @property
    def allowed_chat_id_set(self) -> set[int]:
        if not self.allowed_chat_ids.strip():
            return set()
        return {int(x.strip()) for x in self.allowed_chat_ids.split(",") if x.strip()}

    @property
    def allowed_chat_username_set(self) -> set[str]:
        if not self.allowed_chat_usernames.strip():
            return set()
        return {
            x.strip().lstrip("@").lower()
            for x in self.allowed_chat_usernames.split(",")
            if x.strip()
        }

    @property
    def allowed_topic_thread_id_set(self) -> set[int]:
        ids: set[int] = set()
        if self.allowed_topic_thread_ids.strip():
            ids.update({int(x.strip()) for x in self.allowed_topic_thread_ids.split(",") if x.strip()})
        if self.allowed_topic_thread_id > 0:
            ids.add(self.allowed_topic_thread_id)
        return ids

    @property
    def topic_command_restriction_map(self) -> dict[int, set[str]]:
        """Parse 'topic_id:cmd1+cmd2,topic_id2:cmd3' into {topic_id: {cmd1, cmd2}}."""
        raw = self.topic_command_restrictions.strip()
        if not raw:
            return {}
        result: dict[int, set[str]] = {}
        for entry in raw.split(","):
            entry = entry.strip()
            if ":" not in entry:
                continue
            tid_str, cmds_str = entry.split(":", 1)
            tid = int(tid_str.strip())
            cmds = {c.strip().lstrip("/") for c in cmds_str.split("+") if c.strip()}
            result[tid] = cmds
        return result


settings = Settings()
