"""AIDB Consolidation Engine — merge redundant episodic memories into semantic summaries.

Pipeline:
  1. Find candidate clusters (embedding similarity + temporal proximity + entity overlap)
  2. Score each cluster for consolidation worthiness
  3. Generate a summary (extractive v1, LLM-driven v2)
  4. Create new semantic memory with provenance links
  5. Mark source memories as consolidated (don't delete)
"""

import json
import math
import struct
import time
from collections import defaultdict

from aidb.engine import AIDB, _deserialize_f32, _serialize_f32


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _find_clusters(
    memories: list[dict],
    sim_threshold: float = 0.6,
    time_window_days: float = 7.0,
    min_cluster_size: int = 2,
    max_cluster_size: int = 10,
) -> list[list[dict]]:
    """Find clusters of related memories using greedy agglomerative approach.

    Two memories can cluster together if:
      - Embedding similarity >= sim_threshold
      - Created within time_window_days of each other

    Returns list of clusters (each cluster = list of memory dicts).
    """
    if len(memories) < min_cluster_size:
        return []

    # Sort by creation time
    sorted_mems = sorted(memories, key=lambda m: m["created_at"])
    used = set()
    clusters = []

    for i, anchor in enumerate(sorted_mems):
        if anchor["rid"] in used:
            continue

        cluster = [anchor]
        used.add(anchor["rid"])

        for j in range(i + 1, len(sorted_mems)):
            candidate = sorted_mems[j]
            if candidate["rid"] in used:
                continue

            # Time proximity check
            time_diff = abs(candidate["created_at"] - anchor["created_at"])
            if time_diff > time_window_days * 86400:
                continue

            # Similarity check
            sim = _cosine_similarity(anchor["embedding"], candidate["embedding"])
            if sim >= sim_threshold:
                cluster.append(candidate)
                used.add(candidate["rid"])

                if len(cluster) >= max_cluster_size:
                    break

        if len(cluster) >= min_cluster_size:
            clusters.append(cluster)

    return clusters


def _entity_overlap(db: AIDB, rids: list[str]) -> set[str]:
    """Find entities that appear in edges connected to any of the given RIDs."""
    entities = set()
    for rid in rids:
        edges = db.get_edges(rid)
        for e in edges:
            entities.add(e["src"])
            entities.add(e["dst"])
    return entities


def _extractive_summary(memories: list[dict]) -> str:
    """Generate an extractive summary by selecting the most important memory
    and combining key facts from the cluster.

    v1: Simple approach — pick the highest-importance memory as the lead,
    then append unique information from others.
    """
    # Sort by importance descending
    ranked = sorted(memories, key=lambda m: m["importance"], reverse=True)

    # Lead with the most important
    lead = ranked[0]["text"]

    # Collect additional facts from other memories
    additional = []
    for mem in ranked[1:]:
        # Only add if it brings meaningfully different content
        text = mem["text"].strip()
        if text and text != lead:
            additional.append(text)

    if additional:
        parts = [lead] + additional
        return " | ".join(parts)
    return lead


def _mean_embedding(memories: list[dict]) -> list[float]:
    """Compute the mean embedding of a set of memories."""
    n = len(memories)
    dim = len(memories[0]["embedding"])
    result = [0.0] * dim
    for mem in memories:
        for i, v in enumerate(mem["embedding"]):
            result[i] += v
    return [v / n for v in result]


def find_consolidation_candidates(
    db: AIDB,
    sim_threshold: float = 0.6,
    time_window_days: float = 7.0,
    min_cluster_size: int = 2,
) -> list[list[dict]]:
    """Find clusters of memories that are candidates for consolidation.

    Returns clusters without actually consolidating — for preview/review.
    """
    # Fetch all active episodic memories with embeddings
    rows = db._conn.execute(
        """SELECT rid, type, text, embedding, created_at, importance, valence,
                  half_life, last_access, metadata
           FROM memories
           WHERE consolidation_status = 'active'
           AND type IN ('episodic', 'semantic')"""
    ).fetchall()

    memories = []
    for row in rows:
        memories.append({
            "rid": row["rid"],
            "type": row["type"],
            "text": row["text"],
            "embedding": _deserialize_f32(row["embedding"]),
            "created_at": row["created_at"],
            "importance": row["importance"],
            "valence": row["valence"],
            "half_life": row["half_life"],
            "last_access": row["last_access"],
            "metadata": json.loads(row["metadata"]),
        })

    return _find_clusters(
        memories,
        sim_threshold=sim_threshold,
        time_window_days=time_window_days,
        min_cluster_size=min_cluster_size,
    )


