"""FastMCP server definition with AIDB lifespan context."""

import logging
import os
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Configure logging to stderr (stdout is reserved for MCP JSON-RPC)
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("aidb.mcp")


@asynccontextmanager
async def lifespan(app: FastMCP):
    """Initialize AIDB and embedder on server start, clean up on shutdown."""
    from sentence_transformers import SentenceTransformer

    from aidb import AIDB

    db_path = os.environ.get("AIDB_DB_PATH", str(Path.home() / ".aidb" / "memory.db"))
    model_name = os.environ.get("AIDB_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    embedding_dim = int(os.environ.get("AIDB_EMBEDDING_DIM", "384"))

    # Ensure parent directory exists
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    log.info("Loading embedding model: %s", model_name)
    embedder = SentenceTransformer(model_name)

    log.info("Opening AIDB at: %s (dim=%d)", db_path, embedding_dim)
    db = AIDB(db_path=db_path, embedding_dim=embedding_dim, embedder=embedder)

    # Lock to serialize access to the unsendable PyAIDB
    lock = threading.Lock()

    try:
        yield {"db": db, "lock": lock}
    finally:
        log.info("Shutting down AIDB")
        db.close()


mcp = FastMCP("aidb", lifespan=lifespan)

# Import tools and resources so they register with the server
from . import resources as _resources  # noqa: F401, E402
from . import tools as _tools  # noqa: F401, E402
