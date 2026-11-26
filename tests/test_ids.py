"""Tests for rewii.core.ids."""
from __future__ import annotations

import pytest

from rewii.core import ids


def test_valid_game_id():
    for good in ("RMCE01", "rmce01", " GALE01 ", "HADE", "W2GE"):
        assert ids.valid_game_id(good), good
    for bad in ("", "RMC", "RMCE0", "RMCE012", "../../", "RM/E01", "RMCE0?", "??????", "RMCÉ01",
                "RM E01", "A:\\B", None, 123456):
        assert not ids.valid_game_id(bad), bad
    assert ids.valid_id4("RMCE") and not ids.valid_id4("/tmp") and not ids.valid_id4("RMCE01")


def test_clean_and_filename():
    assert ids.clean_game_id(" rmce01 ") == "RMCE01"
    with pytest.raises(ids.InvalidGameId):
        ids.clean_game_id("../x")
    assert ids.id_for_filename("rmce01") == "RMCE01"
    assert ids.id_for_filename("../../") == "______" and ids.id_for_filename("/a/b") == "_A_B"
    assert ids.id_for_filename("") == "_"
