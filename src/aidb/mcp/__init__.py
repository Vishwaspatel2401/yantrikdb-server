"""AIDB MCP Server — expose the cognitive memory engine as MCP tools."""

from .server import mcp


def main():
    """Entry point for the aidb-mcp console script."""
    mcp.run(transport="stdio")
