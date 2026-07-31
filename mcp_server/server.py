"""Runnable no-op MCP server skeleton for Closewire.

No tools are registered yet — this exists so the server can be constructed and launched
end-to-end before real tools land in phase 11. The MCP SDK is imported lazily inside
:func:`build_server` so ``import mcp_server.server`` stays clean even if the SDK is not
installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server.fastmcp import FastMCP

__all__ = ["SERVER_NAME", "build_server", "main"]

SERVER_NAME = "closewire"


def build_server() -> "FastMCP":
    """Construct the FastMCP server.

    No tools are registered at scaffold stage. Later phases will attach one tool per
    Closebot action (``bots.*``, ``personas.*``, ``leads.*``, ``metrics.*``, …), each
    backed by :mod:`closewire_client`.
    """
    from mcp.server.fastmcp import FastMCP

    server: Any = FastMCP(SERVER_NAME)
    # TODO(phase 11): register tools here from closewire_client's endpoint groups.
    return server


def main() -> None:
    """Run the Closewire MCP server over stdio (no-op until tools are registered)."""
    server = build_server()
    server.run()


if __name__ == "__main__":
    main()
