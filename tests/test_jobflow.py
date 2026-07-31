"""Job-Flow graph validation tests.

`jobflow.py` shipped in phase 07 with no tests at all — a review critic pointed out that
its documented guarantees rested entirely on one ad-hoc run against a live graph that is
not committed. These pin the guarantees the module advertises, and in particular the
id-keying defects a deep-dive found:

* duplicate ids were only detected when ids were already strings, because membership was
  tested with the raw value against a set populated with ``str(node_id)``;
* an id of ``0`` was treated as "no id" because presence was gated on truthiness, which
  cascaded into three phantom errors for one valid graph;
* a target-less edge injected a literal ``"None"`` into the reachability set.

Every test states the graph inline, so a failure says what shape broke.

Run via pytest or directly (``python tests/test_jobflow.py``), matching the house style.
"""

from __future__ import annotations

from typing import Any

from closewire_client.jobflow import (
    SOURCE_CLASS,
    Problem,
    connect,
    index_by_class,
    load_catalogue,
    make_node,
    new_graph,
    validate_graph,
)

CATALOGUE = load_catalogue()


def _errors(graph: Any) -> list[Problem]:
    return [p for p in validate_graph(graph, CATALOGUE) if p.is_error]


def _warnings(graph: Any) -> list[Problem]:
    return [p for p in validate_graph(graph, CATALOGUE) if not p.is_error]


def _source(node_id: Any = "s1") -> dict[str, Any]:
    return {
        "id": node_id,
        "type": SOURCE_CLASS,
        "position": {"x": 0, "y": 0},
        "data": {"type": SOURCE_CLASS},
    }


# ── Catalogue ─────────────────────────────────────────────────────────────────
def test_catalogue_indexes_tools_as_well_as_atomic_nodes() -> None:
    """Indexing only ``atomicNodes`` missed 5 classes; 32 is the real total."""
    index = index_by_class(CATALOGUE)
    assert len(index) == 32, len(index)
    assert SOURCE_CLASS in index


# ── The clean case ────────────────────────────────────────────────────────────
def test_a_minimal_source_only_graph_is_clean() -> None:
    assert validate_graph({"nodes": [_source()], "edges": []}, CATALOGUE) == []


# ── Id keying — the defects a deep-dive fixed ─────────────────────────────────
def test_duplicate_integer_ids_are_caught() -> None:
    """Regression: raw-vs-str keying let integer duplicates through entirely."""
    graph = {
        "nodes": [
            {"id": 1, "type": SOURCE_CLASS, "data": {"type": SOURCE_CLASS}},
            {"id": 1, "type": SOURCE_CLASS, "data": {"type": SOURCE_CLASS}},
        ],
        "edges": [],
    }
    assert any("duplicate node id" in p.message for p in _errors(graph)), _errors(graph)


def test_duplicate_string_ids_are_still_caught() -> None:
    """The control — the case that always worked must not regress."""
    graph = {"nodes": [_source("a"), _source("a")], "edges": []}
    assert any("duplicate node id" in p.message for p in _errors(graph))


def test_id_zero_is_a_real_id_not_a_missing_one() -> None:
    """Regression: truthiness gating made id ``0`` vanish, cascading three phantom errors."""
    graph = {"nodes": [_source(0)], "edges": []}
    assert _errors(graph) == [], _errors(graph)


def test_edge_referring_to_an_integer_id_resolves() -> None:
    """Endpoints must be canonicalised the same way ids are, or every edge looks dangling."""
    graph = {
        "nodes": [
            {"id": 1, "type": SOURCE_CLASS, "data": {"type": SOURCE_CLASS}},
            {"id": 2, "type": "Statement", "data": {"type": "Statement"}},
        ],
        "edges": [{"id": "e1", "source": 1, "target": 2}],
    }
    assert _errors(graph) == [], _errors(graph)


# ── The five defects the phase claims to catch ────────────────────────────────
def test_unknown_node_type_is_an_error() -> None:
    graph = {"nodes": [_source(), {"id": "x", "type": "NoSuchClass", "data": {}}], "edges": []}
    assert any("unknown node type" in p.message for p in _errors(graph))


def test_dangling_edge_is_an_error() -> None:
    graph = {"nodes": [_source()], "edges": [{"id": "e", "source": "s1", "target": "ghost"}]}
    assert any("not a node in this graph" in p.message for p in _errors(graph))


def test_a_graph_with_no_source_is_an_error() -> None:
    graph = {
        "nodes": [{"id": "n", "type": "Statement", "data": {"type": "Statement"}}],
        "edges": [],
    }
    assert any(SOURCE_CLASS in p.message for p in _errors(graph))


def test_unreachable_node_is_a_warning_not_an_error() -> None:
    """Real published flows carry orphans, so this must not block a save."""
    graph = {
        "nodes": [_source(), {"id": "orphan", "type": "Statement",
                              "data": {"type": "Statement"}}],
        "edges": [],
    }
    assert _errors(graph) == []
    assert any("unreachable" in p.message for p in _warnings(graph))


def test_empty_graph_is_an_error() -> None:
    """Pins the *node-count* guard specifically.

    Asserting only "some error was reported" left this green when the guard was removed —
    the missing-``Source`` check fired instead and the test could not tell the difference.
    """
    problems = validate_graph({"nodes": [], "edges": []}, CATALOGUE)
    assert any("at least one node" in p.message for p in problems), problems


def test_missing_nodes_key_is_an_error() -> None:
    assert _errors({"edges": []})


def test_a_non_object_graph_is_an_error() -> None:
    assert _errors(["not", "a", "graph"])


# ── Warnings, not errors, for undeclared properties ───────────────────────────
def test_undeclared_property_is_a_warning() -> None:
    """``Source`` declares no properties yet legitimately carries ``globalAgentTools``."""
    node = _source()
    node["data"]["globalAgentTools"] = []
    assert _errors({"nodes": [node], "edges": []}) == []
    assert _warnings({"nodes": [node], "edges": []})


# ── Construction helpers round-trip ───────────────────────────────────────────
def test_helpers_build_a_graph_that_validates() -> None:
    graph = new_graph()
    graph["nodes"].append(make_node("s1", SOURCE_CLASS))
    graph["nodes"].append(make_node("m1", "Statement"))
    graph["edges"].append(connect("s1", "m1"))
    assert validate_graph(graph, CATALOGUE) == []


def test_make_node_emits_the_shape_a_live_graph_uses() -> None:
    """Pins node shape directly.

    The round-trip test above cannot catch a regression here: the validator deliberately has
    no required-field rule, so dropping ``data["type"]`` from ``make_node`` left it green.
    """
    node = make_node("n1", "Statement", Title="hello")
    assert node["id"] == "n1"
    assert node["type"] == "Statement"
    assert node["data"]["type"] == "Statement", "live nodes carry `type` inside `data` too"
    assert node["data"]["Title"] == "hello"
    assert node["position"] == {"x": 0, "y": 0}, "the builder canvas requires a position"


def test_connect_carries_a_source_handle_only_when_given() -> None:
    assert "sourceHandle" not in connect("a", "b")
    assert connect("a", "b", source_handle="yes")["sourceHandle"] == "yes"


if __name__ == "__main__":  # run without pytest
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  [PASS] {fn.__name__}")
    print(f"\n{len(tests)} jobflow tests passed.")
