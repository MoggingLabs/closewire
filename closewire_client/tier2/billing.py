"""Billing: wallet balance, options, transactions, and refill.

**Refill moves real money.** Everything in this module except :func:`refill` is a read and
is safe to run. :func:`refill` is gated exactly like a delete — an explicit token that must
equal the amount — because the failure mode is not "the wrong bot" but "the wrong number of
zeroes", and a boolean cannot tell 5 from 500.

Phase 08 deliberately does **not** perform a live refill. The read side and every refusal
path are proven; the single live refill is left for the operator's sign-off. See
``docs/validation/08-tier2.md``.
"""

from __future__ import annotations

import logging
from typing import Any

from ..rest import RestClient
from ._confirm import describe_intent, require_confirm

log = logging.getLogger("closewire.tier2")

__all__ = [
    "balance",
    "options",
    "transactions",
    "refill",
    "preview_refill",
    "set_options",
    "DEFAULT_CURRENCY",
    "AMOUNT_UNIT_IS_UNKNOWN",
]

#: ``CreateRefillDto.currency`` is nullable, but sending the account's own currency is
#: safer than letting the server pick a default this client never saw.
DEFAULT_CURRENCY = "usd"

#: Whether ``refill(amount=...)`` is in major units (dollars) or minor units (cents) is
#: **undocumented**, and this client refuses to guess. `CreateRefillDto.amount` carries no
#: description; `BalanceDto.balance` — the only described money field — says "smallest unit
#: of currency (cents in USD)". An earlier revision asserted "whole units" with no basis.
#: Kept as a named constant so the warning is greppable and so :func:`preview_refill` and
#: the CLI can surface the same text rather than re-deriving it.
AMOUNT_UNIT_IS_UNKNOWN = (
    "UNIT UNKNOWN: the spec does not say whether `amount` is dollars or cents. "
    "`BalanceDto.balance` is documented as 'smallest unit of currency (cents in USD)', so "
    "amount=5 may be 5 CENTS, not $5. The confirmation token cannot catch a wrong unit — it "
    "only proves amount == confirm. Verify with the smallest possible refill first."
)


# ── Reads (no gate) ───────────────────────────────────────────────────────────
def balance(client: RestClient) -> Any:
    """Wallet balance. ``GET /agency/billing/balance``"""
    return client.request("GET", "/agency/billing/balance")


def options(client: RestClient) -> Any:
    """Billing configuration — over-billing, auto-refill, thresholds.

    ``GET /agency/billing/options``. Worth reading before any refill: if
    ``autoRefillEnabled`` is set, a manual top-up may not be what the operator wants.
    """
    return client.request("GET", "/agency/billing/options")


def transactions(client: RestClient, **params: Any) -> Any:
    """Wallet transaction history. ``GET /agency/billing/transactions``"""
    return client.request("GET", "/agency/billing/transactions", params=params or None)


