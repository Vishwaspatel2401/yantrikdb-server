"""End-to-end test: verify that adaptive learning actually improves recall.

Scenario: High-importance memories from unrelated domains act as distractors,
pushing down the correct results. The optimizer should learn to:
1. Boost similarity weight (w_sim) — semantic match matters most
2. Reduce importance gating (alpha_imp) — stop letting high-importance irrelevant
   memories dominate

Embeddings use structured construction so we have real semantic signal.
Some topic clusters partially overlap (realistic ambiguity).
"""

import numpy as np
from yantrikdb import YantrikDB

DIM = 64

# Domain base vectors
_domain_rng = np.random.default_rng(42)
DOMAIN_BASES = {}
for _d in ["work", "family", "health", "food", "tech", "travel"]:
    _v = _domain_rng.standard_normal(DIM).astype(np.float64)
    _v /= np.linalg.norm(_v)
    DOMAIN_BASES[_d] = _v

# Topic sub-clusters
TOPIC_BASES = {}
_topic_rng = np.random.default_rng(123)
for _t in [
    "ml", "cicd", "caching", "review", "pairing", "database", "hnsw",
    "celebration", "children", "health_update", "conflict", "wedding",
    "exercise", "medical", "mental_health",
    "cooking", "restaurant",
    "programming", "hardware", "publishing",
    "asia", "africa", "europe",
]:
    _v = _topic_rng.standard_normal(DIM).astype(np.float64)
    _v /= np.linalg.norm(_v)
    TOPIC_BASES[_t] = _v


def make_emb(domain: str, topic: str, noise_seed: int, noise_scale: float = 0.1) -> list[float]:
    """Create embedding: 0.5*domain + 0.4*topic + noise_scale*noise."""
    local_rng = np.random.default_rng(noise_seed)
    noise = local_rng.standard_normal(DIM).astype(np.float64)
    noise /= np.linalg.norm(noise)
    v = 0.5 * DOMAIN_BASES[domain] + 0.4 * TOPIC_BASES[topic] + noise_scale * noise
    v = v.astype(np.float32)
    v /= np.linalg.norm(v)
    return v.tolist()


def make_query_emb(domain: str, topic: str, noise_seed: int = None, noise_scale: float = 0.0) -> list[float]:
    """Create query embedding. Optional noise for harder queries."""
    v = (0.5 * DOMAIN_BASES[domain] + 0.4 * TOPIC_BASES[topic]).astype(np.float64)
    if noise_seed is not None and noise_scale > 0:
        local_rng = np.random.default_rng(noise_seed)
        noise = local_rng.standard_normal(DIM).astype(np.float64)
        noise /= np.linalg.norm(noise)
        v += noise_scale * noise
    v = v.astype(np.float32)
    v /= np.linalg.norm(v)
    return v.tolist()


