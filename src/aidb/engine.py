"""AIDB Engine — cognitive memory operations."""

import json
import math
import sqlite3
import struct
import time
from pathlib import Path
from typing import Optional

import sqlite_vec
from uuid_utils import uuid7

from aidb.schema import SCHEMA_SQL, SCHEMA_VERSION


def _serialize_f32(vector: list[float]) -> bytes:
    """Serialize a float32 vector to bytes for sqlite-vec."""
    return struct.pack(f"{len(vector)}f", *vector)


def _deserialize_f32(blob: bytes) -> list[float]:
    """Deserialize bytes to float32 vector."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


class AIDB:
    """Cognitive memory engine.

    Core operations: record(), recall(), relate(), consolidate(), decay(), forget().
    """

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        embedding_dim: int = 384,
        embedder=None,
    ):
        self.db_path = str(db_path)
        self.embedding_dim = embedding_dim
        self._embedder = embedder
        self._conn = self._init_db()

    def _init_db(self) -> sqlite3.Connection:
        """Initialize the database with schema and sqlite-vec."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        # Enable WAL mode for concurrent reads + background writes
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")

        # Create schema
        conn.executescript(SCHEMA_SQL)

        # Create virtual table for vector search
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories
            USING vec0(rid TEXT PRIMARY KEY, embedding float[{self.embedding_dim}])
            """
        )

        # Set schema version
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
        return conn

    def _embed(self, text: str) -> list[float]:
        """Generate embedding for text."""
        if self._embedder is None:
            raise RuntimeError(
                "No embedder configured. Pass an embedder to AIDB() or call set_embedder()."
            )
        return self._embedder.encode(text)

    def set_embedder(self, embedder) -> None:
        """Set the embedding model (e.g., SentenceTransformer instance)."""
        self._embedder = embedder

    def _log_op(self, op_type: str, target_rid: str | None, payload: dict) -> str:
        """Append an operation to the oplog."""
        op_id = str(uuid7())
        self._conn.execute(
            "INSERT INTO oplog (op_id, op_type, timestamp, target_rid, payload) VALUES (?, ?, ?, ?, ?)",
            (op_id, op_type, time.time(), target_rid, json.dumps(payload)),
        )
        return op_id

    # ──────────────────────────────────────────────
    # record() — store a memory
    # ──────────────────────────────────────────────

    def record(
        self,
        text: str,
        memory_type: str = "episodic",
        importance: float = 0.5,
        valence: float = 0.0,
        half_life: float = 604800.0,
        metadata: dict | None = None,
        embedding: list[float] | None = None,
    ) -> str:
        """Store a new memory.

        Args:
            text: The memory content.
            memory_type: episodic | semantic | procedural | emotional
            importance: Base importance score [0, 1].
            valence: Emotional weight [-1, 1].
            half_life: Decay half-life in seconds (default: 7 days).
            metadata: Optional JSON-serializable metadata.
            embedding: Pre-computed embedding. If None, uses the configured embedder.

        Returns:
            The memory's RID (UUIDv7).
        """
        rid = str(uuid7())
        now = time.time()

        if embedding is None:
            embedding = self._embed(text)

        emb_blob = _serialize_f32(embedding)

        self._conn.execute(
            """INSERT INTO memories
            (rid, type, text, embedding, created_at, updated_at, importance,
             half_life, last_access, valence, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rid, memory_type, text, emb_blob,
                now, now, importance, half_life, now, valence,
                json.dumps(metadata or {}),
            ),
        )

        # Insert into vector index
        self._conn.execute(
            "INSERT INTO vec_memories (rid, embedding) VALUES (?, ?)",
            (rid, emb_blob),
        )

        self._log_op("record", rid, {
            "type": memory_type,
            "text": text,
            "importance": importance,
            "valence": valence,
        })

        self._conn.commit()
        return rid

    # ──────────────────────────────────────────────
    # recall() — multi-signal retrieval
    # ──────────────────────────────────────────────

    def recall(
        self,
        query: str | None = None,
        query_embedding: list[float] | None = None,
        top_k: int = 10,
        time_window: tuple[float, float] | None = None,
        memory_type: str | None = None,
        include_consolidated: bool = False,
        expand_entities: bool = True,
    ) -> list[dict]:
        """Retrieve memories using multi-signal fusion.

        Combines: vector similarity + temporal recency + importance/decay + valence.

        Args:
            query: Text query (will be embedded).
            query_embedding: Pre-computed query embedding.
            top_k: Number of results to return.
            time_window: Optional (start, end) unix timestamps.
            memory_type: Filter by memory type.
            include_consolidated: Include memories that have been consolidated.
            expand_entities: Include graph-connected memories.

        Returns:
            List of memory dicts ranked by composite score, with 'why_retrieved' explanations.
        """
        now = time.time()

        if query_embedding is None and query is not None:
            query_embedding = self._embed(query)
        elif query_embedding is None:
            raise ValueError("Must provide either query or query_embedding")

        emb_blob = _serialize_f32(query_embedding)

        # Step 1: Vector candidate generation (fetch more than top_k for re-ranking)
        fetch_k = min(top_k * 5, 200)
        vec_results = self._conn.execute(
            """SELECT rid, distance
            FROM vec_memories
            WHERE embedding MATCH ?
            ORDER BY distance
            LIMIT ?""",
            (emb_blob, fetch_k),
        ).fetchall()

        if not vec_results:
            return []

        rids = [r["rid"] for r in vec_results]
        vec_scores = {r["rid"]: 1.0 - r["distance"] for r in vec_results}  # cosine similarity

        # Step 2: Fetch full memory records
        placeholders = ",".join("?" * len(rids))
        statuses = ["active", "consolidated"] if include_consolidated else ["active"]
        status_placeholders = ",".join("?" * len(statuses))

        sql = f"""
            SELECT * FROM memories
            WHERE rid IN ({placeholders})
            AND consolidation_status IN ({status_placeholders})
        """
        params = list(rids) + statuses

        if time_window:
            sql += " AND created_at BETWEEN ? AND ?"
            params.extend(time_window)

        if memory_type:
            sql += " AND type = ?"
            params.append(memory_type)

        memories = self._conn.execute(sql, params).fetchall()

        # Step 3: Score with multi-signal fusion
        scored = []
        for mem in memories:
            rid = mem["rid"]
            sim_score = vec_scores.get(rid, 0.0)

            # Decay score: I(t) = importance * 2^(-t / half_life)
            t = now - mem["last_access"]
            half_life = mem["half_life"]
            decay_score = mem["importance"] * math.pow(2, -t / half_life) if half_life > 0 else 0

            # Recency score: exponential decay from creation (separate from importance decay)
            age = now - mem["created_at"]
            recency_score = math.exp(-age / (7 * 86400))  # 7-day half-period

            # Valence boost: high emotional weight increases retrieval priority
            valence_boost = 1.0 + 0.3 * abs(mem["valence"])

            # Composite score
            composite = (
                0.40 * sim_score
                + 0.25 * decay_score
                + 0.20 * recency_score
                + 0.15 * min(1.0, mem["importance"])
            ) * valence_boost

            # Build explanation
            why = []
            if sim_score > 0.5:
                why.append(f"semantically similar ({sim_score:.2f})")
            if recency_score > 0.5:
                why.append("recent")
            if decay_score > 0.3:
                why.append(f"important (decay={decay_score:.2f})")
            if abs(mem["valence"]) > 0.5:
                why.append(f"emotionally weighted ({mem['valence']:.2f})")

            scored.append({
                "rid": rid,
                "type": mem["type"],
                "text": mem["text"],
                "created_at": mem["created_at"],
                "importance": mem["importance"],
                "valence": mem["valence"],
                "score": composite,
                "scores": {
                    "similarity": sim_score,
                    "decay": decay_score,
                    "recency": recency_score,
                    "importance": mem["importance"],
                },
                "why_retrieved": why or ["matched query"],
                "metadata": json.loads(mem["metadata"]),
            })

        # Step 4: Sort and return top_k
        scored.sort(key=lambda x: x["score"], reverse=True)
        results = scored[:top_k]

        # Reinforce accessed memories (spaced repetition)
        for r in results:
            self._reinforce(r["rid"])

        return results

    def _reinforce(self, rid: str) -> None:
        """Reinforce a memory on access — increase half_life and update last_access."""
        self._conn.execute(
            """UPDATE memories
            SET last_access = ?,
                half_life = MIN(half_life * 1.2, 31536000.0)
            WHERE rid = ?""",
            (time.time(), rid),
        )

    # ──────────────────────────────────────────────
    # relate() — create entity links
    # ──────────────────────────────────────────────

    def relate(
        self,
        src: str,
        dst: str,
        rel_type: str = "related_to",
        weight: float = 1.0,
    ) -> str:
        """Create or update a relationship between entities.

        Args:
            src: Source entity name or memory RID.
            dst: Destination entity name or memory RID.
            rel_type: Relationship type.
            weight: Relationship strength [0, 1].

        Returns:
            The edge ID.
        """
        edge_id = str(uuid7())
        now = time.time()

        self._conn.execute(
            """INSERT INTO edges (edge_id, src, dst, rel_type, weight, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(src, dst, rel_type) DO UPDATE SET
                weight = ?,
                created_at = ?""",
            (edge_id, src, dst, rel_type, weight, now, weight, now),
        )

        # Ensure entities exist
        for entity in (src, dst):
            self._conn.execute(
                """INSERT INTO entities (name, first_seen, last_seen)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    last_seen = ?,
                    mention_count = mention_count + 1""",
                (entity, now, now, now),
            )

        self._log_op("relate", None, {
            "src": src, "dst": dst, "rel_type": rel_type, "weight": weight,
        })
        self._conn.commit()
        return edge_id

    # ──────────────────────────────────────────────
    # decay() — compute current importance scores
    # ──────────────────────────────────────────────

    def decay(self, threshold: float = 0.01) -> list[dict]:
        """Find memories that have decayed below a threshold.

        Does NOT delete — returns candidates for review or auto-forget.
        This is lazy decay: scores are computed, not stored.

        Args:
            threshold: Minimum effective importance to keep.

        Returns:
            List of memories below threshold with their current scores.
        """
        now = time.time()
        memories = self._conn.execute(
            "SELECT rid, text, importance, half_life, last_access, type FROM memories WHERE consolidation_status = 'active'"
        ).fetchall()

        decayed = []
        for mem in memories:
            t = now - mem["last_access"]
            score = mem["importance"] * math.pow(2, -t / mem["half_life"]) if mem["half_life"] > 0 else 0
            if score < threshold:
                decayed.append({
                    "rid": mem["rid"],
                    "text": mem["text"],
                    "type": mem["type"],
                    "original_importance": mem["importance"],
                    "current_score": score,
                    "days_since_access": t / 86400,
                })

        return decayed

    # ──────────────────────────────────────────────
    # forget() — tombstone a memory
    # ──────────────────────────────────────────────

    def forget(self, rid: str) -> bool:
        """Tombstone a memory. Does not physically delete — preserves for replication.

        Args:
            rid: Memory RID to forget.

        Returns:
            True if the memory was found and tombstoned.
        """
        result = self._conn.execute(
            "UPDATE memories SET consolidation_status = 'tombstoned', updated_at = ? WHERE rid = ?",
            (time.time(), rid),
        )

        if result.rowcount > 0:
            # Remove from vector index
            self._conn.execute("DELETE FROM vec_memories WHERE rid = ?", (rid,))
            self._log_op("forget", rid, {})
            self._conn.commit()
            return True

        return False

    # ──────────────────────────────────────────────
    # Utility methods
    # ──────────────────────────────────────────────

    def get(self, rid: str) -> dict | None:
        """Get a single memory by RID."""
        row = self._conn.execute(
            "SELECT * FROM memories WHERE rid = ?", (rid,)
        ).fetchone()

        if row is None:
            return None

        return {
            "rid": row["rid"],
            "type": row["type"],
            "text": row["text"],
            "created_at": row["created_at"],
            "importance": row["importance"],
            "valence": row["valence"],
            "half_life": row["half_life"],
            "last_access": row["last_access"],
            "consolidation_status": row["consolidation_status"],
            "consolidated_into": row["consolidated_into"],
            "metadata": json.loads(row["metadata"]),
        }

    def get_edges(self, entity: str) -> list[dict]:
        """Get all edges connected to an entity."""
        rows = self._conn.execute(
            """SELECT * FROM edges
            WHERE (src = ? OR dst = ?) AND tombstoned = 0""",
            (entity, entity),
        ).fetchall()

        return [
            {
                "edge_id": r["edge_id"],
                "src": r["src"],
                "dst": r["dst"],
                "rel_type": r["rel_type"],
                "weight": r["weight"],
            }
            for r in rows
        ]

    def stats(self) -> dict:
        """Get engine statistics."""
        total = self._conn.execute(
            "SELECT COUNT(*) as c FROM memories WHERE consolidation_status = 'active'"
        ).fetchone()["c"]
        consolidated = self._conn.execute(
            "SELECT COUNT(*) as c FROM memories WHERE consolidation_status = 'consolidated'"
        ).fetchone()["c"]
        tombstoned = self._conn.execute(
            "SELECT COUNT(*) as c FROM memories WHERE consolidation_status = 'tombstoned'"
        ).fetchone()["c"]
        edges = self._conn.execute(
            "SELECT COUNT(*) as c FROM edges WHERE tombstoned = 0"
        ).fetchone()["c"]
        entities = self._conn.execute(
            "SELECT COUNT(*) as c FROM entities"
        ).fetchone()["c"]
        ops = self._conn.execute(
            "SELECT COUNT(*) as c FROM oplog"
        ).fetchone()["c"]

        return {
            "active_memories": total,
            "consolidated_memories": consolidated,
            "tombstoned_memories": tombstoned,
            "edges": edges,
            "entities": entities,
            "operations": ops,
        }

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
