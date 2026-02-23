"""Tests for the proactive trigger system."""

import math
import time

import pytest

from aidb import AIDB
from aidb.triggers import Trigger, check_all_triggers, check_decay_triggers, check_consolidation_triggers

DIM = 8


def _vec(seed: float) -> list[float]:
    raw = [math.sin(seed * (i + 1) * 1.7) + math.cos(seed * (i + 2) * 0.3) for i in range(DIM)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


@pytest.fixture
def db():
    engine = AIDB(db_path=":memory:", embedding_dim=DIM)
    yield engine
    engine.close()


class TestDecayTriggers:
    def test_triggers_on_decayed_important_memory(self, db):
        rid = db.record("important deadline March 15", importance=0.9, half_life=100.0, embedding=_vec(1.0))
        # Backdate to simulate time passing
        db._conn.execute(
            "UPDATE memories SET last_access = ? WHERE rid = ?",
            (time.time() - 10000, rid),
        )
        db._conn.commit()

        triggers = check_decay_triggers(db, importance_threshold=0.5, decay_threshold=0.1)
        assert len(triggers) >= 1
        t = triggers[0]
        assert t.trigger_type == "decay_review"
        assert t.source_rids == [rid]
        assert t.urgency > 0
        assert "important deadline" in t.context["text"]
        assert t.suggested_action == "ask_user_to_confirm_or_forget"

    def test_no_trigger_for_fresh_memory(self, db):
        db.record("just recorded", importance=0.9, half_life=604800.0, embedding=_vec(1.0))
        triggers = check_decay_triggers(db, importance_threshold=0.5, decay_threshold=0.1)
        assert len(triggers) == 0

    def test_no_trigger_for_low_importance(self, db):
        rid = db.record("trivial", importance=0.1, half_life=1.0, embedding=_vec(1.0))
        db._conn.execute(
            "UPDATE memories SET last_access = ? WHERE rid = ?",
            (time.time() - 10000, rid),
        )
        db._conn.commit()

        triggers = check_decay_triggers(db, importance_threshold=0.5, decay_threshold=0.1)
        assert len(triggers) == 0

    def test_urgency_ordering(self, db):
        rid1 = db.record("critical deadline", importance=0.95, half_life=100.0, embedding=_vec(1.0))
        rid2 = db.record("medium task", importance=0.6, half_life=100.0, embedding=_vec(2.0))

        past = time.time() - 10000
        db._conn.execute("UPDATE memories SET last_access = ? WHERE rid = ?", (past, rid1))
        db._conn.execute("UPDATE memories SET last_access = ? WHERE rid = ?", (past, rid2))
        db._conn.commit()

        triggers = check_decay_triggers(db, importance_threshold=0.5, decay_threshold=0.1)
        assert len(triggers) == 2
        # Higher importance should have higher urgency
        assert triggers[0].urgency >= triggers[1].urgency

    def test_max_triggers_respected(self, db):
        past = time.time() - 10000
        for i in range(10):
            rid = db.record(f"memory {i}", importance=0.8, half_life=100.0, embedding=_vec(float(i)))
            db._conn.execute("UPDATE memories SET last_access = ? WHERE rid = ?", (past, rid))
        db._conn.commit()

        triggers = check_decay_triggers(db, importance_threshold=0.5, decay_threshold=0.1, max_triggers=3)
        assert len(triggers) == 3


class TestConsolidationTriggers:
    def test_trigger_when_enough_memories(self, db):
        for i in range(15):
            db.record(f"episodic memory {i}", memory_type="episodic", embedding=_vec(float(i)))

        triggers = check_consolidation_triggers(db, min_active_memories=10)
        assert len(triggers) == 1
        assert triggers[0].trigger_type == "consolidation_ready"
        assert triggers[0].context["episodic_count"] == 15

    def test_no_trigger_when_few_memories(self, db):
        for i in range(3):
            db.record(f"mem {i}", memory_type="episodic", embedding=_vec(float(i)))

        triggers = check_consolidation_triggers(db, min_active_memories=10)
        assert len(triggers) == 0


class TestAllTriggers:
    def test_combined_output(self, db):
        # Create some decayed memories and some fresh ones
        past = time.time() - 10000
        rid = db.record("important old", importance=0.9, half_life=100.0, embedding=_vec(1.0))
        db._conn.execute("UPDATE memories SET last_access = ? WHERE rid = ?", (past, rid))

        for i in range(12):
            db.record(f"episodic {i}", memory_type="episodic", embedding=_vec(float(i + 10)))
        db._conn.commit()

        triggers = check_all_triggers(db, importance_threshold=0.5, decay_threshold=0.1)
        trigger_types = {t.trigger_type for t in triggers}
        assert "decay_review" in trigger_types
        assert "consolidation_ready" in trigger_types

    def test_trigger_dataclass(self, db):
        rid = db.record("test", importance=0.9, half_life=1.0, embedding=_vec(1.0))
        db._conn.execute(
            "UPDATE memories SET last_access = ? WHERE rid = ?",
            (time.time() - 1000, rid),
        )
        db._conn.commit()

        triggers = check_decay_triggers(db, importance_threshold=0.5, decay_threshold=0.5)
        assert len(triggers) >= 1
        t = triggers[0]
        assert isinstance(t, Trigger)
        assert isinstance(t.context, dict)
        assert "days_since_access" in t.context
        assert t.context["original_importance"] == 0.9