# ── Memories ──
# Key design: some memories have very high importance but are about DIFFERENT topics
# than what queries ask for. This creates the distractor problem that learning should fix.
MEMORIES = [
    # Work (8) — indices 0-7
    {"text": "Sarah presented the new recommendation engine using collaborative filtering",
     "type": "episodic", "importance": 0.7, "valence": 0.3, "domain": "work", "topic": "ml", "seed": 100},
    {"text": "Quarterly review showed 23% increase in user engagement from the ML pipeline",
     "type": "episodic", "importance": 0.8, "valence": 0.6, "domain": "work", "topic": "ml", "seed": 101},
    {"text": "CI/CD pipeline keeps failing on the payment module integration tests",
     "type": "episodic", "importance": 0.6, "valence": -0.4, "domain": "work", "topic": "cicd", "seed": 102},
    {"text": "Mike suggested Redis for caching API responses to reduce latency",
     "type": "episodic", "importance": 0.5, "valence": 0.2, "domain": "work", "topic": "caching", "seed": 103},
    {"text": "Annual review: exceeded technical delivery expectations, improve documentation",
     "type": "episodic", "importance": 0.8, "valence": -0.2, "domain": "work", "topic": "review", "seed": 104},
    {"text": "Learned HNSW algorithm for approximate nearest neighbor search",
     "type": "semantic", "importance": 0.6, "valence": 0.4, "domain": "work", "topic": "hnsw", "seed": 105},
    {"text": "Database migration from PostgreSQL to CockroachDB took three extra weeks",
     "type": "episodic", "importance": 0.7, "valence": -0.5, "domain": "work", "topic": "database", "seed": 106},
    {"text": "Pair programming with Alex on the authentication refactor",
     "type": "episodic", "importance": 0.5, "valence": 0.5, "domain": "work", "topic": "pairing", "seed": 107},

    # Family — HIGH IMPORTANCE DISTRACTORS (indices 8-14)
    # These have very high importance and should NOT appear in work/tech/health queries
    {"text": "Grandma's 80th birthday party at the lakehouse — best family day ever",
     "type": "episodic", "importance": 0.95, "valence": 0.9, "domain": "family", "topic": "celebration", "seed": 200},
    {"text": "Mom called about Dad's successful knee surgery, physical therapy starting",
     "type": "episodic", "importance": 0.9, "valence": 0.3, "domain": "family", "topic": "health_update", "seed": 201},
    {"text": "Sister announcing twins in September — the whole family is overjoyed",
     "type": "episodic", "importance": 0.92, "valence": 0.8, "domain": "family", "topic": "celebration", "seed": 202},
    {"text": "Family argument about holiday plans ruined Thanksgiving dinner",
     "type": "episodic", "importance": 0.6, "valence": -0.7, "domain": "family", "topic": "conflict", "seed": 203},
    {"text": "Nephew's first words were 'doggy' when he saw our golden retriever",
     "type": "episodic", "importance": 0.7, "valence": 0.9, "domain": "family", "topic": "children", "seed": 204},
    {"text": "Teaching daughter to ride a bicycle in the park on Sunday afternoon",
     "type": "episodic", "importance": 0.7, "valence": 0.8, "domain": "family", "topic": "children", "seed": 205},
    {"text": "Cousin's wedding in Napa Valley with vineyard ceremony",
     "type": "episodic", "importance": 0.75, "valence": 0.85, "domain": "family", "topic": "wedding", "seed": 206},

    # Health (5) — indices 15-19
    {"text": "Morning yoga routine three times a week for back pain",
     "type": "procedural", "importance": 0.6, "valence": 0.3, "domain": "health", "topic": "exercise", "seed": 300},
    {"text": "Doctor recommended reducing caffeine for better sleep quality",
     "type": "semantic", "importance": 0.7, "valence": -0.1, "domain": "health", "topic": "medical", "seed": 301},
    {"text": "Ran first 10K race in 52 minutes, amazing feeling at the finish line",
     "type": "episodic", "importance": 0.75, "valence": 0.8, "domain": "health", "topic": "exercise", "seed": 302},
    {"text": "Blood test showed vitamin D deficiency, started supplements",
     "type": "semantic", "importance": 0.65, "valence": -0.2, "domain": "health", "topic": "medical", "seed": 303},
    {"text": "Meditation app reduced anxiety before the big presentation",
     "type": "episodic", "importance": 0.5, "valence": 0.4, "domain": "health", "topic": "mental_health", "seed": 304},

    # Food (3) — indices 20-22
    {"text": "Incredible ramen shop in East Village with homemade noodles",
     "type": "episodic", "importance": 0.4, "valence": 0.7, "domain": "food", "topic": "restaurant", "seed": 400},
    {"text": "Sourdough bread from scratch using 3-day fermentation",
     "type": "procedural", "importance": 0.5, "valence": 0.5, "domain": "food", "topic": "cooking", "seed": 401},
    {"text": "Thai green curry recipe with fresh lemongrass and galangal",
     "type": "procedural", "importance": 0.45, "valence": 0.4, "domain": "food", "topic": "cooking", "seed": 402},

    # Tech (4) — indices 23-26
    {"text": "Rust ownership eliminates data races without garbage collection",
     "type": "semantic", "importance": 0.6, "valence": 0.3, "domain": "tech", "topic": "programming", "seed": 500},
    {"text": "Published Python package to PyPI using flit and GitHub Actions",
     "type": "procedural", "importance": 0.55, "valence": 0.4, "domain": "tech", "topic": "publishing", "seed": 501},
    {"text": "WebAssembly runs near-native code in browsers",
     "type": "semantic", "importance": 0.5, "valence": 0.3, "domain": "tech", "topic": "programming", "seed": 502},
    {"text": "Home automation with Raspberry Pi and MQTT protocol",
     "type": "procedural", "importance": 0.5, "valence": 0.5, "domain": "tech", "topic": "hardware", "seed": 503},

    # Travel (3) — indices 27-29
    {"text": "Ancient temples of Kyoto during cherry blossom season",
     "type": "episodic", "importance": 0.8, "valence": 0.9, "domain": "travel", "topic": "asia", "seed": 600},
    {"text": "Lost in Marrakech medina, found amazing leather artisan shop",
     "type": "episodic", "importance": 0.6, "valence": 0.5, "domain": "travel", "topic": "africa", "seed": 601},
    {"text": "Northern lights in Iceland — most spectacular phenomenon ever",
     "type": "episodic", "importance": 0.85, "valence": 0.95, "domain": "travel", "topic": "europe", "seed": 602},
]

