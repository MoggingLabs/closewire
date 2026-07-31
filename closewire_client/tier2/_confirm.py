"""The Tier-2 confirmation gate.

Tier-2 operations publish a bot, destroy data, or move money. None of them may fire because
an argument defaulted, a loop ran twice, or a model filled in a plausible-looking field. So
every one of them is gated here, once, rather than by each function remembering to check.

**Two independent things must be true**, and they fail in different directions on purpose:

* ``confirm=True`` — a deliberate opt-in. Catches "I called the wrong function".
* a **typed token that must equal the target** — the bot id for a delete, the amount for a
  refill. Catches "I called the right function on the wrong thing", which ``confirm=True``
  alone cannot: a bare boolean is equally true for every target in the account.

The token is compared against the *actual* target the call will act on, so it cannot be
satisfied by echoing a constant. ``delete(bot_id="A", confirm="A")`` passes;
``delete(bot_id="B", confirm="A")`` raises and sends nothing. That is the whole point — the
caller has to name what it is destroying, and be right.

**The gate hands back the value it validated, and callers must act on that.** Comparing a
canonicalised target while the caller builds its URL from the raw argument would make the
promise false in the one way nobody would notice: ``delete("bot_A ", confirm="bot_A")``
would pass the gate and then issue ``DELETE /bot/bot_A``-plus-a-space. So
:func:`confirm_target` *returns* the canonical target and every Tier-2 call site rebinds its
argument to that return value — the confirmed value and the acted-on value are then the same
value, not two values that happen to agree. :func:`require_confirm` is the raise-only view,
for the callers that already hold a value with no raw/canonical gap (a validated ``int``
amount, a change-set string built here).

This gate is **independent of** ``CLOSEWIRE_DRY_RUN``: both must clear before anything is
sent. Dry-run stops the send; this stops the intent. A confirmed call under dry-run is still
suppressed, and an unconfirmed call is refused whether or not dry-run is on — so an operator
who turns dry-run off does not thereby arm every Tier-2 call in their script.

Refusal is always an exception, never a silent no-op return: a Tier-2 call that quietly did
nothing and returned ``None`` would be indistinguishable from one that worked.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ConfirmationRequired",
    "canonical_target",
    "confirm_target",
    "require_confirm",
    "describe_intent",
]


class ConfirmationRequired(RuntimeError):
    """A Tier-2 operation was called without a valid confirmation.

    Carries the action and target so a CLI can print a copy-pasteable next step rather than
    making the operator work out what token was wanted.
    """

    def __init__(
        self, action: str, target: str, reason: str, *, token_required: bool = True
    ) -> None:
        self.action = action
        self.target = target
        self.reason = reason
        #: What the operator should actually pass. Load-bearing: the guidance used to be
        #: built from the target unconditionally, so a refused **publish** advised
        #: ``confirm='bot_X'`` while `token_required=False` accepts only a literal ``True``
        #: — following the message's own instruction was refused again. The advice now
        #: comes from the same flag that decides what will be accepted, so the two cannot
        #: disagree.
        self.advice: Any = target if token_required else True
        how = (
            "pass the confirmation token that matches the target exactly"
            if token_required
            else "confirm explicitly"
        )
        super().__init__(
            f"REFUSED: {action} on {target!r} — {reason}\n"
            f"  Nothing was sent. To proceed, {how}:  confirm={self.advice!r}"
        )


def canonical_target(action: str, target: Any) -> str:
    """The one text form of what a call will act on — validated, not merely formatted.

    Every Tier-2 target passes through here before it is confirmed, logged, previewed, or
    interpolated into a path, so that all four see the same string.

    Args:
        action: What is about to happen, for the error message.
        target: The bot id, persona id, amount, or change set.

    Returns:
        ``str(target)`` stripped of surrounding whitespace.

    Raises:
        ValueError: If the target is empty or whitespace only. Raised rather than refused
            because it is a malformed argument, not a missing confirmation — the same way
            :func:`closewire_client.tier2.billing.refill` rejects a non-positive amount
            before the gate is ever consulted. An empty target is never intentional: there
            would be nothing for the operator to confirm, and the request would go to a
            path with a hole in it (``DELETE /bot/``), whose meaning is the server's to
            decide rather than ours.
    """
    text = str(target).strip()
    if not text:
        raise ValueError(
            f"{action}: no target was given (got {target!r}). A Tier-2 call with an empty "
            "target is never intentional — there would be nothing to confirm, and the "
            "request would be sent to a path with a hole in it."
        )
    return text


def confirm_target(
    action: str,
    target: Any,
    confirm: Any,
    *,
    token_required: bool = True,
) -> str:
    """Refuse unless this call is explicitly confirmed for **this** target, and return it.

    The return value is the point, not a convenience: it is the canonical target this call
    was confirmed *for*, and it is what the caller must act on. Rebinding at the call site —
    ``bot_id = confirm_target("delete bot", bot_id, confirm)`` — makes the raw argument
    unreachable afterwards, so the value in the confirmation and the value in the URL cannot
    drift apart. A caller that ignored the return value and used its own variable would
    reintroduce exactly the gap this closes.

    Args:
        action: What is about to happen, e.g. ``"delete bot"``. Used in the message only.
        target: The thing it happens to — a bot id, or an amount for a refill. Compared
            against ``confirm`` when ``token_required``.
        confirm: What the caller supplied. ``True`` alone is accepted only when
            ``token_required`` is False.
        token_required: Whether a matching typed token is demanded on top of the boolean.
            ``True`` for anything destructive or financial; ``False`` for publish, which is
            reversible by publishing again and has no second target to confuse it with.

    Returns:
        The canonical target — the exact value the call is now authorised to act on.

    Raises:
        ConfirmationRequired: Unless the confirmation is valid for this target.
        ValueError: If the target is empty — see :func:`canonical_target`.
    """
    target_text = canonical_target(action, target)

    if confirm is None or confirm is False:
        raise ConfirmationRequired(
            action, target_text, "no confirmation was given",
            token_required=token_required,
        )

    if not token_required:
        # Publish-style: an explicit True is enough, but *only* a literal True. A truthy
        # string would mean the caller thought a token was wanted and got it wrong, which
        # should not silently succeed.
        if confirm is True:
            return target_text
        raise ConfirmationRequired(
            action,
            target_text,
            f"expected confirm=True, got {confirm!r}",
            token_required=False,
        )

    if confirm is True:
        # The dangerous near-miss: `confirm=True` on a destructive op looks confirmed and
        # reads as confirmed, but names no target — exactly the mistake the token exists to
        # catch. Refused explicitly rather than falling through to the mismatch message.
        raise ConfirmationRequired(
            action,
            target_text,
            "confirm=True is not enough for a destructive or financial operation — it "
            "would be equally true for every target in the account",
        )

    if _canonical(confirm) != target_text:
        raise ConfirmationRequired(
            action,
            target_text,
            f"the confirmation token {_canonical(confirm)!r} does not match the target",
        )

    return target_text


def require_confirm(
    action: str,
    target: Any,
    confirm: Any,
    *,
    token_required: bool = True,
) -> None:
    """:func:`confirm_target` without the return value — the raise-only view of the gate.

    Use this **only** when the value to act on is already canonical and cannot drift from
    the value that was confirmed: a ``refill`` amount validated as a positive ``int`` (whose
    text form is exact and whose body must carry the ``int``, not its text), or a change set
    this package built itself. Anything that interpolates a caller-supplied string into a
    path must use :func:`confirm_target` and act on what it returns — see that function.

    Raises:
        ConfirmationRequired: Unless the confirmation is valid for this target.
        ValueError: If the target is empty — see :func:`canonical_target`.
    """
    confirm_target(action, target, confirm, token_required=token_required)


def _canonical(value: Any) -> str:
    """Compare tokens as text, so ``5`` and ``"5"`` agree.

    A CLI can only ever hand over a string, while Python callers pass an int amount. Making
    the *caller* match the type would fail confirmations that are plainly correct, and the
    pressure that creates — operators reaching for a looser gate — is worse than the
    strictness buys. Values are stripped so a trailing newline from a shell does not refuse
    a correct token.
    """
    return str(value).strip()


def describe_intent(action: str, target: Any, **details: Any) -> str:
    """The safe no-op explanation: what *would* happen, in one block.

    Phase 08 requires the default — no flags — to explain rather than act. Built here so
    every Tier-2 surface explains itself the same way.

    The target goes through :func:`canonical_target`, so the preview names the same string
    the gate will demand and the request will carry. A preview that advertised a token the
    gate would then not recognise would be worse than no preview.
    """
    lines = [f"Would {action}: {canonical_target(action, target)}"]
    for key, value in details.items():
        if value is not None:
            lines.append(f"  {key}: {value}")
    lines.append("Nothing has been sent. Re-run with a matching confirmation to proceed.")
    return "\n".join(lines)
