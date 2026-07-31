"""Console encoding, in one place.

Real payloads carry client names, message bodies, and emoji; this project's own output uses
arrows and em-dashes. On a console running a legacy codepage — cp1252 is the Windows default
— printing any of those raises ``UnicodeEncodeError`` and the process dies mid-output.

Phase 06 fixed this once, inside ``cli/main.py``. That was the wrong home: being able to
print non-ASCII is a property of the *process*, not of the CLI, so every other entry point —
each ``scripts/verify_*.py`` harness — still carried the same latent crash, and phase 08's
harness duly hit it on a ``→``. Putting the fix here means an entry point opts in with one
import instead of re-deriving it, and there is one implementation to get right.
"""

from __future__ import annotations

import sys

__all__ = ["configure_streams"]


def configure_streams() -> None:
    """Make ``stdout``/``stderr`` able to carry any text this project produces.

    UTF-8 with ``backslashreplace``: a character the terminal genuinely cannot render is
    shown escaped rather than killing the run. Idempotent, and safe on streams that do not
    support reconfiguration (a pipe under some runners, a captured buffer in tests).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (ValueError, OSError):  # pragma: no cover - exotic stream
            pass