# ── Queries ──
# Some use noisy query embeddings (harder) to create baseline failures
# that learning should be able to fix.
QUERIES = [
    # Easy: precise domain+topic match
    {"query": "Sarah's recommendation engine presentation",
     "domain": "work", "topic": "ml", "noise": None, "expected_idx": [0]},
    {"query": "family celebrations and parties",
     "domain": "family", "topic": "celebration", "noise": None, "expected_idx": [8, 10, 14]},

    # Medium: some noise in query embedding
    {"query": "health issues and doctor recommendations",
     "domain": "health", "topic": "medical", "noise": (700, 0.15), "expected_idx": [16, 18]},
    {"query": "how to publish Python packages",
     "domain": "tech", "topic": "publishing", "noise": (701, 0.15), "expected_idx": [24]},

    # Hard: noisy query + high-importance distractors should interfere
    {"query": "CI/CD failures and database problems at work",
     "domain": "work", "topic": "cicd", "noise": (702, 0.20), "expected_idx": [2, 6]},
    {"query": "ML algorithms and vector search",
     "domain": "work", "topic": "hnsw", "noise": (703, 0.20), "expected_idx": [5]},
    {"query": "cooking recipes and techniques",
     "domain": "food", "topic": "cooking", "noise": (704, 0.15), "expected_idx": [21, 22]},
    {"query": "travel to Asia and temples",
     "domain": "travel", "topic": "asia", "noise": (705, 0.15), "expected_idx": [27]},

    # Harder: noisy + cross-domain pull
    {"query": "work performance and review feedback",
     "domain": "work", "topic": "review", "noise": (706, 0.25), "expected_idx": [4]},
    {"query": "ML pipeline improvements and engagement metrics",
     "domain": "work", "topic": "ml", "noise": (707, 0.20), "expected_idx": [1]},
    {"query": "exercise routines and fitness goals",
     "domain": "health", "topic": "exercise", "noise": (708, 0.20), "expected_idx": [15, 17]},
    {"query": "systems programming concepts and languages",
     "domain": "tech", "topic": "programming", "noise": (709, 0.20), "expected_idx": [23, 25]},

    # Family-specific (should not have work distractors)
    {"query": "nephew and children milestones",
     "domain": "family", "topic": "children", "noise": (710, 0.15), "expected_idx": [12, 13]},
    {"query": "pair programming sessions",
     "domain": "work", "topic": "pairing", "noise": (711, 0.20), "expected_idx": [7]},
    {"query": "Dad's surgery and recovery",
     "domain": "family", "topic": "health_update", "noise": (712, 0.15), "expected_idx": [9]},
    {"query": "API caching strategies",
     "domain": "work", "topic": "caching", "noise": (713, 0.25), "expected_idx": [3]},
    {"query": "DIY hardware projects at home",
     "domain": "tech", "topic": "hardware", "noise": (714, 0.20), "expected_idx": [26]},
    {"query": "Marrakech and African travel adventures",
     "domain": "travel", "topic": "africa", "noise": (715, 0.15), "expected_idx": [28]},
    {"query": "teaching kids to ride bikes and play",
     "domain": "family", "topic": "children", "noise": (716, 0.20), "expected_idx": [13]},
    {"query": "holiday arguments and family tension",
     "domain": "family", "topic": "conflict", "noise": (717, 0.20), "expected_idx": [11]},
]


