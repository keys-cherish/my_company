"""Tests for roulette command argument parsing."""

from handlers.roulette import _parse_demon_bet_arg


def test_parse_demon_without_amount():
    assert _parse_demon_bet_arg("/cp_demon") == (True, 0)


def test_parse_demon_with_amount():
    assert _parse_demon_bet_arg("/cp_demon 5000") == (True, 5000)
    assert _parse_demon_bet_arg("/cp_demon 5,000") == (True, 5000)


def test_parse_demon_with_invalid_amount():
    assert _parse_demon_bet_arg("/cp_demon abc") == (False, 0)
