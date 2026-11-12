"""Visual query builder (req-web-query-builder): pick a type, follow edges, add conditions; get Gryphon.

A query is held entirely in the URL, so it is shareable and Back works:

    ?t=<start type>                       the type to find
    &h=<EDGE_TYPE>:<out|in>:<target type>  one hop per ``h``, in order (at most MAX_HOPS)
    &c=<var>.<field>:<op>:<value>          one condition per ``c`` (at most MAX_CONDITIONS)

Every name in a query (types, edge types, fields, operators) is checked against what the grid declares before it
is compiled, and every value is passed as a ``$param`` input, so nothing a reader types becomes query text. The
exported saved search is the one place values are written into the query; they are escaped for Gryphon's string
literal and a value holding a line break is refused.

The builder only reads. Saving is an export: a GRIFT file holding one search, which an operator imports.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

MAX_HOPS = 3
MAX_CONDITIONS = 6
RESULT_LIMIT = 200
EDGE_OPTION_LIMIT = 40
VARS = ("n0", "n1", "n2", "n3")
OPERATORS: dict[str, str] = {
    "eq": "=",
    "ne": "!=",
    "contains": "CONTAINS",
    "starts": "STARTS_WITH",
    "ends": "ENDS_WITH",
    "gt": ">",
    "lt": "<",
    "set": "IS NOT NULL",
    "unset": "IS NULL",
}
OPERATOR_LABELS: dict[str, str] = {
    "eq": "is",
    "ne": "is not",
    "contains": "contains",
    "starts": "starts with",
    "ends": "ends with",
    "gt": "greater than",
    "lt": "less than",
    "set": "is set",
    "unset": "is not set",
}
UNARY = {"set", "unset"}
BUILTIN_FIELDS = ("name", "entity_id")
_TYPE = re.compile(r"^[a-z][a-z0-9_]*$")
_EDGE = re.compile(r"^[A-Z][A-Z0-9_]*(__[a-z0-9_]+)?$")
_FIELD = re.compile(r"^[a-z_][a-z0-9_]*$")


class QueryError(ValueError):
    """A query part that names something the grid does not declare; the message says which."""


@dataclass(frozen=True)
class Hop:
    edge_type: str
    direction: str
    target_type: str


@dataclass(frozen=True)
class Condition:
    var: str
    field: str
    op: str
    value: str = ""


@dataclass
class QuerySpec:
    start_type: str = ""
    hops: list[Hop] = field(default_factory=list)
    conditions: list[Condition] = field(default_factory=list)

    def var_types(self) -> dict[str, str]:
        return {VARS[i]: t for i, t in enumerate([self.start_type, *(h.target_type for h in self.hops)])}

    def query_string(self) -> str:
        from urllib.parse import urlencode

        pairs = [("t", self.start_type)]
        pairs += [("h", f"{h.edge_type}:{h.direction}:{h.target_type}") for h in self.hops]
        pairs += [("c", f"{c.var}.{c.field}:{c.op}:{c.value}") for c in self.conditions]
        return urlencode(pairs)


def model_fields(entity_type: str) -> list[str]:
    """The fields a condition may name for a type: name, entity id, then the model's scalar fields."""
    from tap_grid.registry import get_model_class

    try:
        cls = get_model_class(entity_type)
    except KeyError:
        return []
    out = list(BUILTIN_FIELDS)
    for f in cls._meta.get_fields():
        if f.is_relation or f.name in ("id", "entity", "entity_id", "batch_id", "flip_map", "name"):
            continue
        if _FIELD.match(f.name):
            out.append(f.name)
    return out


def _known_types() -> set[str]:
    from tap_web.entity_search import domain_types

    return set(domain_types())


