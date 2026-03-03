"""Configuration management using pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Telegram
    bot_token: str = ""
    proxy_url: str = ""  # HTTP代理，如 http://127.0.0.1:7890
    run_mode: str = "webhook"  # polling / webhook
    use_uvloop: bool = True
    app_timezone: str = "Asia/Shanghai"
    webhook_base_url: str = ""  # 例如: https://example.com
    webhook_path: str = "/telegram/webhook"
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8080
    webhook_secret_token: str = ""
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
    redis_stream_enabled: bool = True
    redis_stream_key: str = "my_company:events"
    redis_stream_maxlen: int = 20000

    # Game constants
    initial_traffic: int = 100000
    company_creation_cost: int = 55000
    min_owner_share_pct: int = 30  # owner must hold >= 30%
    valuation_fund_coeff: float = 1.0
    valuation_income_days: int = 30
    daily_operating_cost_pct: float = 0.07
    dividend_pct: float = 0.70

    # Research
    base_research_cost: int = 2500
    base_research_seconds: int = 3600  # 1 hour default

    # Product
    product_create_cost: int = 1500
    product_upgrade_cost_base: int = 800
    product_upgrade_income_pct: float = 0.20  # +20% per upgrade

    # AI iterate (AI研发迭代)
    ai_rd_daily_limit: int = 5  # per company/day
    ai_rd_product_cooldown_seconds: int = 4 * 3600  # cooldown per product
    ai_rd_company_cooldown_seconds: int = 3600  # global cooldown per company

    # Roadshow
    roadshow_cost: int = 700
    roadshow_daily_once: bool = True
    roadshow_cooldown_seconds: int = 7200  # legacy: used only when roadshow_daily_once = false
    roadshow_satire_chance: float = 0.18
    roadshow_satire_penalty_rate: float = 0.20

    # Reputation buff
    max_reputation_buff_pct: float = 0.50  # max 50% revenue buff
    reputation_per_research: int = 5
    reputation_per_cooperation: int = 10
    reputation_per_dividend: int = 3

    # Settlement
    settlement_hour: int = 0  # midnight Beijing time
    settlement_minute: int = 0
    backup_enabled: bool = True
    backup_interval_minutes: int = 180
    backup_keep_files: int = 72
    backup_notify_super_admin: bool = True

    # Tax system (统一税率体系)
    # 所有税费使用统一税率：日结算税、分红税、交易税等
    tax_rate: float = 0.10  # 统一税率 10%
    social_insurance_rate: float = 0.02

    # Employee system
    base_employee_limit: int = 10  # initial company employees / minimum limit
    employee_limit_per_level: int = 3  # legacy linear growth term (kept for compatibility)
    max_employee_limit: int = 10_000  # hard cap for any company
    employee_limit_growth_exponent: float = 2.2  # level->employee limit curve
    employee_effective_cap_for_progress: int = 600  # soft cap to avoid revenue inflation
    employee_salary_base: int = 80  # base daily salary per employee
    employee_base_output: int = 100  # base daily income per employee (人均基础产出)
    employee_efficiency_rate: float = 0.003  # product income boost per sqrt(employee) (效率加成系数)

    # Random events
    event_chance: float = 0.35  # 35% chance per company per day

    # Daily check-in (每日打卡)
    checkin_base_reward: int = 300  # Day-1 reward
    checkin_streak_rewards: str = "300,450,600,800,1000,1500,3000"  # Day 1-7 rewards, comma-separated
    checkin_streak_cycle: int = 7  # streak resets after this many days (宝箱循环)
    checkin_streak_bonus_pool: str = "5000,8000,12000,20000,50000"  # 7-day chest random pick

    # Red packet (公司红包)
    redpacket_min_amount: int = 500  # minimum total amount
    redpacket_max_amount: int = 500_000  # maximum total amount
    redpacket_max_count: int = 50  # max number of splits
    redpacket_expire_seconds: int = 86400  # 24h expiry
    redpacket_lucky_bonus_pct: float = 0.10  # 手气最佳额外10%奖金(系统出)

    # 积分兑换真实流量 (个人积分 → MB/GB流量)
    # 满级公司(Lv.10)每日营收约30万+，按比例计算日上限不超过5TB
    traffic_exchange_rate: int = 100  # 100积分 = 1MB
    traffic_exchange_api_url: str = ""  # 外部流量发放接口URL，空=未接入
    traffic_exchange_api_key: str = ""  # 外部接口密钥
    traffic_exchange_daily_limit_mb: int = 5_000_000  # 每人每日上限 5TB = 5,000,000MB

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
    ai_image_model: str = "grok-imagine-1.0-edit"  # 图片生成模型
    # 额外请求头，JSON格式；例如 {"X-Api-Version":"2024-01-01"}
    ai_extra_headers_json: str = ""

    # 流量来源接口（预置参数，后续接入外部API）
    traffic_api_url: str = ""  # 外部流量接口URL
    traffic_api_key: str = ""  # 外部接口认证密钥
    traffic_total_pool: int = 10_000_000  # 全局流量池总量
    traffic_daily_distribution: int = 100_000  # 每日可分配流量

    # 管理员
    super_admin_tg_id: int = 0  # 兼容旧配置：单个超级管理员TG ID（高危命令）
    super_admin_tg_ids: str = ""  # 新配置：逗号分隔的超级管理员TG ID列表
    admin_tg_ids: str = ""  # 逗号分隔的管理员TG ID列表
    admin_secret_key: str = ""  # 管理员认证密钥

    @property
    def admin_tg_id_set(self) -> set[int]:
        if not self.admin_tg_ids.strip():
            return set()
        return {int(x.strip()) for x in self.admin_tg_ids.split(",") if x.strip()}

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


settings = Settings()
