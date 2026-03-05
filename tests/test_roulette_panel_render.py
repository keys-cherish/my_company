"""Tests for roulette panel rendering."""

from services.roulette_service import DEVIL_TG_ID, GameState, render_game_panel


def test_render_panel_mentions_current_human_turn():
    state = GameState(
        room_id="room_mention",
        phase="playing",
        players=[
            {
                "tg_id": 1001,
                "company_id": 1,
                "name": "普普普",
                "hp": 2,
                "max_hp": 2,
                "items": ["saw"],
                "is_devil": False,
                "alive": True,
            },
            {
                "tg_id": DEVIL_TG_ID,
                "company_id": 0,
                "name": "魔鬼",
                "hp": 2,
                "max_hp": 2,
                "items": [],
                "is_devil": True,
                "alive": True,
            },
        ],
        current_round=0,
        shells=[True, False],
        shell_index=0,
        turn_order=[1001, DEVIL_TG_ID],
        turn_index=0,
        live_count=1,
        blank_count=1,
    )

    panel = render_game_panel(state, viewer_tg_id=1001)

    assert "▶ <a href='tg://user?id=1001'>普普普</a> 的回合" in panel


def test_render_panel_escapes_html_in_names_and_logs():
    state = GameState(
        room_id="room_escape",
        phase="playing",
        players=[
            {
                "tg_id": 2001,
                "company_id": 1,
                "name": "<A&B>",
                "hp": 2,
                "max_hp": 2,
                "items": [],
                "is_devil": False,
                "alive": True,
            },
            {
                "tg_id": DEVIL_TG_ID,
                "company_id": 0,
                "name": "魔鬼",
                "hp": 2,
                "max_hp": 2,
                "items": [],
                "is_devil": True,
                "alive": True,
            },
        ],
        current_round=0,
        shells=[True, False],
        shell_index=0,
        turn_order=[2001, DEVIL_TG_ID],
        turn_index=0,
        live_count=1,
        blank_count=1,
        action_log=["<A&B> 开枪了"],
    )

    panel = render_game_panel(state, viewer_tg_id=2001)

    assert "&lt;A&amp;B&gt;" in panel
    assert "  &lt;A&amp;B&gt; 开枪了" in panel
