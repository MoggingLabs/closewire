"""Tier-1 writes — non-destructive mutations.

Separate from :mod:`closewire_client.endpoints` **on purpose**. Here the tier is visible in
the import path, so:

* a reviewer can see at the import line whether a module mutates;
* phase 11 can gate MCP tools on the package a function came from rather than on a name;
* the read-only property stays checkable — see below for exactly what it covers.

**What "read-only" does and does not mean here**, stated precisely because an earlier
revision of this docstring overstated it. ``endpoints/`` is *not* wholly free of mutating
verbs: it also holds the **generated** wrappers for the full API surface, and those include
``POST``/``DELETE`` calls (e.g. ``endpoints/bot.py`` wraps ``DELETE /bot/{id}``). The
guarantee phase 05 actually established, and that still holds, is narrower and worth
stating exactly:

* the six **curated** read modules (``_reads``, ``bots``, ``personas``, ``sources``,
  ``leads``, ``metrics``) contain exactly one mutating verb — ``leads.search``, a POST whose
  semantics are a pure read, which is why it is the sole entry in ``rest.READ_ONLY_POSTS``;
* nothing in ``endpoints/`` or in the **read** CLI (``cli/reads.py``) imports this
  package, so no read path can reach a Tier-1 write. ``cli/testing.py`` does import it,
  deliberately — it is a Tier-1 command surface, not a read one. An earlier revision of
  this line said "or ``cli/``" flatly, which phase 09 falsified;
* the CLI's read commands route only through those curated modules.

Nothing here publishes a bot, deletes a record the account cannot reconstruct, or refills a
wallet — those are Tier-2, in :mod:`closewire_client.tier2`.

**Most of this package rides the Pacer's serial write lane**, charged to the write budget and
suppressed by ``CLOSEWIRE_DRY_RUN``. Two functions do not, and an earlier revision of this
paragraph claimed "everything" did: :func:`testing.list_sessions` and
:func:`testing.get_messages` are GETs, so they take the **read** lane and dry-run does
not suppress them. That is correct for reads — an operator who sets ``CLOSEWIRE_DRY_RUN`` and
runs ``closewire test show`` still puts a real GET on the wire, and should.

**Two qualifications, since phase 09 added** :mod:`~closewire_client.writes.testing`
**and an earlier revision of this line said "nothing here deletes or spends":**

* ``testing.send`` **does spend credits** — it makes a bot generate a reply. It is Tier-1
  because it drives a *synthetic* QA conversation the caller just created, not a real
  contact, and because the money it moves is per-message credit rather than a wallet
  operation. It is still the only thing in this package that costs anything, and it is
  called out in its own docstring.
* ``testing.delete_session`` **does delete** — a throwaway QA session, created moments
  earlier by the same caller. Tier-2's destroy rule is deliberately scoped to root
  collections whose records cannot be reconstructed (``/bot``, ``/persona``,
  ``/agency/source``, ``/lead``, ``/account/apiKey``); a test session is none of them.
"""

from __future__ import annotations

from . import bots, personas, testing

__all__ = ["bots", "personas", "testing"]
