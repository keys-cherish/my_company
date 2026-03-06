"""Dividend distribution system."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.models import Company
from services.company_service import add_funds
from services.shareholder_service import get_shareholders
from services.user_service import add_reputation, add_traffic, add_self_points


async def distribute_dividends(
    session: AsyncSession,
    company: Company,
    profit: int,
) -> list[tuple[int, int]]:
    """Distribute a profit-based dividend pool and return paid records.

    The company dividend outflow is deducted first to avoid money inflation.
    Any failed individual payout will be refunded back to company funds.
    """
    if profit <= 0:
        return []

    dividend_pool = int(profit * settings.dividend_pct)
    if dividend_pool <= 0:
        return []

    # Cap daily auto-dividend at max_daily_dividend
    dividend_pool = min(dividend_pool, settings.max_daily_dividend)

    shareholders = await get_shareholders(session, company.id)
    planned: list[tuple[int, int]] = []

    for sh in shareholders:
        share_amount = int(dividend_pool * sh.shares / 100)
        if share_amount > 0:
            planned.append((sh.user_id, share_amount))

    if not planned:
        return []

    total_planned = sum(amount for _, amount in planned)
    deducted = await add_funds(session, company.id, -total_planned)
    if not deducted:
        return []

    distributions: list[tuple[int, int]] = []
    failed_total = 0
    for user_id, share_amount in planned:
        ok = await add_traffic(session, user_id, share_amount)
        if not ok:
            failed_total += share_amount
            continue

        distributions.append((user_id, share_amount))

        # Reputation gain from receiving dividends
        rep = settings.reputation_per_dividend
        await add_reputation(session, user_id, rep)

        # Points for receiving dividends
        await add_self_points(user_id, 2, session=session)

    if failed_total > 0:
        await add_funds(session, company.id, failed_total)

    return distributions