def create_db_and_store():
    db = YantrikDB(":memory:", embedding_dim=DIM)
    rids = []
    for m in MEMORIES:
        emb = make_emb(m["domain"], m["topic"], m["seed"])
        rid = db.record(
            text=m["text"],
            embedding=emb,
            memory_type=m["type"],
            importance=m["importance"],
            valence=m["valence"],
            domain=m["domain"],
        )
        rids.append(rid)
    return db, rids


def run_queries(db, rids):
    results = []
    for q in QUERIES:
        noise = q.get("noise")
        if noise:
            emb = make_query_emb(q["domain"], q["topic"], noise_seed=noise[0], noise_scale=noise[1])
        else:
            emb = make_query_emb(q["domain"], q["topic"])

        recalled = db.recall(query_embedding=emb, top_k=5, query=q["query"])
        recalled_rids = [r["rid"] for r in recalled]
        recalled_scores = [(r["rid"], r["score"]) for r in recalled]

        expected_rids = {rids[i] for i in q["expected_idx"]}
        found = sum(1 for r in recalled_rids if r in expected_rids)
        recall_score = found / len(expected_rids) if expected_rids else 0.0

        mrr = 0.0
        for rank, rid in enumerate(recalled_rids, 1):
            if rid in expected_rids:
                mrr = 1.0 / rank
                break

        passed = recall_score >= 0.5
        results.append((passed, recall_score, mrr, recalled_scores))
    return results


def provide_feedback(db, rids):
    """Provide feedback: mark expected as relevant, others as irrelevant."""
    for q in QUERIES:
        noise = q.get("noise")
        if noise:
            emb = make_query_emb(q["domain"], q["topic"], noise_seed=noise[0], noise_scale=noise[1])
        else:
            emb = make_query_emb(q["domain"], q["topic"])

        recalled = db.recall(query_embedding=emb, top_k=5, query=q["query"])
        expected_rids = {rids[i] for i in q["expected_idx"]}

        for rank, r in enumerate(recalled):
            fb = "relevant" if r["rid"] in expected_rids else "irrelevant"
            db.recall_feedback(
                query_text=q["query"],
                query_embedding=emb,
                rid=r["rid"],
                feedback=fb,
                score_at_retrieval=r["score"],
                rank_at_retrieval=rank,
            )


def run_learning_generations(db, n_generations=20):
    for _ in range(n_generations):
        db.think({"run_consolidation": False, "run_conflict_scan": False, "run_pattern_mining": False})


def print_results(label, results, rids):
    passed = sum(1 for r in results if r[0])
    total = len(results)
    mean_recall = np.mean([r[1] for r in results])
    mean_mrr = np.mean([r[2] for r in results])
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  Pass rate:   {passed}/{total} ({100*passed/total:.0f}%)")
    print(f"  Mean recall: {mean_recall:.3f}")
    print(f"  Mean MRR:    {mean_mrr:.3f}")

    for qi, (p, recall_score, mrr, scores) in enumerate(results):
        status = "PASS" if p else "FAIL"
        q_short = QUERIES[qi]["query"][:45]
        # Show which memory indices were returned
        returned_idx = []
        for rid, _ in scores[:3]:
            for mi, m_rid in enumerate(rids):
                if m_rid == rid:
                    returned_idx.append(mi)
                    break
        expected = QUERIES[qi]["expected_idx"]
        print(f"  [{status}] Q{qi+1}: rec={recall_score:.2f} mrr={mrr:.2f}  "
              f"exp={expected} got={returned_idx}  {q_short}")


