"""Closewire — a paced Python client for the Closebot API.

Wraps two distinct Closebot surfaces (see ``RESEARCH.md``):

* **REST management API** — ``https://api.closebot.com`` (``X-CB-KEY`` auth). Configure
  and read bots, personas, sources, leads, metrics, testing, billing. See
  :mod:`closewire_client.rest` and :mod:`closewire_client.endpoints`.
* **Live message API** — ``https://api.closebot.ai/message`` (runtime). See
  :mod:`closewire_client.live`.

Every call on either surface passes through :class:`~closewire_client.pacing.Pacer` —
jittered think-time, serial writes, hourly ceilings, backoff, and a circuit breaker.
There is no unpaced path.
"""

from __future__ import annotations

from closewire_client.config import (
    Config,
    ConfigError,
    MissingConfigError,
    load_config,
    redact_secret,
)
from closewire_client.errors import (
    ClosebotAPIError,
    ClosewireError,
    ClosewireTransportError,
)
from closewire_client.pacing import (
    NestedSlotError,
    Pacer,
    PacerStats,
    PacingBypassError,
    PacingHalt,
)
from closewire_client import tiers as tiers
from closewire_client.tiers import Tier2FieldBlocked, Tier2RouteBlocked

# Install the route-tier guard over the generated endpoint package **here**, before any
# `closewire_client.endpoints.*` module can be imported — Python runs a parent package's
# __init__ first, and this file is one codegen never rewrites. See closewire_client/tiers.py
# for why the enforcement cannot live inside endpoints/ itself.
tiers.install()

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "Config",
    "ConfigError",
    "MissingConfigError",
    "load_config",
    "redact_secret",
    "ClosewireError",
    "ClosewireTransportError",
    "ClosebotAPIError",
    "Pacer",
    "PacerStats",
    "PacingHalt",
    "PacingBypassError",
    "NestedSlotError",
    "tiers",
    "Tier2RouteBlocked",
    "Tier2FieldBlocked",
]
