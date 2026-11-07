from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from evocode.token_budget import TokenBudgetExceeded, TokenBudgetLedger


def test_concurrent_reservations_cannot_exceed_shared_budget(tmp_path):
    database = tmp_path / "budget.sqlite3"

    def reserve(index):
        ledger = TokenBudgetLedger(database, 100, safety_margin_tokens=0)
        try:
            return ledger.reserve(10, role="coder", model_id=f"model-{index}")
        except TokenBudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=16) as pool:
        reservations = list(pool.map(reserve, range(24)))

    assert sum(item is not None for item in reservations) == 10
    state = TokenBudgetLedger(database, 100, safety_margin_tokens=0).snapshot()
    assert state["pending_reserved_tokens"] == 100
    assert state["reserved_or_reported_tokens"] == 100


def test_reservation_rejects_request_that_would_cross_limit(tmp_path):
    ledger = TokenBudgetLedger(tmp_path / "budget.sqlite3", 50, safety_margin_tokens=0)
    ledger.reserve(30, role="planner", model_id="test-model")

    with pytest.raises(TokenBudgetExceeded):
        ledger.reserve(21, role="coder", model_id="test-model")

    assert ledger.snapshot()["pending_reserved_tokens"] == 30


def test_settle_uses_reported_total_or_sum_of_input_and_output(tmp_path):
    ledger = TokenBudgetLedger(tmp_path / "budget.sqlite3", 500, safety_margin_tokens=0)
    first = ledger.reserve(150, role="planner", model_id="test-model")
    second = ledger.reserve(150, role="coder", model_id="test-model")

    ledger.settle(first, {"total_tokens": 37})
    ledger.settle(second, {"prompt_tokens": 21, "completion_tokens": 9})

    state = ledger.snapshot()
    assert state["reported_tokens"] == 67
    assert state["pending_reserved_tokens"] == 0
    assert state["unknown_reserved_tokens"] == 0
    assert state["usage_known_requests"] == 2
    assert state["usage_unknown_requests"] == 0


def test_missing_usage_keeps_estimate_reserved_instead_of_releasing_it(tmp_path):
    ledger = TokenBudgetLedger(tmp_path / "budget.sqlite3", 500, safety_margin_tokens=0)
    reservation = ledger.reserve(123, role="evolution", model_id="test-model")

    ledger.settle(reservation, None)

    state = ledger.snapshot()
    assert state["reported_tokens"] == 0
    assert state["pending_reserved_tokens"] == 0
    assert state["unknown_reserved_tokens"] == 123
    assert state["usage_unknown_requests"] == 1


def test_explicit_zero_usage_is_known_and_does_not_become_unknown_reserve(tmp_path):
    ledger = TokenBudgetLedger(tmp_path / "budget.sqlite3", 500, safety_margin_tokens=0)
    reservation = ledger.reserve(100, role="coder", model_id="test-model")

    ledger.settle(reservation, {"total_tokens": 0})

    state = ledger.snapshot()
    assert state["reported_tokens"] == 0
    assert state["pending_reserved_tokens"] == 0
    assert state["unknown_reserved_tokens"] == 0
    assert state["usage_known_requests"] == 1
    assert state["usage_unknown_requests"] == 0


def test_release_removes_only_pending_estimate(tmp_path):
    ledger = TokenBudgetLedger(tmp_path / "budget.sqlite3", 500, safety_margin_tokens=0)
    reservation = ledger.reserve(100, role="coder", model_id="test-model")
    ledger.release(reservation)

    state = ledger.snapshot()
    assert state["pending_reserved_tokens"] == 0
    assert state["unknown_reserved_tokens"] == 0
    assert state["reported_tokens"] == 0
    assert state["remaining_admissible_tokens"] == 500


def test_existing_ledger_rejects_a_different_total_limit(tmp_path):
    database = tmp_path / "budget.sqlite3"
    TokenBudgetLedger(database, 500, safety_margin_tokens=0)

    with pytest.raises(ValueError, match="different limit"):
        TokenBudgetLedger(database, 501, safety_margin_tokens=0)


def test_budget_stopped_ledger_can_raise_limit_and_resume(tmp_path):
    database = tmp_path / "budget.sqlite3"
    ledger = TokenBudgetLedger(database, 50, safety_margin_tokens=7)
    ledger.reserve(43, role="planner", model_id="test-model")

    with pytest.raises(TokenBudgetExceeded):
        ledger.reserve(1, role="coder", model_id="test-model")
    assert ledger.snapshot()["stop_reason"] == "token_budget_limit"

    ledger.raise_limit(100)

    state = ledger.snapshot()
    assert state["max_total_tokens"] == 100
    assert state["safety_margin_tokens"] == 7
    assert state["stop_reason"] is None
    assert ledger.reserve(50, role="coder", model_id="test-model")

    reopened = TokenBudgetLedger(database, 100, safety_margin_tokens=999)
    assert reopened.snapshot()["max_total_tokens"] == 100
    assert reopened.snapshot()["safety_margin_tokens"] == 7


@pytest.mark.parametrize("new_limit", [49, 50])
def test_raise_limit_rejects_lower_or_equal_limit(tmp_path, new_limit):
    ledger = TokenBudgetLedger(tmp_path / "budget.sqlite3", 50, safety_margin_tokens=0)
    with pytest.raises(TokenBudgetExceeded):
        ledger.reserve(51, role="planner", model_id="test-model")

    with pytest.raises(ValueError, match="greater than"):
        ledger.raise_limit(new_limit)

    state = ledger.snapshot()
    assert state["max_total_tokens"] == 50
    assert state["stop_reason"] == "token_budget_limit"


def test_raise_limit_does_not_clear_a_different_stop_reason(tmp_path):
    database = tmp_path / "budget.sqlite3"
    ledger = TokenBudgetLedger(database, 50, safety_margin_tokens=0)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE token_budget_state SET stop_reason='operator_stop' WHERE singleton=1"
        )

    with pytest.raises(ValueError, match="not stopped by the token budget limit"):
        ledger.raise_limit(100)

    state = ledger.snapshot()
    assert state["max_total_tokens"] == 50
    assert state["stop_reason"] == "operator_stop"