def main():
    print("Creating DB and storing 30 memories (structured embeddings)...")
    db, rids = create_db_and_store()

    print("\n--- BASELINE ---")
    baseline = run_queries(db, rids)
    print_results("BASELINE (default weights)", baseline, rids)

    w = db.learned_weights()
    print(f"\n  Weights: w_sim={w['w_sim']:.3f} w_decay={w['w_decay']:.3f} "
          f"w_recency={w['w_recency']:.3f} gate_tau={w['gate_tau']:.3f} "
          f"alpha_imp={w['alpha_imp']:.3f} kw={w['keyword_boost']:.3f}")

    print("\n--- PROVIDING FEEDBACK ---")
    provide_feedback(db, rids)

    print("\n--- RUNNING 20 LEARNING GENERATIONS ---")
    run_learning_generations(db, 20)

    w = db.learned_weights()
    print(f"  Learned: w_sim={w['w_sim']:.3f} w_decay={w['w_decay']:.3f} "
          f"w_recency={w['w_recency']:.3f} gate_tau={w['gate_tau']:.3f} "
          f"alpha_imp={w['alpha_imp']:.3f} kw={w['keyword_boost']:.3f} gen={w['generation']}")

    print("\n--- POST-LEARNING ---")
    post = run_queries(db, rids)
    print_results("POST-LEARNING (adaptive weights)", post, rids)

    # Comparison
    bp = sum(1 for r in baseline if r[0])
    pp = sum(1 for r in post if r[0])
    br = np.mean([r[1] for r in baseline])
    pr = np.mean([r[1] for r in post])
    bm = np.mean([r[2] for r in baseline])
    pm = np.mean([r[2] for r in post])

    print(f"\n{'='*60}")
    print(f"  COMPARISON")
    print(f"{'='*60}")
    delta_p = pp - bp
    delta_r = pr - br
    delta_m = pm - bm
    print(f"  Pass rate: {bp} → {pp} ({'+' if delta_p >= 0 else ''}{delta_p})")
    print(f"  Recall:    {br:.3f} → {pr:.3f} ({'+' if delta_r >= 0 else ''}{delta_r:.3f})")
    print(f"  MRR:       {bm:.3f} → {pm:.3f} ({'+' if delta_m >= 0 else ''}{delta_m:.3f})")

    changes = []
    for qi in range(len(QUERIES)):
        b = baseline[qi][1]
        p = post[qi][1]
        if abs(b - p) > 0.001:
            changes.append((qi, b, p))

    if changes:
        print(f"\n  Per-query changes:")
        for qi, b, p in changes:
            direction = "UP" if p > b else "DOWN"
            q_short = QUERIES[qi]["query"][:45]
            print(f"    Q{qi+1}: {b:.2f} → {p:.2f} ({direction})  {q_short}")
    else:
        print(f"\n  No per-query changes detected.")

    # Verdict
    if delta_p > 0:
        print(f"\n  VERDICT: IMPROVED pass rate ({bp} → {pp})")
    elif delta_p == 0 and delta_r > 0.01:
        print(f"\n  VERDICT: IMPROVED recall ({br:.3f} → {pr:.3f})")
    elif delta_p == 0 and delta_m > 0.01:
        print(f"\n  VERDICT: IMPROVED ranking ({bm:.3f} → {pm:.3f})")
    elif delta_p == 0 and abs(delta_r) <= 0.01:
        print(f"\n  VERDICT: NEUTRAL (no meaningful change)")
    elif delta_p < 0:
        print(f"\n  VERDICT: REGRESSED ({bp} → {pp})")
    else:
        print(f"\n  VERDICT: MIXED")


if __name__ == "__main__":
    main()
