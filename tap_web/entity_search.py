"""Find a domain entity by what the reader remembers of it: its name, an ID or an ARN (req-web-nav-command-palette-3).

Domain entities are the ones a plugin's models declare; platform rows (pages, panels, searches, batches, edges)
are not offered, because the palette already lists pages and the rest are machinery. A term matches an entity's
name or (for a UUID) its entity id. Exact matches rank first, then prefixes, then the rest. The natural-key column
is a placeholder nothing reads yet (req-grid-entity-natural-key-11), so it is not searched.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from functools import lru_cache

MIN_TERM = 2
DEFAULT_LIMIT = 8
MAX_LIMIT = 25


@dataclass(frozen=True)
class EntityHit:
    entity_id: str
    entity_type: str
    type_name: str
    name: str
    drawer_url: str
    page_url: str


@lru_cache(maxsize=1)
def _domain_types_cached(types: tuple[str, ...]) -> tuple[str, ...]:
    from tap_grid.registry import get_model_class

    out = []
    for entity_type in types:
        cls = get_model_class(entity_type)
        if str(getattr(cls, "__module__", "")).startswith("tap_plugin.") and not getattr(cls, "INTERNAL_ONLY", False):
            out.append(entity_type)
    return tuple(sorted(out))


def domain_types() -> tuple[str, ...]:
    """Entity types declared by plugins and writable by ordinary means: the types a reader searches for."""
    from tap_grid.registry import list_entity_types

    return _domain_types_cached(tuple(sorted(list_entity_types())))


def _rank(name: str, term: str) -> int:
    low = name.lower()
    if low == term:
        return 0
    if low.startswith(term):
        return 1
    return 2


def search_entities(term: str, limit: int = DEFAULT_LIMIT) -> list[EntityHit]:
    """Domain entities matching ``term``, best first; empty for a term shorter than two characters.

    TAP-IMPLEMENTS: req-web-nav-command-palette@4a1e9f31303e/1d65f4358612 (derivation) — the entity index behind the palette:
        name or entity id, plugin-declared types only, exact then prefix then substring.

    The caller must already hold ``grid.read``.
    """
    from django.db.models import Q

    from tap_grid.models import Entity
    from tap_web.drawer import _type_names, object_urls

    term = (term or "").strip()
    if len(term) < MIN_TERM:
        return []
    limit = max(1, min(limit, MAX_LIMIT))
    types = domain_types()
    match = Q(name__icontains=term)
    try:
        match |= Q(pk=uuid.UUID(term))
    except ValueError:
        pass
    rows = list(
        Entity.objects.filter(match, deleted_at__isnull=True, entity_type__in=types)
        .values_list("pk", "entity_type", "name")
        .order_by("name")[: limit * 6]
    )
    low = term.lower()
    rows.sort(key=lambda r: (_rank(r[2] or "", low), len(r[2] or ""), r[2] or ""))
    rows = rows[:limit]
    names = _type_names({r[1] for r in rows})
    hits = []
    for pk, entity_type, name in rows:
        drawer_url, page_url = object_urls(entity_type, name or str(pk), pk)
        hits.append(
            EntityHit(
                entity_id=str(pk),
                entity_type=entity_type,
                type_name=names.get(entity_type, entity_type),
                name=name or str(pk),
                drawer_url=drawer_url,
                page_url=page_url,
            )
        )
    return hits
