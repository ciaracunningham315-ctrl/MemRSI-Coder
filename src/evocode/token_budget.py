from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path


class TokenBudgetExceeded(RuntimeError):
    def __init__(self, state: dict):
        self.state = state
        super().__init__(
            "model token budget reached: "
            f"reported={state['reported_tokens']}, "
            f"unknown_reserved={state['unknown_reserved_tokens']}, "
            f"pending={state['pending_reserved_tokens']}, "
            f"limit={state['max_total_tokens']}"
        )


class TokenBudgetLedger:
    """Cross-process model-token admission ledger for bounded live evaluations."""

    def __init__(self, path: str | Path, max_total_tokens: int, *, safety_margin_tokens: int | None = None):
        if type(max_total_tokens) is not int or max_total_tokens <= 0:
            raise ValueError("max_total_tokens must be a positive integer")
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_total_tokens = max_total_tokens
        self.safety_margin_tokens = (safety_margin_tokens if safety_margin_tokens is not None
                                     else min(32_768, max(4_096, max_total_tokens // 50)))
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self):
        for attempt in range(8):
            try:
                with closing(self._connect()) as conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("""CREATE TABLE IF NOT EXISTS token_budget_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                max_total_tokens INTEGER NOT NULL,
                safety_margin_tokens INTEGER NOT NULL,
                reported_tokens INTEGER NOT NULL DEFAULT 0,
                unknown_reserved_tokens INTEGER NOT NULL DEFAULT 0,
                pending_reserved_tokens INTEGER NOT NULL DEFAULT 0,
                request_count INTEGER NOT NULL DEFAULT 0,
                usage_known_requests INTEGER NOT NULL DEFAULT 0,
                usage_unknown_requests INTEGER NOT NULL DEFAULT 0,
                stop_reason TEXT,
                updated_at REAL NOT NULL
                    )""")
                    conn.execute("""CREATE TABLE IF NOT EXISTS token_budget_reservations (
                reservation_id TEXT PRIMARY KEY,
                estimated_tokens INTEGER NOT NULL,
                actual_tokens INTEGER,
                status TEXT NOT NULL,
                role TEXT NOT NULL,
                model_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                settled_at REAL
                    )""")
                    # A single atomic insert makes first-time initialization safe
                    # when several processes open the same ledger concurrently.
                    conn.execute("""INSERT OR IGNORE INTO token_budget_state(
                    singleton,max_total_tokens,safety_margin_tokens,updated_at
                ) VALUES (1,?,?,?)""", (self.max_total_tokens, self.safety_margin_tokens, time.time()))
                    row = conn.execute("SELECT max_total_tokens FROM token_budget_state WHERE singleton=1").fetchone()
                    if row["max_total_tokens"] != self.max_total_tokens:
                        raise ValueError("token budget ledger already exists with a different limit")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 7:
                    raise
                time.sleep(0.025 * (2 ** attempt))
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def raise_limit(self, new_limit: int) -> None:
        """Explicitly resume a budget-stopped ledger with a larger token limit."""
        if type(new_limit) is not int or new_limit <= 0:
            raise ValueError("new_limit must be a positive integer")

        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = conn.execute(
                "SELECT max_total_tokens, stop_reason FROM token_budget_state WHERE singleton=1"
            ).fetchone()
            if new_limit <= state["max_total_tokens"]:
                conn.rollback()
                raise ValueError("new_limit must be greater than the existing limit")
            if state["stop_reason"] != "token_budget_limit":
                conn.rollback()
                raise ValueError("ledger is not stopped by the token budget limit")

            conn.execute("""UPDATE token_budget_state
                SET max_total_tokens=?,stop_reason=NULL,updated_at=?
                WHERE singleton=1 AND max_total_tokens=? AND stop_reason='token_budget_limit'""",
                         (new_limit, time.time(), state["max_total_tokens"]))
            conn.commit()
        self.max_total_tokens = new_limit

    @staticmethod
    def estimate_request_tokens(payload: dict) -> int:
        """Use serialized UTF-8 bytes as a conservative input-token upper-bound proxy."""
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return max(1, len(serialized.encode("utf-8")))

    def reserve(self, estimated_tokens: int, *, role: str, model_id: str) -> str:
        if type(estimated_tokens) is not int or estimated_tokens <= 0:
            raise ValueError("estimated_tokens must be a positive integer")
        reservation_id = str(uuid.uuid4())
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = conn.execute("SELECT * FROM token_budget_state WHERE singleton=1").fetchone()
            occupied = (state["reported_tokens"] + state["unknown_reserved_tokens"] +
                        state["pending_reserved_tokens"])
            if state["stop_reason"] or occupied + estimated_tokens + state["safety_margin_tokens"] > state["max_total_tokens"]:
                conn.execute("UPDATE token_budget_state SET stop_reason=COALESCE(stop_reason,'token_budget_limit'), updated_at=? WHERE singleton=1",
                             (time.time(),))
                conn.commit()
                raise TokenBudgetExceeded(self.snapshot())
            conn.execute("""INSERT INTO token_budget_reservations(
                reservation_id,estimated_tokens,status,role,model_id,created_at
            ) VALUES (?,?, 'pending',?,?,?)""",
                         (reservation_id, estimated_tokens, role, model_id, time.time()))
            conn.execute("""UPDATE token_budget_state SET pending_reserved_tokens=pending_reserved_tokens+?,
                request_count=request_count+1,updated_at=? WHERE singleton=1""",
                         (estimated_tokens, time.time()))
            conn.commit()
        return reservation_id

    def settle(self, reservation_id: str, usage: dict | None = None) -> None:
        total = None
        if isinstance(usage, dict):
            total = usage.get("total_tokens")
            if type(total) is not int:
                input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
                output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
                if type(input_tokens) is int and type(output_tokens) is int:
                    total = input_tokens + output_tokens
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            reservation = conn.execute(
                "SELECT * FROM token_budget_reservations WHERE reservation_id=?", (reservation_id,)
            ).fetchone()
            if reservation is None or reservation["status"] != "pending":
                conn.rollback()
                return
            estimate = reservation["estimated_tokens"]
            pending_delta = -estimate
            known = type(total) is int and total >= 0
            known_delta = int(total) if known else 0
            unknown_delta = 0 if known else estimate
            status = "reported" if known else "usage_unknown_reserved"
            conn.execute("""UPDATE token_budget_reservations SET actual_tokens=?,status=?,settled_at=?
                WHERE reservation_id=?""", (known_delta if known else None, status, time.time(), reservation_id))
            conn.execute("""UPDATE token_budget_state SET
                pending_reserved_tokens=pending_reserved_tokens+?,
                reported_tokens=reported_tokens+?,
                unknown_reserved_tokens=unknown_reserved_tokens+?,
                usage_known_requests=usage_known_requests+?,
                usage_unknown_requests=usage_unknown_requests+?,updated_at=?
                WHERE singleton=1""",
                         (pending_delta, known_delta, unknown_delta, int(known),
                          int(not known), time.time()))
            state = conn.execute("SELECT * FROM token_budget_state WHERE singleton=1").fetchone()
            occupied = (state["reported_tokens"] + state["unknown_reserved_tokens"] +
                        state["pending_reserved_tokens"])
            if occupied + state["safety_margin_tokens"] >= state["max_total_tokens"]:
                conn.execute("UPDATE token_budget_state SET stop_reason=COALESCE(stop_reason,'token_budget_limit'),updated_at=? WHERE singleton=1",
                             (time.time(),))
            conn.commit()

    def release(self, reservation_id: str) -> None:
        """Release a reservation only when the provider definitely did no generation."""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            reservation = conn.execute(
                "SELECT * FROM token_budget_reservations WHERE reservation_id=?", (reservation_id,)
            ).fetchone()
            if reservation is None or reservation["status"] != "pending":
                conn.rollback()
                return
            conn.execute("UPDATE token_budget_reservations SET status='released',settled_at=? WHERE reservation_id=?",
                         (time.time(), reservation_id))
            conn.execute("UPDATE token_budget_state SET pending_reserved_tokens=pending_reserved_tokens-?,updated_at=? WHERE singleton=1",
                         (reservation["estimated_tokens"], time.time()))
            conn.commit()

    def snapshot(self) -> dict:
        with closing(self._connect()) as conn:
            state = conn.execute("SELECT * FROM token_budget_state WHERE singleton=1").fetchone()
        occupied = (state["reported_tokens"] + state["unknown_reserved_tokens"] +
                    state["pending_reserved_tokens"])
        return {
            "max_total_tokens": state["max_total_tokens"],
            "safety_margin_tokens": state["safety_margin_tokens"],
            "reported_tokens": state["reported_tokens"],
            "unknown_reserved_tokens": state["unknown_reserved_tokens"],
            "pending_reserved_tokens": state["pending_reserved_tokens"],
            "reserved_or_reported_tokens": occupied,
            "overrun_tokens": max(0, occupied - state["max_total_tokens"]),
            "remaining_admissible_tokens": max(
                0, state["max_total_tokens"] - occupied - state["safety_margin_tokens"]
            ),
            "request_count": state["request_count"],
            "usage_known_requests": state["usage_known_requests"],
            "usage_unknown_requests": state["usage_unknown_requests"],
            "stop_reason": state["stop_reason"],
        }


_LEDGER_CACHE: dict[tuple[str, int], TokenBudgetLedger] = {}


def budget_from_environment() -> TokenBudgetLedger | None:
    raw_path = os.getenv("EVOCODE_TOKEN_BUDGET_DB")
    raw_limit = os.getenv("EVOCODE_MAX_TOTAL_MODEL_TOKENS")
    if not raw_path and not raw_limit:
        return None
    if not raw_path or not raw_limit:
        raise ValueError("both EVOCODE_TOKEN_BUDGET_DB and EVOCODE_MAX_TOTAL_MODEL_TOKENS are required")
    limit = int(raw_limit)
    key = (str(Path(raw_path).expanduser().resolve()), limit)
    if key not in _LEDGER_CACHE:
        _LEDGER_CACHE[key] = TokenBudgetLedger(key[0], limit)
    return _LEDGER_CACHE[key]
