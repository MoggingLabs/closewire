"""Local validation for Closebot Job-Flow graphs.

A bot's logic is a graph of typed nodes, not a prompt. Before a graph is sent to
``bots.save``, this module checks it **offline** against
the vendored node catalogue (``schema/node-descriptors.json``), so an obvious mistake costs
nothing instead of a paced round-trip and an opaque 400.

What it can check, and why that list stops where it does — every rule below is grounded in
the live catalogue and a live graph, not assumed:

* **Node types.** A node's ``type`` must match a catalogue ``className``. The catalogue
  keys on ``className`` — not ``type``, ``name`` or ``id`` — and covers ``atomicNodes``
  *and* ``tools`` (32 classes; indexing only the former missed 5).
* **Graph integrity.** Unique node ids, no edge pointing at a missing node, at least one
  ``Source`` node, and nothing unreachable from it. Ids are compared in one canonical form
  (:func:`_node_key`), so a graph carrying integer ids is checked exactly as strictly as
  one carrying strings, and an edge may name a node in either form.
* **Enum values.** A property with a non-empty ``enumValues`` must hold one of them.
* **Property names.** Reported as **warnings**, never errors. A live node's ``data``
  carries descriptor properties *plus* meta keys (``type``, ``name``,
  ``__dynamicVariables``) *plus* node-specific extras the catalogue does not declare —
  ``Source`` has no declared properties yet legitimately carries ``globalAgentTools``. An
  unknown key is therefore a hint, not proof of a defect.

**There is deliberately no "required field" check.** The catalogue has no ``required`` flag;
``defaultValue: null`` appears on properties that are plainly optional. Inventing a rule
here would reject valid graphs, so required-ness is left to the server.

There is **no standalone validate route**. The server's verdict on a graph arrives in the
``SaveBotResponse.invalidPaths`` field of ``POST /bot/{id}/save`` — see
:func:`closewire_client.writes.bots.invalid_paths`. That is precisely why
:func:`validate_graph` is a pre-flight and not a replacement: the authoritative check costs
a write, so this one exists to make the cheap mistakes free.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "Problem",
    "load_catalogue",
    "index_by_class",
    "validate_graph",
    "new_graph",
    "make_node",
    "connect",
    "META_KEYS",
    "SOURCE_CLASS",
]

#: Keys that appear in a node's ``data`` for bookkeeping rather than as a declared property.
META_KEYS = frozenset({"type", "name", "__dynamicVariables"})

#: The entry-point node class every flow must have.
SOURCE_CLASS = "Source"

_VENDORED = Path(__file__).resolve().parent.parent / "schema" / "node-descriptors.json"


@dataclass(frozen=True)
class Problem:
    """One validation finding.

    ``severity`` is ``"error"`` for something the graph cannot be right about, and
    ``"warning"`` for something suspicious that the catalogue cannot settle.
    """

    severity: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.where}: {self.message}"

    @property
    def is_error(self) -> bool:
        return self.severity == "error"


def load_catalogue(path: "str | Path | None" = None) -> dict[str, Any]:
    """Load the node catalogue, defaulting to the vendored copy so this works offline.

    The vendored file and the live ``GET /bot/nodeDescriptors`` response have the same
    shape and the same 27 + 5 classes, verified against a live account — so pre-flight
    validation needs no network call.
    """
    source = Path(path) if path else _VENDORED
    return json.loads(source.read_text(encoding="utf-8"))


def index_by_class(catalogue: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index ``atomicNodes`` **and** ``tools`` by ``className``."""
    index: dict[str, dict[str, Any]] = {}
    for section in ("atomicNodes", "tools"):
        for entry in catalogue.get(section) or []:
            if isinstance(entry, dict) and entry.get("className"):
                index.setdefault(entry["className"], entry)
    return index


def _node_key(value: Any) -> str | None:
    """The canonical form of a node id, or ``None`` when there is no usable id.

    Ids come out of JSON, where a graph exported by one tool numbers its nodes ``1, 2, 3``
    and one written by hand names them ``"n1"`` — and an edge may name a node in a
    different form than the node declares. So *every* id in this module goes through here:
    the duplicate set, edge endpoints, ``Source`` detection and the reachability walk. One
    notion of "the same node", derived in one place.

    That was the defect: :func:`validate_graph` tested membership with the raw value but
    populated the set with ``str(value)``, so two nodes both called ``1`` compared as
    different and the duplicate check — which ``bots.save(validate_first=True)`` advertises
    — passed the graph through to a paced write and an opaque 400.

    "No id" means **absent or blank**, not falsy: ``0`` is a perfectly good id, and reading
    it as missing reported three phantom errors (no id, dangling edge, no ``Source``) for
    one valid graph.
    """
    if value is None:
        return None
    key = str(value).strip()
    return key or None


def _declared_properties(descriptor: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        p["name"]: p
        for p in descriptor.get("properties") or []
        if isinstance(p, dict) and p.get("name")
    }


