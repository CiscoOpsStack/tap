"""The scope lens per spec-web-navigation (req-web-nav-scope-lens).

One dimension value (``aws_account = 123456789012``, say) the reader picks once, from the palette, and every
table on every page then narrows to: a row whose entity carries that dimension with a different value is left
out, and a row whose entity does not carry the key at all is kept, because the scope says nothing about it.

The scope lives in the reader's session, never in the grid. Its options are the entity dimensions actually
present: every key outside the reserved ``tap.`` namespace that holds at least two values (a scope of one
value would narrow nothing). A plugin may name the values of one key, the way an account number reads better as
the account's name, with :func:`register_scope_labeler`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

SESSION_KEY = "tap_scope"
RESERVED_PREFIX = "tap."

_LABELERS: dict[str, Callable[[list[str]], Mapping[str, str]]] = {}


@dataclass(frozen=True)
class Scope:
    key: str
    value: str
    label: str

    @property
    def text(self) -> str:
        return self.label if self.label == self.value else f"{self.label} ({self.value})"


@dataclass(frozen=True)
class ScopeOption:
    key: str
    value: str
    label: str
    count: int


def register_scope_labeler(key: str, labeler: Callable[[list[str]], Mapping[str, str]]) -> None:
    """Name the values of dimension ``key``: ``labeler(values)`` returns ``{value: label}`` for those it knows."""
    _LABELERS[key] = labeler


def key_label(key: str) -> str:
    from tap_web.panels.viewer_panel import humanize

    return humanize(key.replace(".", "_"))


def read_scope(session: Any) -> Scope | None:
    raw = session.get(SESSION_KEY) if session is not None else None
    if not isinstance(raw, dict):
        return None
    key, value = raw.get("key"), raw.get("value")
    if not isinstance(key, str) or not isinstance(value, str) or not key or not value:
        return None
    label = raw.get("label")
    return Scope(key=key, value=value, label=label if isinstance(label, str) and label else value)


def write_scope(session: Any, scope: Scope | None) -> None:
    if scope is None:
        session.pop(SESSION_KEY, None)
    else:
        session[SESSION_KEY] = asdict(scope)


def _labels(key: str, values: list[str]) -> Mapping[str, str]:
    labeler = _LABELERS.get(key)
    if labeler is None:
        return {}
    try:
        return labeler(values)
    except Exception:  # noqa: BLE001 — a label is presentation; the raw value stands in
        return {}


def scope_options() -> list[ScopeOption]:
    """Every dimension value a reader can scope to, most-populated key first.

    TAP-IMPLEMENTS: req-web-nav-scope-lens@9ae1bb8256e2/9b037e027920 (derivation) — options are the dimension values present on
        live entities; reserved ``tap.`` keys and single-valued keys are not offered.

    The caller must already hold ``grid.read``: this reads the entity table directly.
    """
    from django.db import connection

    from tap_grid.models import Entity

    table = Entity._meta.db_table
    sql = (
        f"SELECT d.key, d.value, COUNT(*) FROM {table} e, jsonb_each_text(e.dimensions) d "  # noqa: S608 — table name is the model's
        "WHERE e.deleted_at IS NULL GROUP BY d.key, d.value"
    )
    with connection.cursor() as cursor:
        cursor.execute(sql)
        rows: list[tuple[str, str, int]] = list(cursor.fetchall())
    by_key: dict[str, list[tuple[str, int]]] = {}
    for key, value, count in rows:
        if key.startswith(RESERVED_PREFIX) or not value:
            continue
        by_key.setdefault(key, []).append((value, int(count)))
    options: list[ScopeOption] = []
    for key, values in sorted(by_key.items(), key=lambda kv: (-sum(c for _, c in kv[1]), kv[0])):
        if len(values) < 2:
            continue
        labels = _labels(key, [v for v, _ in values])
        for value, count in sorted(values, key=lambda vc: (-vc[1], vc[0])):
            options.append(ScopeOption(key=key, value=value, label=labels.get(value, value), count=count))
    return options


def resolve_option(key: str, value: str) -> Scope | None:
    """The scope for ``key = value`` if that value is offered, named by its labeler; ``None`` otherwise."""
    for option in scope_options():
        if option.key == key and option.value == value:
            return Scope(key=key, value=value, label=option.label)
    return None


def _row_entity_id(row: Mapping[str, Any], id_field: str) -> str:
    from tap_web.panels.table_panel.filters import get_path

    for path in (id_field, "entity_id") if id_field else ("entity_id",):
        value = get_path(row, path)
        if value:
            return str(value)
    return ""


def apply_scope(
    rows: list[dict[str, Any]], scope: Scope | None, id_field: str = ""
) -> tuple[list[dict[str, Any]], int]:
    """Narrow ``rows`` to the scope; return the kept rows and how many were left out.

    TAP-IMPLEMENTS: req-web-nav-scope-lens@9ae1bb8256e2/ffe32c4909d3 (enforcement) — a row is left out only when its entity
        carries the scope's key with another value; rows naming no entity, or an entity without the key, stay.

    A row names its entity by ``id_field`` (the table's drawer id field) or ``entity_id``. The dimensions of every
    named entity are read in one query.
    """
    if scope is None or not rows:
        return rows, 0
    ids = {_row_entity_id(r, id_field) for r in rows}
    ids.discard("")
    dims = _dimensions(ids)
    kept: list[dict[str, Any]] = []
    for row in rows:
        entity_dims = dims.get(_row_entity_id(row, id_field))
        if entity_dims is not None and scope.key in entity_dims and str(entity_dims[scope.key]) != scope.value:
            continue
        kept.append(row)
    return kept, len(rows) - len(kept)


def _dimensions(ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    import uuid

    from tap_grid.models import Entity

    valid = []
    for raw in ids:
        try:
            valid.append(uuid.UUID(raw))
        except ValueError, AttributeError:
            continue
    if not valid:
        return {}
    return {str(pk): d or {} for pk, d in Entity.objects.filter(pk__in=valid).values_list("pk", "dimensions")}
