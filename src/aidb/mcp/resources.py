"""MCP resource definitions for AIDB."""

import json

from mcp.server.fastmcp import Context

from .server import mcp


@mcp.resource("aidb://stats")
def stats_resource(ctx: Context = None) -> str:
    """Current AIDB engine statistics."""
    lc = ctx.request_context.lifespan_context
    db, lock = lc["db"], lc["lock"]
    with lock:
        stats = db.stats()
    return json.dumps(stats, indent=2)


@mcp.resource("aidb://memory/{rid}")
def memory_resource(rid: str, ctx: Context = None) -> str:
    """A specific memory record."""
    lc = ctx.request_context.lifespan_context
    db, lock = lc["db"], lc["lock"]
    with lock:
        mem = db.get(rid)
    if mem is None:
        return json.dumps({"error": "Memory not found", "rid": rid})
    return json.dumps(mem, indent=2)
