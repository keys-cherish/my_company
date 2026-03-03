"""Number, currency, and quota formatting helpers."""

from __future__ import annotations

CURRENCY_NAME = "积分"


def fmt_currency(amount: int) -> str:
    """Format in-game currency amount."""
    return f"{amount:,} {CURRENCY_NAME}"


def fmt_traffic(amount: int) -> str:
    """Format user/company credits (legacy name kept for compatibility)."""
    return fmt_currency(amount)


def fmt_quota(amount: int) -> str:
    """Format quota/reserve credits."""
    return fmt_currency(amount)


def fmt_real_traffic(mb: int) -> str:
    """Format real traffic in MB/GB/TB."""
    if mb >= 1024 * 1024:
        return f"{mb / 1024 / 1024:.1f}TB"
    if mb >= 1024:
        return f"{mb / 1024:.1f}GB"
    return f"{mb}MB"


def fmt_pct(value: float) -> str:
    """Format a 0-100 float as percentage string."""
    return f"{value:.2f}%"


def fmt_shares(shares: float) -> str:
    return f"{shares:.2f}%"


def fmt_reputation_buff(reputation: int) -> str:
    """Calculate and display revenue buff from reputation.

    Buff = min(reputation * 0.1%, max_buff). Non-stackable.
    """
    from config import settings

    buff = min(reputation * 0.001, settings.max_reputation_buff_pct)
    return f"+{buff * 100:.1f}%营收"


def reputation_buff_multiplier(reputation: int) -> float:
    """Return the multiplier (e.g. 1.15 for +15%)."""
    from config import settings

    buff = min(reputation * 0.001, settings.max_reputation_buff_pct)
    return 1.0 + buff


def fmt_duration(seconds: int) -> str:
    """Format seconds into human-readable duration (largest unit first)."""
    if seconds <= 0:
        return "0秒"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分钟")
    if seconds and not days:  # skip seconds when duration >= 1 day
        parts.append(f"{seconds}秒")
    return "".join(parts) if parts else "0秒"


def compact_number(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)