def parse(params: Any) -> QuerySpec:
    """Read a QuerySpec from request GET params, refusing any name the grid does not declare."""
    types = _known_types()
    spec = QuerySpec(start_type=str(params.get("t", "") or ""))
    if not spec.start_type:
        return spec
    if not _TYPE.match(spec.start_type) or spec.start_type not in types:
        raise QueryError(f"Unknown type {spec.start_type!r}. Pick a type from the list.")
    for raw in params.getlist("h")[:MAX_HOPS]:
        parts = str(raw).split(":")
        if len(parts) != 3 or not _EDGE.match(parts[0]) or parts[1] not in ("out", "in") or parts[2] not in types:
            raise QueryError(f"Unknown relationship {raw!r}. Pick one from the list.")
        spec.hops.append(Hop(*parts))
    var_types = spec.var_types()
    for raw in params.getlist("c")[:MAX_CONDITIONS]:
        head, _, value = str(raw).partition(":")
        op, _, value = value.partition(":")
        var, _, fname = head.partition(".")
        if var not in var_types or op not in OPERATORS or fname not in model_fields(var_types[var]):
            raise QueryError(f"Unknown condition {raw!r}. Pick the field and test from the lists.")
        if "\n" in value or "\r" in value:
            raise QueryError("A condition value cannot hold a line break.")
        spec.conditions.append(Condition(var, fname, op, "" if op in UNARY else value))
    return spec


def _path(var: str, fname: str) -> str:
    return f"{var}.{fname}" if fname in BUILTIN_FIELDS else f"{var}.data.{fname}"


def _typed(value: str) -> Any:
    if re.fullmatch(r"-?[0-9]+", value):
        return int(value)
    if re.fullmatch(r"-?[0-9]+\.[0-9]+", value):
        return float(value)
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def compile_gryphon(spec: QuerySpec, *, literal: bool = False) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    """(query lines, inputs, input schema). With ``literal`` the values are written into the query and inputs are empty.

    TAP-IMPLEMENTS: req-web-query-builder@9d5123c46bcd/e94ce11a1e20 (derivation) — the one translation from a builder query to
        Gryphon: declared names only, values as $params (or escaped literals for an export).
    """
    if not spec.start_type:
        return [], {}, {}
    pattern = f"(n0:{spec.start_type})"
    for i, hop in enumerate(spec.hops, start=1):
        edge = f"-[:{hop.edge_type}]->" if hop.direction == "out" else f"<-[:{hop.edge_type}]-"
        pattern += f"{edge}({VARS[i]}:{hop.target_type})"
    lines = [f"MATCH {pattern}"]
    inputs: dict[str, Any] = {}
    props: dict[str, Any] = {}
    preds = []
    for i, c in enumerate(spec.conditions):
        op = OPERATORS[c.op]
        if c.op in UNARY:
            preds.append(f"{_path(c.var, c.field)} {op}")
            continue
        value = _typed(c.value)
        if literal:
            preds.append(f"{_path(c.var, c.field)} {op} {_literal(value)}")
        else:
            name = f"p{i}"
            inputs[name] = value
            kind = (
                "boolean"
                if isinstance(value, bool)
                else "integer" if isinstance(value, int) else "number" if isinstance(value, float) else "string"
            )
            props[name] = {"type": kind}
            preds.append(f"{_path(c.var, c.field)} {op} ${name}")
    if preds:
        lines.append("WHERE " + " AND ".join(preds))
    returns = ["n0.entity_id AS id", "n0.name AS name"]
    for i in range(1, len(spec.hops) + 1):
        returns += [f"{VARS[i]}.entity_id AS {VARS[i]}_id", f"{VARS[i]}.name AS {VARS[i]}_name"]
    shown = {(c.var, c.field) for c in spec.conditions if c.field not in BUILTIN_FIELDS}
    returns += [f"{_path(v, f)} AS {v}_{f}" for v, f in sorted(shown)]
    lines.append("RETURN " + ", ".join(returns))
    schema = {"type": "object", "properties": props, "required": sorted(props)} if props else {}
    return lines, inputs, schema


@dataclass(frozen=True)
class EdgeOption:
    value: str
    label: str
    count: int


