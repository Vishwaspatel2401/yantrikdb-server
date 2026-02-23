"""Synthetic conversation data for evaluation.

Simulates multi-session agent interactions spanning days/weeks.
Each session has a theme, and memories reference entities that
overlap across sessions to test graph-aware retrieval.
"""

import random
import time

# ── Session Templates ────────────────────────────────────
# Each session = a list of (text, memory_type, importance, valence, entities)
# Entities are (src, dst, rel_type) tuples for graph construction.

SESSIONS = [
    {
        "name": "session_1_project_kickoff",
        "description": "User discusses starting a new ML project with their team",
        "time_offset_days": 0,
        "memories": [
            {
                "text": "We decided to use PyTorch for the new recommendation engine project",
                "type": "episodic",
                "importance": 0.8,
                "valence": 0.3,
                "entities": [("PyTorch", "recommendation engine", "used_in")],
            },
            {
                "text": "Sarah will lead the data pipeline work, she has experience with Spark",
                "type": "episodic",
                "importance": 0.7,
                "valence": 0.2,
                "entities": [
                    ("Sarah", "data pipeline", "leads"),
                    ("Sarah", "Spark", "experienced_with"),
                ],
            },
            {
                "text": "The project deadline is set for March 15th, which gives us about 8 weeks",
                "type": "episodic",
                "importance": 0.9,
                "valence": -0.1,
                "entities": [("recommendation engine", "March 15th", "deadline")],
            },
            {
                "text": "We need at least 1 million user interactions for training data",
                "type": "semantic",
                "importance": 0.6,
                "valence": 0.0,
                "entities": [("recommendation engine", "training data", "requires")],
            },
            {
                "text": "Mike suggested using collaborative filtering as the baseline approach",
                "type": "episodic",
                "importance": 0.5,
                "valence": 0.1,
                "entities": [
                    ("Mike", "collaborative filtering", "suggested"),
                    ("collaborative filtering", "recommendation engine", "approach_for"),
                ],
            },
        ],
    },
    {
        "name": "session_2_debugging",
        "description": "User runs into issues with data processing and gets frustrated",
        "time_offset_days": 3,
        "memories": [
            {
                "text": "The Spark job keeps failing on the user interaction dataset, out of memory errors",
                "type": "episodic",
                "importance": 0.7,
                "valence": -0.6,
                "entities": [
                    ("Spark", "user interaction dataset", "processing"),
                    ("Spark", "OOM error", "has_issue"),
                ],
            },
            {
                "text": "Sarah found that the dataset has duplicate entries causing the memory spike",
                "type": "episodic",
                "importance": 0.8,
                "valence": 0.4,
                "entities": [
                    ("Sarah", "dataset duplicates", "discovered"),
                    ("dataset duplicates", "OOM error", "causes"),
                ],
            },
            {
                "text": "After deduplication we went from 5 million to 1.2 million clean records",
                "type": "semantic",
                "importance": 0.7,
                "valence": 0.3,
                "entities": [("user interaction dataset", "1.2 million records", "cleaned_to")],
            },
            {
                "text": "I learned that Spark's default partition size is too small for our data volume",
                "type": "semantic",
                "importance": 0.5,
                "valence": 0.0,
                "entities": [("Spark", "partition size", "config_issue")],
            },
        ],
    },
    {
        "name": "session_3_architecture",
        "description": "Team discusses model architecture choices",
        "time_offset_days": 7,
        "memories": [
            {
                "text": "We decided to go with a two-tower model architecture instead of collaborative filtering",
                "type": "episodic",
                "importance": 0.9,
                "valence": 0.4,
                "entities": [
                    ("two-tower model", "recommendation engine", "architecture_for"),
                    ("two-tower model", "collaborative filtering", "replaces"),
                ],
            },
            {
                "text": "The user tower encodes browsing history and the item tower encodes product features",
                "type": "semantic",
                "importance": 0.7,
                "valence": 0.0,
                "entities": [
                    ("user tower", "browsing history", "encodes"),
                    ("item tower", "product features", "encodes"),
                ],
            },
            {
                "text": "Mike is concerned about serving latency, we need sub-100ms inference",
                "type": "episodic",
                "importance": 0.6,
                "valence": -0.3,
                "entities": [
                    ("Mike", "serving latency", "concerned_about"),
                    ("recommendation engine", "100ms", "latency_target"),
                ],
            },
            {
                "text": "We'll use FAISS for approximate nearest neighbor search in production",
                "type": "episodic",
                "importance": 0.7,
                "valence": 0.2,
                "entities": [("FAISS", "recommendation engine", "used_in")],
            },
        ],
    },
    {
        "name": "session_4_personal",
        "description": "User shares personal preferences and context",
        "time_offset_days": 10,
        "memories": [
            {
                "text": "I prefer writing code in the morning and doing reviews in the afternoon",
                "type": "procedural",
                "importance": 0.4,
                "valence": 0.2,
                "entities": [],
            },
            {
                "text": "My daughter's school play is on March 10th, I need to block that afternoon",
                "type": "episodic",
                "importance": 0.8,
                "valence": 0.7,
                "entities": [("daughter", "school play", "performing_in")],
            },
            {
                "text": "I find that pair programming with Sarah is very productive",
                "type": "semantic",
                "importance": 0.5,
                "valence": 0.5,
                "entities": [("Sarah", "pair programming", "productive_with")],
            },
            {
                "text": "Coffee after lunch makes me more focused for the afternoon deep work",
                "type": "procedural",
                "importance": 0.3,
                "valence": 0.3,
                "entities": [],
            },
        ],
    },
    {
        "name": "session_5_progress_update",
        "description": "Mid-project check-in with progress and concerns",
        "time_offset_days": 14,
        "memories": [
            {
                "text": "The two-tower model is showing 15% improvement over the collaborative filtering baseline",
                "type": "semantic",
                "importance": 0.9,
                "valence": 0.6,
                "entities": [
                    ("two-tower model", "15% improvement", "achieves"),
                    ("two-tower model", "collaborative filtering", "outperforms"),
                ],
            },
            {
                "text": "Sarah's data pipeline is now processing all 1.2 million records in under 10 minutes",
                "type": "episodic",
                "importance": 0.6,
                "valence": 0.4,
                "entities": [("Sarah", "data pipeline", "completed")],
            },
            {
                "text": "We're behind on the A/B testing infrastructure, might need to push the deadline",
                "type": "episodic",
                "importance": 0.8,
                "valence": -0.5,
                "entities": [
                    ("A/B testing", "recommendation engine", "needed_for"),
                    ("March 15th", "deadline", "at_risk"),
                ],
            },
            {
                "text": "Mike built a model serving prototype with ONNX Runtime, hitting 45ms latency",
                "type": "episodic",
                "importance": 0.7,
                "valence": 0.5,
                "entities": [
                    ("Mike", "ONNX Runtime", "built_with"),
                    ("ONNX Runtime", "45ms latency", "achieves"),
                ],
            },
        ],
    },
    {
        "name": "session_6_conflict",
        "description": "Contradictory information and updated decisions",
        "time_offset_days": 18,
        "memories": [
            {
                "text": "Actually we changed the deadline to March 22nd to allow time for A/B testing",
                "type": "episodic",
                "importance": 0.9,
                "valence": 0.2,
                "entities": [("recommendation engine", "March 22nd", "new_deadline")],
            },
            {
                "text": "Sarah is moving to the search team next month, we need to document the pipeline",
                "type": "episodic",
                "importance": 0.8,
                "valence": -0.4,
                "entities": [
                    ("Sarah", "search team", "moving_to"),
                    ("data pipeline", "documentation", "needs"),
                ],
            },
            {
                "text": "We switched from FAISS to ScaNN because it handles our data distribution better",
                "type": "episodic",
                "importance": 0.7,
                "valence": 0.1,
                "entities": [
                    ("ScaNN", "FAISS", "replaces"),
                    ("ScaNN", "recommendation engine", "used_in"),
                ],
            },
        ],
    },
]


