"""Settlement data structures for breakdown tracking."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class IncomeBreakdown:
    """收入明细。

    Attributes:
        product_income: 产品基础收入
        level_bonus: 等级加成
        cooperation_bonus: 合作加成
        realestate_income: 地产收入
        reputation_buff: 声望加成
        ad_boost: 广告加成
        shop_buff: 商店Buff
        totalwar_buff: 商战Buff
        type_bonus: 公司类型加成
        employee_income: 员工产出
    """
    product_income: int = 0
    level_bonus: int = 0
    cooperation_bonus: int = 0
    realestate_income: int = 0
    reputation_buff: int = 0
    ad_boost: int = 0
    shop_buff: int = 0
    totalwar_buff: int = 0
    type_bonus: int = 0
    employee_income: int = 0
    immoral_buff: int = 0

    @property
    def total(self) -> int:
        """计算总收入。"""
        return sum([
            self.product_income,
            self.level_bonus,
            self.cooperation_bonus,
            self.realestate_income,
            self.reputation_buff,
            self.ad_boost,
            self.shop_buff,
            self.totalwar_buff,
            self.type_bonus,
            self.employee_income,
            self.immoral_buff,
        ])


@dataclass
class PenaltyBreakdown:
    """惩罚明细。

    Attributes:
        rename_penalty: 改名惩罚
        battle_debuff: 战斗减益
        roadshow_penalty: 路演惩罚
    """
    rename_penalty: int = 0
    battle_debuff: int = 0
    roadshow_penalty: int = 0

    @property
    def total(self) -> int:
        """计算总惩罚。"""
        return self.rename_penalty + self.battle_debuff + self.roadshow_penalty


@dataclass
class CostBreakdown:
    """成本明细。

    Attributes:
        tax: 税收
        salary: 工资
        social_insurance: 社保
        base_operating: 基础运营
        office_cost: 办公成本
        training_cost: 培训成本
        regulation_cost: 监管成本
        insurance_cost: 保险成本
        work_cost_adjust: 工时调整
        culture_maintenance: 文化维护
        regulation_fine: 监管罚款
        type_cost_modifier: 类型成本修正
    """
    tax: int = 0
    salary: int = 0
    social_insurance: int = 0
    base_operating: int = 0
    office_cost: int = 0
    training_cost: int = 0
    regulation_cost: int = 0
    insurance_cost: int = 0
    work_cost_adjust: int = 0
    culture_maintenance: int = 0
    regulation_fine: int = 0
    type_cost_modifier: int = 0

    @property
    def total(self) -> int:
        """计算总成本。"""
        return sum([
            self.tax,
            self.salary,
            self.social_insurance,
            self.base_operating,
            self.office_cost,
            self.training_cost,
            self.regulation_cost,
            self.insurance_cost,
            self.work_cost_adjust,
            self.culture_maintenance,
            self.regulation_fine,
            self.type_cost_modifier,
        ])


@dataclass
class SettlementResult:
    """结算最终结果。

    Attributes:
        income: 收入明细
        penalties: 惩罚明细
        costs: 成本明细
        gross_income: 毛收入（应用惩罚后）
        net_income: 净收入（扣成本前）
        profit: 利润（扣成本后）
        events: 事件消息列表
    """
    income: IncomeBreakdown
    penalties: PenaltyBreakdown
    costs: CostBreakdown
    gross_income: int
    net_income: int
    profit: int
    events: list[str] = field(default_factory=list)
