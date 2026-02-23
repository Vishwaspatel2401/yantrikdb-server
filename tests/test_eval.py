"""Tests for the evaluation harness — uses a mock embedder for speed."""

import math

import pytest

from aidb import AIDB
from aidb.eval.harness import evaluate
from aidb.eval.synthetic import GOLDEN_QUERIES, SESSIONS, load_sessions_into_db


DIM = 64


class MockEmbedder:
    """Deterministic embedder that hashes text into a unit vector.

    Not semantically meaningful, but tests the harness plumbing.
    """

    def encode(self, text: str) -> list[float]:
        # Simple hash-based pseudo-embedding
        raw = []
        for i in range(DIM):
            h = hash(text + str(i)) % 10000
            raw.append(h / 10000.0 - 0.5)
        norm = math.sqrt(sum(x * x for x in raw))
        return [x / norm for x in raw]


@pytest.fixture
def loaded_db():
    embedder = MockEmbedder()
    db = AIDB(db_path=":memory:", embedding_dim=DIM, embedder=embedder)
    text_to_rid = load_sessions_into_db(db, embedder=embedder)
    yield db, text_to_rid, embedder
    db.close()


class TestSyntheticData:
    def test_all_sessions_loaded(self, loaded_db):
        db, text_to_rid, _ = loaded_db
        total_mems = sum(len(s["memories"]) for s in SESSIONS)
        assert db.stats()["active_memories"] == total_mems
        assert len(text_to_rid) == total_mems

    def test_entities_created(self, loaded_db):
        db, _, _ = loaded_db
        assert db.stats()["entities"] > 0
        assert db.stats()["edges"] > 0

    def test_temporal_ordering(self, loaded_db):
        db, text_to_rid, _ = loaded_db
        # First session memory should be older than last session memory
        first_rid = text_to_rid[SESSIONS[0]["memories"][0]["text"]]
        last_rid = text_to_rid[SESSIONS[-1]["memories"][-1]["text"]]
        first = db.get(first_rid)
        last = db.get(last_rid)
        assert first["created_at"] < last["created_at"]


class TestHarness:
    def test_harness_runs(self, loaded_db):
        db, text_to_rid, embedder = loaded_db
        report = evaluate(db, text_to_rid, top_k=10, embedder=embedder)

        assert report.total_queries == len(GOLDEN_QUERIES)
        assert report.total_memories > 0
        assert report.elapsed_seconds >= 0
        assert 0.0 <= report.mean_recall_at_k <= 1.0
        assert 0.0 <= report.mean_precision_at_k <= 1.0
        assert 0.0 <= report.mean_reciprocal_rank <= 1.0

    def test_per_query_results(self, loaded_db):
        db, text_to_rid, embedder = loaded_db
        report = evaluate(db, text_to_rid, top_k=10, embedder=embedder)

        for qr in report.query_results:
            assert qr.query_id
            assert qr.expected_count > 0
            assert len(qr.hits) + len(qr.misses) == qr.expected_count
            assert 0.0 <= qr.recall_at_k <= 1.0
            assert 0.0 <= qr.precision_at_k <= 1.0

    def test_report_summary(self, loaded_db):
        db, text_to_rid, embedder = loaded_db
        report = evaluate(db, text_to_rid, top_k=10, embedder=embedder)
        summary = report.summary()

        assert "Evaluation Report" in summary
        assert "Mean Recall@K" in summary
        assert "Mean MRR" in summary

    def test_recall_by_tag(self, loaded_db):
        db, text_to_rid, embedder = loaded_db
        report = evaluate(db, text_to_rid, top_k=10, embedder=embedder)

        assert "semantic" in report.recall_by_tag
        assert len(report.recall_by_tag) > 0