# ── Golden Queries ───────────────────────────────────────
# Each query has: text, expected RIDs by semantic match, and tags for what
# capabilities are being tested (vector, temporal, graph, decay, conflict).

GOLDEN_QUERIES = [
    {
        "id": "q1_direct_semantic",
        "query": "What framework are we using for the recommendation engine?",
        "expected_texts": [
            "We decided to use PyTorch for the new recommendation engine project",
            "We decided to go with a two-tower model architecture instead of collaborative filtering",
        ],
        "test_tags": ["semantic"],
        "description": "Direct semantic match — should find framework decisions",
    },
    {
        "id": "q2_entity_graph",
        "query": "What is Sarah working on?",
        "expected_texts": [
            "Sarah will lead the data pipeline work, she has experience with Spark",
            "Sarah found that the dataset has duplicate entries causing the memory spike",
            "Sarah's data pipeline is now processing all 1.2 million records in under 10 minutes",
            "Sarah is moving to the search team next month, we need to document the pipeline",
            "I find that pair programming with Sarah is very productive",
        ],
        "test_tags": ["semantic", "graph"],
        "description": "Entity-centric query — should find all Sarah-related memories",
    },
    {
        "id": "q3_temporal",
        "query": "What happened in our last meeting?",
        "expected_texts": [
            "Actually we changed the deadline to March 22nd to allow time for A/B testing",
            "Sarah is moving to the search team next month, we need to document the pipeline",
            "We switched from FAISS to ScaNN because it handles our data distribution better",
        ],
        "test_tags": ["temporal"],
        "description": "Recency query — should favor most recent session",
    },
    {
        "id": "q4_emotional",
        "query": "What was I frustrated about?",
        "expected_texts": [
            "The Spark job keeps failing on the user interaction dataset, out of memory errors",
            "We're behind on the A/B testing infrastructure, might need to push the deadline",
        ],
        "test_tags": ["valence"],
        "description": "Emotional query — should find negatively valenced memories",
    },
    {
        "id": "q5_conflict",
        "query": "When is the project deadline?",
        "expected_texts": [
            "Actually we changed the deadline to March 22nd to allow time for A/B testing",
            "The project deadline is set for March 15th, which gives us about 8 weeks",
        ],
        "test_tags": ["conflict", "temporal"],
        "description": "Conflict detection — deadline changed, latest should rank first",
    },
    {
        "id": "q6_procedural",
        "query": "What are my work habits and preferences?",
        "expected_texts": [
            "I prefer writing code in the morning and doing reviews in the afternoon",
            "Coffee after lunch makes me more focused for the afternoon deep work",
            "I find that pair programming with Sarah is very productive",
        ],
        "test_tags": ["semantic", "type_filter"],
        "description": "Procedural memory query — personal preferences and habits",
    },
    {
        "id": "q7_multi_hop",
        "query": "What problems did we have with the data and how were they resolved?",
        "expected_texts": [
            "The Spark job keeps failing on the user interaction dataset, out of memory errors",
            "Sarah found that the dataset has duplicate entries causing the memory spike",
            "After deduplication we went from 5 million to 1.2 million clean records",
            "I learned that Spark's default partition size is too small for our data volume",
        ],
        "test_tags": ["semantic", "graph"],
        "description": "Multi-hop reasoning — problem -> cause -> resolution chain",
    },
    {
        "id": "q8_performance",
        "query": "How is the model performing?",
        "expected_texts": [
            "The two-tower model is showing 15% improvement over the collaborative filtering baseline",
            "Mike built a model serving prototype with ONNX Runtime, hitting 45ms latency",
        ],
        "test_tags": ["semantic"],
        "description": "Performance metrics — should find quantitative results",
    },
    {
        "id": "q9_personal",
        "query": "Do I have any personal events coming up?",
        "expected_texts": [
            "My daughter's school play is on March 10th, I need to block that afternoon",
        ],
        "test_tags": ["semantic", "valence"],
        "description": "Personal life — high valence personal memory",
    },
    {
        "id": "q10_technology_evolution",
        "query": "What vector search technology are we using?",
        "expected_texts": [
            "We switched from FAISS to ScaNN because it handles our data distribution better",
            "We'll use FAISS for approximate nearest neighbor search in production",
        ],
        "test_tags": ["conflict", "temporal"],
        "description": "Technology change — ScaNN replaced FAISS, should rank latest first",
    },
]


