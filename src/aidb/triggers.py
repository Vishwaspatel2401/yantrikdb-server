"""AIDB Proactive Triggers — give AI genuine reasons to initiate conversation.

Trigger types:
  - decay_review: Important memory decaying — "Should we still remember X?"
  - conflict_detected: Contradictory memories found — "You said X, but also Y"
  - consolidation_ready: Cluster ripe for merging — "I noticed several similar memories"
"""

import math
import time
from dataclasses import dataclass

from aidb.engine import AIDB


@dataclass
class Trigger:
    """A proactive trigger — a reason for the AI to initiate conversation."""

    trigger_type: str  # decay_review | conflict_detected | consolidation_ready
    reason: str  # Human-readable explanation
    urgency: float  # [0, 1] — how urgent is this trigger
    source_rids: list[str]  # Memory RIDs involved
    suggested_action: str  # What the AI should do
    context: dict  # Additional context for the AI


def check_decay_triggers(
    db: AIDB,
    importance_threshold: float = 0.5,
    decay_threshold: float = 0.1,
    max_triggers: int = 5,
) -> list[Trigger]:
    """Find important memories that are decaying significantly.

    Triggers when:
      - Original importance >= importance_threshold (it was important)
      - Current effective score < decay_threshold (it's fading)

    This gives the AI a reason to say: "Hey, you mentioned X a while ago —
    is that still relevant? Should I keep remembering it?"

    Args:
        db: AIDB instance.
        importance_threshold: Minimum original importance to trigger on.
        decay_threshold: Maximum current score to trigger on.
        max_triggers: Maximum number of triggers to return.

    Returns:
        List of Trigger objects, sorted by urgency (highest first).
    """
    now = time.time()
    rows = db._conn.execute(
        """SELECT rid, text, type, importance, half_life, last_access, valence
           FROM memories
           WHERE consolidation_status = 'active'
           AND importance >= ?""",
        (importance_threshold,),
    ).fetchall()

    triggers = []
    for row in rows:
        t = now - row["last_access"]
        half_life = row["half_life"]
        current_score = row["importance"] * math.pow(2, -t / half_life) if half_life > 0 else 0

        if current_score < decay_threshold:
            days_since = t / 86400
            decay_ratio = current_score / row["importance"] if row["importance"] > 0 else 0

            # Urgency: higher for more important memories that decayed more
            urgency = row["importance"] * (1.0 - decay_ratio)

            triggers.append(Trigger(
                trigger_type="decay_review",
                reason=(
                    f"Important memory (importance={row['importance']:.1f}) "
                    f"has decayed to {current_score:.3f} after {days_since:.0f} days"
                ),
                urgency=urgency,
                source_rids=[row["rid"]],
                suggested_action="ask_user_to_confirm_or_forget",
                context={
                    "text": row["text"],
                    "type": row["type"],
                    "original_importance": row["importance"],
                    "current_score": current_score,
                    "days_since_access": days_since,
                    "valence": row["valence"],
                },
            ))

    # Sort by urgency, return top N
    triggers.sort(key=lambda t: t.urgency, reverse=True)
    return triggers[:max_triggers]


def check_consolidation_triggers(
    db: AIDB,
    min_active_memories: int = 10,
) -> list[Trigger]:
    """Trigger when there are enough active memories that consolidation might help.

    Simple heuristic: if active memories exceed a threshold, suggest running
    consolidation. More sophisticated versions would check cluster density.
    """
    stats = db.stats()
    triggers = []

    if stats["active_memories"] >= min_active_memories:
        # Check if there are memories that haven't been consolidated
        unconsolidated = db._conn.execute(
            """SELECT COUNT(*) as c FROM memories
               WHERE consolidation_status = 'active'
               AND type = 'episodic'"""
        ).fetchone()["c"]

        if unconsolidated >= min_active_memories:
            triggers.append(Trigger(
                trigger_type="consolidation_ready",
                reason=f"{unconsolidated} episodic memories could be consolidated",
                urgency=min(1.0, unconsolidated / 50.0),  # Urgency scales with count
                source_rids=[],
                suggested_action="run_consolidation",
                context={
                    "episodic_count": unconsolidated,
                    "total_active": stats["active_memories"],
                },
            ))

    return triggers


def check_all_triggers(
    db: AIDB,
    importance_threshold: float = 0.5,
    decay_threshold: float = 0.1,
    max_triggers: int = 5,
) -> list[Trigger]:
    """Run all trigger checks and return a unified, priority-sorted list."""
    triggers = []
    triggers.extend(check_decay_triggers(db, importance_threshold, decay_threshold, max_triggers))
    triggers.extend(check_consolidation_triggers(db))

    # Sort all triggers by urgency
    triggers.sort(key=lambda t: t.urgency, reverse=True)
    return triggers[:max_triggers]
