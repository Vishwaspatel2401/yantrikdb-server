#!/usr/bin/env python3
"""Run the AIDB evaluation harness with real embeddings.

Usage:
    python scripts/run_eval.py [--model MODEL_NAME] [--top-k K]
"""

import argparse
import sys

from sentence_transformers import SentenceTransformer

from aidb import AIDB
from aidb.consolidate import consolidate
from aidb.eval.harness import evaluate
from aidb.eval.synthetic import load_sessions_into_db


def main():
    parser = argparse.ArgumentParser(description="AIDB Evaluation Harness")
    parser.add_argument(
        "--model", default="all-MiniLM-L6-v2",
        help="SentenceTransformer model name (default: all-MiniLM-L6-v2)",
    )
    parser.add_argument("--top-k", type=int, default=10, help="Top-K for recall")
    args = parser.parse_args()

    print(f"Loading embedding model: {args.model}...")
    embedder = SentenceTransformer(args.model)
    dim = embedder.get_sentence_embedding_dimension()
    print(f"Embedding dimension: {dim}")

    # ── Pre-consolidation baseline ──
    print("\n" + "=" * 60)
    print("PHASE 1: Pre-consolidation baseline")
    print("=" * 60)

    db = AIDB(db_path=":memory:", embedding_dim=dim, embedder=embedder)
    text_to_rid = load_sessions_into_db(db, embedder=embedder)

    stats = db.stats()
    print(f"Loaded: {stats['active_memories']} memories, {stats['edges']} edges, {stats['entities']} entities")

    print(f"\nRunning evaluation (top_k={args.top_k})...")
    report_before = evaluate(db, text_to_rid, top_k=args.top_k, embedder=embedder)
    print(report_before.summary())

    # ── Consolidation ──
    print("\n" + "=" * 60)
    print("PHASE 2: Running consolidation")
    print("=" * 60)

    # Dry run first
    from aidb.consolidate import find_consolidation_candidates
    candidates = find_consolidation_candidates(db, sim_threshold=0.5, time_window_days=10.0)
    print(f"Found {len(candidates)} consolidation clusters:")
    for i, cluster in enumerate(candidates):
        print(f"  Cluster {i+1} ({len(cluster)} memories):")
        for mem in cluster:
            print(f"    - {mem['text'][:80]}...")

    # Run consolidation
    results = consolidate(db, sim_threshold=0.5, time_window_days=10.0)
    print(f"\nConsolidated {len(results)} clusters:")
    for r in results:
        print(f"  [{r['cluster_size']} memories -> 1] {r['summary'][:100]}...")

    stats_after = db.stats()
    print(f"\nAfter consolidation: {stats_after['active_memories']} active, "
          f"{stats_after['consolidated_memories']} consolidated, "
          f"{stats_after['edges']} edges")

    reduction = 1 - (stats_after['active_memories'] / stats['active_memories'])
    print(f"Memory reduction: {reduction:.0%}")

    # ── Post-consolidation evaluation ──
    print("\n" + "=" * 60)
    print("PHASE 3: Post-consolidation evaluation")
    print("=" * 60)

    report_after = evaluate(db, text_to_rid, top_k=args.top_k, embedder=embedder)
    report_after.mode = "aidb_post_consolidation"
    print(report_after.summary())

    # ── Comparison ──
    print("\n" + "=" * 60)
    print("COMPARISON: Before vs After Consolidation")
    print("=" * 60)
    print(f"{'Metric':<25} {'Before':>10} {'After':>10} {'Delta':>10}")
    print("-" * 55)
    print(f"{'Active memories':<25} {stats['active_memories']:>10} {stats_after['active_memories']:>10} {stats_after['active_memories'] - stats['active_memories']:>+10}")
    print(f"{'Mean Recall@K':<25} {report_before.mean_recall_at_k:>10.3f} {report_after.mean_recall_at_k:>10.3f} {report_after.mean_recall_at_k - report_before.mean_recall_at_k:>+10.3f}")
    print(f"{'Mean Precision@K':<25} {report_before.mean_precision_at_k:>10.3f} {report_after.mean_precision_at_k:>10.3f} {report_after.mean_precision_at_k - report_before.mean_precision_at_k:>+10.3f}")
    print(f"{'Mean MRR':<25} {report_before.mean_reciprocal_rank:>10.3f} {report_after.mean_reciprocal_rank:>10.3f} {report_after.mean_reciprocal_rank - report_before.mean_reciprocal_rank:>+10.3f}")

    print(f"\n{'Tag':<25} {'Before':>10} {'After':>10} {'Delta':>10}")
    print("-" * 55)
    all_tags = sorted(set(report_before.recall_by_tag.keys()) | set(report_after.recall_by_tag.keys()))
    for tag in all_tags:
        before = report_before.recall_by_tag.get(tag, 0)
        after = report_after.recall_by_tag.get(tag, 0)
        print(f"{tag:<25} {before:>10.3f} {after:>10.3f} {after - before:>+10.3f}")

    db.close()

    # Success criteria
    if report_after.mean_recall_at_k < report_before.mean_recall_at_k * 0.9:
        print("\nFAILED: Recall degraded by more than 10% after consolidation.")
        sys.exit(1)
    else:
        print("\nPASSED: Recall maintained after consolidation.")


if __name__ == "__main__":
    main()