def load_sessions_into_db(db, embedder=None, base_time: float | None = None):
    """Load all synthetic sessions into an AIDB instance.

    Args:
        db: AIDB instance.
        embedder: Optional SentenceTransformer. If None, must use pre-computed embeddings.
        base_time: Base unix timestamp for session timing. Defaults to 21 days ago.

    Returns:
        Dict mapping memory text -> rid for evaluation lookups.
    """
    if base_time is None:
        base_time = time.time() - (21 * 86400)  # 21 days ago

    text_to_rid = {}

    for session in SESSIONS:
        session_time = base_time + (session["time_offset_days"] * 86400)

        for i, mem in enumerate(session["memories"]):
            # Generate embedding if embedder available
            embedding = None
            if embedder is not None:
                vec = embedder.encode(mem["text"])
                embedding = vec.tolist() if hasattr(vec, "tolist") else list(vec)

            rid = db.record(
                text=mem["text"],
                memory_type=mem["type"],
                importance=mem["importance"],
                valence=mem["valence"],
                embedding=embedding,
            )

            text_to_rid[mem["text"]] = rid

            # Backdate created_at and last_access to simulate time passage
            db._conn.execute(
                "UPDATE memories SET created_at = ?, updated_at = ?, last_access = ? WHERE rid = ?",
                (session_time + i, session_time + i, session_time + i, rid),
            )

            # Create entity relationships
            for src, dst, rel_type in mem["entities"]:
                db.relate(src, dst, rel_type=rel_type)

    db._conn.commit()
    return text_to_rid
