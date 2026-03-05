"""Tests for newly added roulette items."""

from services.roulette_service import (
    DEVIL_TG_ID,
    GameState,
    LATE_ROUND_ITEM_KEYS,
    _item_pool_for_round,
    _use_item,
)


def _build_state(*, user_items: list[str], enemy_items: list[str], shells: list[bool]) -> GameState:
    return GameState(
        room_id="room_new_items",
        phase="playing",
        players=[
            {
                "tg_id": 1001,
                "company_id": 1,
                "name": "玩家A",
                "hp": 2,
                "max_hp": 3,
                "items": list(user_items),
                "is_devil": False,
                "alive": True,
                "saw_active": False,
                "known_shell": None,
            },
            {
                "tg_id": DEVIL_TG_ID,
                "company_id": 0,
                "name": "魔鬼",
                "hp": 2,
                "max_hp": 3,
                "items": list(enemy_items),
                "is_devil": True,
                "alive": True,
                "saw_active": False,
                "known_shell": None,
            },
        ],
        current_round=2,
        shells=list(shells),
        shell_index=0,
        turn_order=[1001, DEVIL_TG_ID],
        turn_index=0,
        live_count=sum(1 for s in shells if s),
        blank_count=sum(1 for s in shells if not s),
    )


def test_new_items_only_appear_from_round_three():
    early_pool = _item_pool_for_round(1)
    late_pool = _item_pool_for_round(2)

    assert all(item not in early_pool for item in LATE_ROUND_ITEM_KEYS)
    assert all(item in late_pool for item in LATE_ROUND_ITEM_KEYS)


def test_inverter_flips_current_shell():
    state = _build_state(user_items=["inverter"], enemy_items=[], shells=[True, False, True])

    msgs = _use_item(state, 1001, "inverter")

    assert state.shells[0] is False
    assert state.live_count == 1
    assert state.blank_count == 2
    assert state.players[0]["known_shell"] == "blank"
    assert any("当前子弹变为空弹" in m for m in msgs)


def test_phone_predicts_specified_position():
    state = _build_state(user_items=["phone"], enemy_items=[], shells=[False, True, False])

    msgs = _use_item(state, 1001, "phone", target_tg_id=2)

    assert any("第2发" in m and "实弹" in m for m in msgs)
    assert state.players[0]["known_shell"] is None


def test_adrenaline_steals_and_immediately_uses_item():
    state = _build_state(user_items=["adrenaline"], enemy_items=["saw"], shells=[True, False])

    msgs = _use_item(state, 1001, "adrenaline")

    assert state.players[1]["items"] == []
    assert state.players[0]["saw_active"] is True
    assert "adrenaline" not in state.players[0]["items"]
    assert "saw" not in state.players[0]["items"]
    text = "\n".join(msgs)
    assert "偷到手锯并立刻使用" in text
    assert "装上手锯" in text
