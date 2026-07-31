"""Closewire MCP server.

Wraps :mod:`closewire_client` so Claude gets one tool per Closebot action. At scaffold
stage this is a runnable no-op skeleton — no tools are registered yet (that lands in
phase 11, ``11-mcp-server``).
"""

from __future__ import annotations

from mcp_server.server import SERVER_NAME, build_server, main

__all__ = ["SERVER_NAME", "build_server", "main"]
