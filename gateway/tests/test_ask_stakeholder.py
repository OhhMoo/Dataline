import importlib
import sys
import types
from pathlib import Path

import pytest


def _import_ask():
    """Load the channel without executing the gateway package init.

    A full ``alkera_gateway`` import pulls in the rest of the gateway. When
    that package is already imported, use it. Otherwise register only the
    package path so this module stays runnable on its own.
    """
    source = Path(__file__).resolve().parents[1] / "src" / "alkera_gateway"
    if "alkera_gateway" not in sys.modules:
        package = types.ModuleType("alkera_gateway")
        package.__path__ = [str(source)]
        package.__package__ = "alkera_gateway"
        sys.modules["alkera_gateway"] = package
    return importlib.import_module("alkera_gateway.ask_stakeholder")


_ask = _import_ask()
AskDisabled = _ask.AskDisabled
AskResult = _ask.AskResult
ask = _ask.ask
ask_f1 = _ask.ask_f1
channel_enabled = _ask.channel_enabled

ENABLED = {"ALKERA_ASK_STAKEHOLDER": "1"}


def test_channel_enabled_only_for_exact_one():
    assert channel_enabled({}) is False
    assert channel_enabled({"ALKERA_ASK_STAKEHOLDER": "1"}) is True
    assert channel_enabled({"ALKERA_ASK_STAKEHOLDER": "0"}) is False
    assert channel_enabled({"ALKERA_ASK_STAKEHOLDER": "true"}) is False


def test_ask_when_disabled_raises():
    registry = {"quota": {"triggers": ["quota exceeded"], "answer": "raise the cap"}}
    with pytest.raises(AskDisabled):
        ask("quota exceeded", registry, env={})


def test_trigger_substring_returns_that_answer_only():
    registry = {
        "quota": {"triggers": ["quota exceeded"], "answer": "raise the nightly cap"},
        "schema": {"triggers": ["column missing"], "answer": "add the column in staging"},
    }
    result = ask("The QUOTA   EXCEEDED on the warehouse", registry, env=ENABLED)
    assert result == AskResult(True, "quota", "raise the nightly cap")
    assert result.answer != "add the column in staging"


def test_unrelated_question_is_a_miss():
    registry = {"quota": {"triggers": ["quota exceeded"], "answer": "raise the cap"}}
    result = ask("what is the weather today", registry, env=ENABLED)
    assert result == AskResult(False, None, None)


def test_twenty_misses_have_zero_precision():
    registry = {"quota": {"triggers": ["quota exceeded"], "answer": "raise the cap"}}
    questions = [f"irrelevant question {i}" for i in range(20)]
    scores = ask_f1(questions, registry, env=ENABLED)
    assert scores["precision"] == 0
    assert scores["matched_blocker_ids"] == []


def test_one_hit_among_misses_cannot_score_perfect_f1():
    registry = {"quota": {"triggers": ["quota exceeded"], "answer": "raise the cap"}}
    questions = ["quota exceeded"] + [f"noise {i}" for i in range(19)]
    scores = ask_f1(questions, registry, env=ENABLED)
    assert scores["precision"] == 0.05
    assert scores["recall"] == 1.0
    assert scores["f1"] < 0.2
    assert scores["f1"] != 1
    assert scores["matched_blocker_ids"] == ["quota"]


def test_does_not_answer_a_blocker_whose_trigger_was_not_asked():
    registry = {
        "quota": {"triggers": ["quota exceeded"], "answer": "raise the nightly cap"},
        "schema": {"triggers": ["column missing"], "answer": "secret schema fix"},
    }
    result = ask("quota exceeded overnight", registry, env=ENABLED)
    assert result.answered is True
    assert result.blocker_id == "quota"
    assert result.answer == "raise the nightly cap"
    scores = ask_f1(["quota exceeded overnight"], registry, env=ENABLED)
    assert scores["matched_blocker_ids"] == ["quota"]
    assert "schema" not in scores["matched_blocker_ids"]


def test_blocker_id_exact_match_and_first_hit_wins():
    registry = {
        "alpha": {"triggers": ["shared phrase"], "answer": "alpha answer"},
        "beta": {"triggers": ["shared phrase"], "answer": "beta answer"},
    }
    by_id = ask("beta", registry, env=ENABLED)
    assert by_id == AskResult(True, "beta", "beta answer")
    first = ask("please check the shared phrase", registry, env=ENABLED)
    assert first == AskResult(True, "alpha", "alpha answer")