def edge_options(entity_type: str) -> list[EdgeOption]:
    """The relationships actually present from ``entity_type``: edge type, direction and the type at the other end."""
    from django.db.models import Count

    from tap_grid.models import Edge
    from tap_web.drawer import _type_names, edge_label

    types = _known_types()
    out_rows = (
        Edge.objects.filter(from_entity__entity_type=entity_type, to_entity__deleted_at__isnull=True)
        .values("edge_type", "to_entity__entity_type")  # type: ignore[misc]  # django-stubs sees the BaseModel manager
        .annotate(n=Count("pk"))
    )
    in_rows = (
        Edge.objects.filter(to_entity__entity_type=entity_type, from_entity__deleted_at__isnull=True)
        .values("edge_type", "from_entity__entity_type")  # type: ignore[misc]  # django-stubs sees the BaseModel manager
        .annotate(n=Count("pk"))
    )
    found = [(r["edge_type"], "out", r["to_entity__entity_type"], r["n"]) for r in out_rows]
    found += [(r["edge_type"], "in", r["from_entity__entity_type"], r["n"]) for r in in_rows]
    found = [f for f in found if f[2] in types and _EDGE.match(f[0])]
    names = _type_names({f[2] for f in found})
    opts = [
        EdgeOption(
            value=f"{edge}:{direction}:{target}",
            label=f"{'→' if direction == 'out' else '←'} {edge_label(edge)} {'→' if direction == 'out' else '←'} {names.get(target, target)}",
            count=n,
        )
        for edge, direction, target, n in found
    ]
    return sorted(opts, key=lambda o: (-o.count, o.label))[:EDGE_OPTION_LIMIT]


def run(spec: QuerySpec) -> dict[str, Any]:
    """Execute the query read-only through execute_search; ``rows`` and the compiled text, or an ``error``."""
    from tap_grid.models import Search
    from tap_grid.search import execute_search

    lines, inputs, schema = compile_gryphon(spec)
    if not lines:
        return {"rows": [], "lines": [], "error": ""}
    search = Search(
        name="Query builder",
        search_type="gryphon",
        root="node",
        definition={"query": lines},
        input_schema=schema or None,
        default_limit=RESULT_LIMIT,
        max_limit=RESULT_LIMIT,
    )
    try:
        result = execute_search(search, inputs or None, limit=RESULT_LIMIT, layer="extended")
    except Exception as exc:  # noqa: BLE001 — the reader sees why the query did not run
        return {"rows": [], "lines": lines, "error": str(exc)}
    envelope = result["results"] if "results" in result else result
    rows = [r for r in (envelope.get("rows") or []) if isinstance(r, dict)]
    return {"rows": rows[:RESULT_LIMIT], "lines": lines, "error": ""}


EXPORT_NAMESPACE = uuid.UUID("01a1c25b-77da-76d9-872a-3b1a120cc776")


def export_grift(spec: QuerySpec, name: str) -> dict[str, Any]:
    """A GRIFT document holding the query as one saved Gryphon search (values written in as literals)."""
    lines, _, _ = compile_gryphon(spec, literal=True)
    key = spec.query_string()
    search_id = str(uuid.uuid5(EXPORT_NAMESPACE, f"search:{key}"))
    batch_name = f"Saved query: {name}"
    return {
        "metadata": {"grift_version": "0"},
        "_reserved": {},
        "batches": [
            {
                "batch_entity": {
                    "entity_id": str(uuid.uuid5(EXPORT_NAMESPACE, f"batch:{key}")),
                    "entity_type": "batch",
                    "name": batch_name,
                    "dimensions": {},
                },
                "batch_node": {
                    "source": "tap_web.query_builder",
                    "name": batch_name,
                    "description": f"Exported from the query builder: ?{key}",
                },
                "nodes": [
                    {
                        "entity": {"entity_id": search_id, "entity_type": "search", "name": name, "dimensions": {}},
                        "node": {
                            "name": name,
                            "description": f"Saved from the query builder (?{key}).",
                            "search_type": "gryphon",
                            "root": "node",
                            "definition": {"query": lines},
                            "default_limit": RESULT_LIMIT,
                            "max_limit": RESULT_LIMIT,
                        },
                    }
                ],
                "edges": [],
            }
        ],
    }


def export_filename(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "query"
    return f"{slug}.grift.json"


def dumps(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=1) + "\n"