def validate_graph(
    graph: Any, catalogue: dict[str, Any] | None = None
) -> list[Problem]:
    """Check a Job-Flow graph offline. Returns findings; an empty list means it looks sane.

    Args:
        graph: ``{"nodes": [...], "edges": [...], ...}`` as ``bots.get_steps`` returns.
        catalogue: A node catalogue; the vendored one is loaded when omitted.
    """
    problems: list[Problem] = []
    if not isinstance(graph, dict):
        return [Problem("error", "graph", f"expected an object, got {type(graph).__name__}")]

    index = index_by_class(catalogue or load_catalogue())
    nodes = graph.get("nodes")
    edges = graph.get("edges") or []
    if not isinstance(nodes, list) or not nodes:
        return [Problem("error", "graph.nodes", "a flow needs at least one node")]

    seen_ids: set[str] = set()
    for position, node in enumerate(nodes):
        where = f"nodes[{position}]"
        if not isinstance(node, dict):
            problems.append(Problem("error", where, "node is not an object"))
            continue

        # Canonical from here down — comparing and storing must use the same key, or the
        # duplicate check only works for ids that were already strings.
        node_id = _node_key(node.get("id"))
        if node_id is None:
            problems.append(Problem("error", where, "node has no id"))
        elif node_id in seen_ids:
            problems.append(Problem("error", where, f"duplicate node id {node_id!r}"))
        else:
            seen_ids.add(node_id)

        kind = node.get("type")
        where = f"node {node_id if node_id is not None else position}"
        if kind not in index:
            problems.append(
                Problem(
                    "error",
                    where,
                    f"unknown node type {kind!r} — not one of the "
                    f"{len(index)} classes in the catalogue",
                )
            )
            continue

        declared = _declared_properties(index[kind])
        data = node.get("data") or {}
        if not isinstance(data, dict):
            problems.append(Problem("error", where, "`data` is not an object"))
            continue

        for key, value in data.items():
            if key in META_KEYS:
                continue
            spec = declared.get(key)
            if spec is None:
                problems.append(
                    Problem(
                        "warning",
                        where,
                        f"property {key!r} is not declared for {kind} — the catalogue "
                        "does not describe every field, so this may still be valid",
                    )
                )
                continue
            allowed = spec.get("enumValues") or []
            if allowed and value is not None and value not in allowed:
                problems.append(
                    Problem(
                        "error",
                        where,
                        f"{key}={value!r} is not one of {allowed}",
                    )
                )

    problems.extend(_check_edges(edges, seen_ids))
    problems.extend(_check_reachability(nodes, edges, seen_ids))
    return problems


def _check_edges(edges: Iterable[Any], node_ids: set[str]) -> list[Problem]:
    problems: list[Problem] = []
    for position, edge in enumerate(edges or []):
        where = f"edges[{position}]"
        if not isinstance(edge, dict):
            problems.append(Problem("error", where, "edge is not an object"))
            continue
        for end in ("source", "target"):
            value = edge.get(end)
            key = _node_key(value)
            if key is None:
                problems.append(Problem("error", where, f"edge has no {end}"))
            elif key not in node_ids:
                # Reported with the value as written, so the operator sees what they typed.
                problems.append(
                    Problem("error", where, f"{end} {value!r} is not a node in this graph")
                )
    return problems


def _check_reachability(
    nodes: list[Any], edges: Iterable[Any], node_ids: set[str]
) -> list[Problem]:
    """Every node should be reachable from a Source; a flow with no Source cannot run."""
    sources: list[str] = []
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") != SOURCE_CLASS:
            continue
        key = _node_key(node.get("id"))
        if key is not None:
            sources.append(key)
    if not sources:
        return [
            Problem(
                "error",
                "graph",
                f"no {SOURCE_CLASS} node — a flow has no entry point without one",
            )
        ]

    outgoing: dict[str, list[str]] = {}
    for edge in edges or []:
        if not isinstance(edge, dict):
            continue
        source = _node_key(edge.get("source"))
        target = _node_key(edge.get("target"))
        # A half-edge reaches nothing; _check_edges already reports it. Keeping it would
        # have put the literal string "None" into the reached set — a phantom node.
        if source is None or target is None:
            continue
        outgoing.setdefault(source, []).append(target)

    reached: set[str] = set()
    queue = deque(sources)
    while queue:
        current = queue.popleft()
        if current in reached:
            continue
        reached.add(current)
        queue.extend(outgoing.get(current, []))

    orphans = sorted(node_ids - reached)
    if orphans:
        return [
            Problem(
                "warning",
                "graph",
                f"{len(orphans)} node(s) unreachable from {SOURCE_CLASS}: "
                f"{orphans[:5]}{'…' if len(orphans) > 5 else ''}",
            )
        ]
    return []


# ── Construction helpers ──────────────────────────────────────────────────────
def new_graph() -> dict[str, Any]:
    """An empty graph with the top-level keys a live flow carries."""
    return {
        "nodes": [],
        "edges": [],
        "variables": [],
        "customTools": [],
        "enabledGlobalToolNames": [],
    }


def make_node(node_id: str, class_name: str, /, **properties: Any) -> dict[str, Any]:
    """One node, shaped the way a live graph shapes them.

    ``position`` is required by the builder's canvas; a caller that does not care can let
    it default and reposition in the UI.
    """
    position = properties.pop("position", None) or {"x": 0, "y": 0}
    return {
        "id": node_id,
        "type": class_name,
        "position": position,
        "data": {"type": class_name, **properties},
    }


def connect(
    source: str, target: str, *, source_handle: str | None = None, edge_id: str | None = None
) -> dict[str, Any]:
    """One edge. ``source_handle`` distinguishes a branch node's outputs."""
    edge: dict[str, Any] = {
        "id": edge_id or f"{source}->{target}",
        "source": source,
        "target": target,
    }
    if source_handle is not None:
        edge["sourceHandle"] = source_handle
    return edge