# ── Writes ────────────────────────────────────────────────────────────────────
def refill(
    client: RestClient,
    amount: int,
    *,
    currency: str = DEFAULT_CURRENCY,
    confirm: Any = None,
) -> Any:
    """Top up the wallet. ``POST /agency/billing/refill`` → ``CreateRefillDto``

    **This spends real money.** ``confirm`` must equal ``amount`` exactly; ``confirm=True``
    is refused. The token is the amount rather than a generic acknowledgement because the
    realistic mistake here is an order of magnitude, and a boolean is just as true for 500
    as for 5.

    .. warning::

       **The unit of ``amount`` is NOT documented, and this client does not know it.**
       See :data:`AMOUNT_UNIT_IS_UNKNOWN`. ``CreateRefillDto.amount`` is declared
       ``{"type": "integer", "format": "int64"}`` with **no description**. The only money
       field in the billing family that *is* described is ``BalanceDto.balance`` —
       *"Balance in smallest unit of currency (cents in USD)"* — so the nearest evidence
       points at **minor units**, i.e. ``amount=5`` may well be **5 cents, not $5**.

       Nothing in this repo settles it: the account's balance is 0 with no transaction
       history, so there is no empirical answer either. It cannot be resolved without
       spending money, which is exactly what phase 08 defers to the operator.

       This matters more than it looks. The confirmation token proves ``amount == confirm``
       — it is structurally blind to a wrong *unit*, because both sides carry the same
       wrong number. An operator who trusts the spec's cents convention and passes ``500``
       for "£5" clears every guard in this module; if the field is whole units, that is a
       **$500 charge**. So: verify the unit with a smallest-possible refill before relying
       on any larger one.

    ``amount`` must be a positive integer — ``CreateRefillDto.amount`` is ``int64``, and a
    float would either be truncated server-side or rejected after the money question had
    already been asked.

    This uses :func:`require_confirm` rather than :func:`confirm_target` on purpose: by the
    time the gate runs, ``amount`` is already a validated positive ``int``, so its text form
    is exact and there is no raw-vs-canonical gap to close — and the body must carry the
    ``int`` itself, not the gate's string.
    """
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise TypeError(
            f"refill(): amount must be an int, got {type(amount).__name__}. "
            "CreateRefillDto.amount is int64; a float here is a rounding question nobody "
            "should be answering on a payment."
        )
    if amount <= 0:
        raise ValueError(f"refill(): amount must be positive, got {amount}")

    require_confirm("REFILL WALLET", amount, confirm)
    log.warning(
        "tier2: REFILLING WALLET with %s %s — this spends real money. %s",
        amount, currency, AMOUNT_UNIT_IS_UNKNOWN,
    )
    return client.request(
        "POST", "/agency/billing/refill", json={"amount": amount, "currency": currency}
    )


def set_options(client: RestClient, *, confirm: Any = None, **fields: Any) -> Any:
    """Change billing configuration. ``PUT /agency/billing/options``

    Gated even though it moves no money directly: enabling ``autoRefillEnabled`` arms
    *recurring* spending without further confirmation, which is a larger commitment than the
    single top-up :func:`refill` performs.

    **The token is the change set — fields *and* values** — rendered as ``field=value``
    pairs, comma-joined, fields in sorted order::

        set_options(client, autoRefillEnabled=True, topUpAmount=250,
                    confirm="autoRefillEnabled=True,topUpAmount=250")

    Naming only the fields would repeat, on the one call that arms recurring spending,
    exactly the mistake :func:`refill` refuses to allow on a single payment: ``topUpAmount``
    is equally true of 250 and of 250000, so a field-name token cannot catch the wrong
    number of zeroes, and it cannot tell enabling a flag from disabling it either. The
    payload sent is built from the same dict the token was derived from, so what was
    confirmed and what is sent cannot differ.

    Raises:
        ValueError: If no fields are given, or any field is not in :data:`OPTION_FIELDS`.
        ConfirmationRequired: Unless ``confirm`` matches the rendered change set.
    """
    if not fields:
        raise ValueError("set_options() needs at least one field to change")
    unknown = sorted(set(fields) - OPTION_FIELDS)
    if unknown:
        raise ValueError(
            f"set_options(): unknown field(s) {unknown} — UpdateBillingConfigInput accepts "
            f"{sorted(OPTION_FIELDS)}"
        )
    changes = {name: fields[name] for name in sorted(fields)}
    target = ",".join(f"{name}={value}" for name, value in changes.items())
    require_confirm("change billing options", target, confirm)
    log.warning("tier2: changing billing options %s", target)
    return client.request("PUT", "/agency/billing/options", json=changes)


#: Fields ``PUT /agency/billing/options`` accepts, per ``UpdateBillingConfigInput``.
OPTION_FIELDS: frozenset[str] = frozenset(
    {"overBillingEnabled", "autoRefillEnabled", "topUpAmount", "refillThreshold"}
)


def preview_refill(amount: int, currency: str = DEFAULT_CURRENCY) -> str:
    """What :func:`refill` would do, without doing it."""
    return describe_intent(
        "REFILL WALLET",
        amount,
        currency=currency,
        effect="spends real money from the payment method on file",
        unit=AMOUNT_UNIT_IS_UNKNOWN,
        required_confirmation=amount,
    )
