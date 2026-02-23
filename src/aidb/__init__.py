"""AIDB — A Cognitive Memory Engine for Persistent AI Systems."""

from aidb.consolidate import consolidate
from aidb.engine import AIDB
from aidb.triggers import check_all_triggers

__version__ = "0.1.0"
__all__ = ["AIDB", "consolidate", "check_all_triggers"]