def consolidate(
    db: AIDB,
    sim_threshold: float = 0.6,
    time_window_days: float = 7.0,
    min_cluster_size: int = 2,
    dry_run: bool = False,
) -> list[dict]:
    """Run the full consolidation pipeline.

    For each cluster of related memories:
      1. Generate extractive summary
      2. Create new semantic memory with mean embedding
      3. Transfer entity relationships to consolidated memory
      4. Mark source memories as consolidated
      5. Log the operation

    Args:
        db: AIDB instance.
        sim_threshold: Minimum cosine similarity to cluster memories.
        time_window_days: Maximum time gap within a cluster.
        min_cluster_size: Minimum memories to form a cluster.
        dry_run: If True, find clusters but don't consolidate.

    Returns:
        List of consolidation results with source/target info.
    """
    clusters = find_consolidation_candidates(
        db,
        sim_threshold=sim_threshold,
        time_window_days=time_window_days,
        min_cluster_size=min_cluster_size,
    )

    if dry_run:
        return [
            {
                "cluster_size": len(cluster),
                "texts": [m["text"] for m in cluster],
                "preview_summary": _extractive_summary(cluster),
                "source_rids": [m["rid"] for m in cluster],
            }
            for cluster in clusters
        ]

    results = []
    now = time.time()

    for cluster in clusters:
        source_rids = [m["rid"] for m in cluster]

        # 1. Generate summary
        summary_text = _extractive_summary(cluster)

        # 2. Compute mean embedding
        mean_emb = _mean_embedding(cluster)

        # 3. Aggregate importance (max of sources, slightly boosted)
        max_importance = max(m["importance"] for m in cluster)
        consolidated_importance = min(1.0, max_importance * 1.1)

        # Mean valence
        mean_valence = sum(m["valence"] for m in cluster) / len(cluster)

        # Longer half-life for consolidated memories (they're proven important)
        max_half_life = max(m["half_life"] for m in cluster)
        consolidated_half_life = max_half_life * 1.5

        # 4. Record the new consolidated memory
        consolidated_rid = db.record(
            text=summary_text,
            memory_type="semantic",
            importance=consolidated_importance,
            valence=mean_valence,
            half_life=consolidated_half_life,
            metadata={
                "consolidated_from": source_rids,
                "cluster_size": len(cluster),
                "consolidation_time": now,
            },
            embedding=mean_emb,
        )

        # 5. Transfer entity relationships
        all_entities = set()
        for mem in cluster:
            edges = db.get_edges(mem["rid"])
            for edge in edges:
                all_entities.add(edge["src"])
                all_entities.add(edge["dst"])
                # Create edge from consolidated memory to same entities
                if edge["src"] == mem["rid"]:
                    db.relate(consolidated_rid, edge["dst"], rel_type=edge["rel_type"], weight=edge["weight"])
                elif edge["dst"] == mem["rid"]:
                    db.relate(edge["src"], consolidated_rid, rel_type=edge["rel_type"], weight=edge["weight"])

        # 6. Mark source memories as consolidated
        for source_rid in source_rids:
            db._conn.execute(
                """UPDATE memories
                SET consolidation_status = 'consolidated',
                    consolidated_into = ?,
                    updated_at = ?,
                    importance = importance * 0.3
                WHERE rid = ?""",
                (consolidated_rid, now, source_rid),
            )

        # 7. Log the operation
        db._log_op("consolidate", consolidated_rid, {
            "source_rids": source_rids,
            "cluster_size": len(cluster),
            "summary_preview": summary_text[:200],
        })

        db._conn.commit()

        results.append({
            "consolidated_rid": consolidated_rid,
            "source_rids": source_rids,
            "cluster_size": len(cluster),
            "summary": summary_text,
            "importance": consolidated_importance,
            "entities_linked": len(all_entities),
        })

    return results
