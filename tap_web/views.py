"""TAP Web views."""

import contextlib
import json
import logging
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from django.http import Http404, HttpRequest, HttpResponse, JsonResponse, QueryDict
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods

from tap_auth.capabilities import READ_CAPABILITY
from tap_auth.errors import AuthzError
from tap_grid.caller_context import CallerContext, get_caller_context, require_caller_context, set_caller_context
from tap_web.models import Page
from tap_web.navigation import build_breadcrumb, load_explicit_parents
from tap_web.page import (
    LandingResolution,
    PanelSlot,
    build_url_id,
    column_band,
    get_page_by_slug,
    get_page_slots,
    group_bands,
    iter_layout_rows,
    parse_panel_url_id,
    resolve_landing,
    slot_query_params,
)

logger = logging.getLogger(__name__)


def _authorize_grid_read(operation: str) -> None:
    """Authorize grid.read for the active request actor (req-tap-auth-policy).

    The primary, per-route gate for tap_web read entrypoints: it raises AuthzError
    (→ 403 via CallerContextMiddleware) for an actor lacking grid.read, before any
    Page/Panel/Edge row is resolved, so existence is not leaked. The ORM read
    backstop (req-tap-auth-orm-read-backstop) sits beneath as defense-in-depth for
    any read site this gate misses; this call turns that backstop's 500-class
    `unguarded_operation` into a clean, intended 403.
    """
    from tap_auth import policy
    from tap_grid.caller_context import get_caller_context

    policy.authorize(get_caller_context(), READ_CAPABILITY, operation=operation)


# ---------------------------------------------------------------------------
# Page views
# ---------------------------------------------------------------------------


@require_GET
def landing_view(request: HttpRequest) -> HttpResponse:
    """Serve the root URL by redirecting to the operator's landing page (req-web-page-landing).

    Redirect (302, not in-place render) so there is exactly one canonical URL per
    conceptual page. Otherwise `/` and the target slug both serve the same
    content with different breadcrumbs ("TAP" vs "TAP > <Name>"), since the
    breadcrumb builder keys off the request path, not the rendered Page. The
    root's query string rides along unchanged (req-web-page-landing-9).

    Which page is decided in the boot profile (`web.landing_entity_id` +
    `web.landing_slug`, carried as `settings.TAP_WEB_LANDING`) and resolved by
    `resolve_landing` — the one derivation the health probe and the boot share.
    Any state but `ok` renders the setup placeholder NAMING the state; there is
    no fallback and never a lookup by slug (req-web-page-landing-11).

    TAP-IMPLEMENTS: req-web-rendering-slashpage@51d5cad81171/7237af9bfce9 (surface) — dynamic
        pages work from /: the root resolves to the configured landing page with
        no hardcoded default view.
    """
    _authorize_grid_read("landing_view")
    landing = resolve_landing()
    if not landing.ok or landing.page is None:
        logger.warning("[6a0e] root has no landing page: %s", landing.describe())
        return _render_grid_placeholder(request, landing=landing)
    target = landing.page.slug
    if request.GET:
        target = f"{target}?{request.GET.urlencode()}"
    # The path is a server-side page slug with a leading slash, so the redirect cannot
    # leave this host; the guard is belt-and-braces against a future slug rule change
    # and the recognized sanitizer for a redirect that carries user-supplied query text.
    if not url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        logger.warning("[fe61] landing redirect target rejected as off-host: %r", target)
        return _render_grid_placeholder(request, landing=landing)
    return redirect(target)


@require_GET
def page_view(request: HttpRequest, page_slug: str) -> HttpResponse:
    """Render a Page by its slug.

    TAP-IMPLEMENTS: req-web-rendering-resolution@472c7f8390d8/e095edbf0443 (derivation) —
        Django routes stay static; which Page answers is resolved from the slug
        at request time, here.
    """
    _authorize_grid_read("page_view")
    slug = f"/{page_slug}"
    page = get_page_by_slug(slug)
    if page is None:
        raise Http404(f"Page '{slug}' not found.")
    return _render_page(request, page)


@require_GET
def parameterized_page_view(
    request: HttpRequest,
    page_slug: str,
    **kwargs: Any,
) -> HttpResponse:
    """Render a Page by slug with URL path segments injected as search inputs.

    Captures like ``entity_id`` from URL patterns are merged into query
    parameters so panel seed searches receive them as ``$entity_id`` inputs.
    """
    _authorize_grid_read("parameterized_page_view")
    slug = f"/{page_slug}"
    page = get_page_by_slug(slug)
    if page is None:
        raise Http404(f"Page '{slug}' not found.")
    extra = {k: str(v) for k, v in kwargs.items()}
    return _render_page(request, page, extra_query_params=extra)


# ---------------------------------------------------------------------------
# Panel views
# ---------------------------------------------------------------------------


def panel_view(request: HttpRequest, panel_url_id: str) -> HttpResponse:
    """Render a Panel fragment for HTMX consumption.

    URL format: /panel/<slug>--<entity-uuid>/
    On any exception returns an error fragment so the HTMX swap completes.

    TAP-IMPLEMENTS: req-web-render-panel@daa73ef32808/71c5509c5e73 (surface) — the HTMX
        panel endpoint: Panel.view names the template, the panel type owns
        assets and optional POST handling.
    TAP-IMPLEMENTS: req-web-rendering-panelsan.sec@b55a593a140f/71c5509c5e73 (enforcement) —
        panels render through standard Django views and autoescaping templates
        returned to the HTMX swap; no panel bypasses the template pipeline.
    """
    from tap_web.models import Panel

    # Primary grid.read gate (finding cs-tap-web-panel-001): authorize before any
    # Panel/entity resolution so a capability-less caller cannot read panel content
    # or point a ViewerPanel at an arbitrary entity via query string. AuthzError is
    # re-raised below (not swallowed into a 200 fragment) → 403.
    _authorize_grid_read("panel_view")

    entity_uuid = parse_panel_url_id(panel_url_id)
    if entity_uuid is None:
        return _panel_error(request, f"Invalid panel URL: '{panel_url_id}'")

    try:
        panel = Panel.objects.select_related("entity").get(entity__pk=entity_uuid)
    except Panel.DoesNotExist:
        return _panel_error(request, f"Panel '{entity_uuid}' not found.")

    try:
        panel_type = _get_panel_type_for_panel(panel)

        # POST dispatch: if the panel type defines handle_post, route POST there.
        if request.method == "POST" and panel_type and hasattr(panel_type, "handle_post"):
            # A panel type's POST handler may save through the service layer (the editor
            # panel calls a registered editor's handle_save), so it runs under the web
            # UI's default batch label like every other web save.
            with _web_edit_batch_label(request, f"panel {panel.get_name() or panel.entity_id}", "panel"):
                return panel_type.handle_post(panel, request)

        extra_ctx: dict = {}
        if panel_type and hasattr(panel_type, "get_view_context"):
            extra_ctx = panel_type.get_view_context(panel, request) or {}
        return render(
            request,
            panel.view,
            {
                "panel": panel,
                "edit_url": reverse("panel-edit", kwargs={"panel_url_id": panel_url_id}),
                **extra_ctx,
            },
        )
    except AuthzError:
        # An authorization denial must NOT be swallowed into a 200 error fragment
        # (that would hide a real access denial as "panel render failed"). Let it
        # propagate to CallerContextMiddleware.process_exception → 403.
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("[1899] Error rendering panel %s (view=%s)", entity_uuid, panel.view)
        return _panel_error(request, str(exc))


@contextlib.contextmanager
def _web_edit_batch_label(request: HttpRequest, subject: str, surface: str) -> Iterator[None]:
    """Label every batch a web-UI edit mints (req-grid-service-batch-label-required).

    Every service write that mints a batch must say what the change is. The web UI
    has no field for that yet, by decision: until enough edits come through the UI to
    design the affordance, the edit names itself from what it touched and where.

    The label rides on the request's bound context for the duration of the save, not
    on each call, so it also reaches the service writes a plugin's editor or panel
    type makes in its own `handle_save` — none of which need to know the rule. A
    write that joins a batch its code opened keeps that batch's own label.
    """
    prior = get_caller_context()
    who = getattr(request.user, "get_username", lambda: "")() or "an unknown user"
    set_caller_context(
        replace(
            prior or CallerContext(),
            batch_name=f"Web edit: {subject}",
            batch_description=f"Edited in the web UI by {who} via the {surface}.",
        )
    )
    try:
        yield
    finally:
        set_caller_context(prior)


@require_http_methods(["GET", "POST"])
def panel_edit_view(request: HttpRequest, panel_url_id: str) -> HttpResponse:
    """Editor for a Panel object — routes through the generic editor shell.

    URL format: /panel/<slug>--<entity-uuid>/edit/
    Dispatches to the panel's registered PanelType for typed form handling.
    Falls back to raw JSON config editing when no PanelType is registered.

    TAP-IMPLEMENTS: req-web-render-panel-edit@71d93bb8bbdc/4a9b721f59d0 (surface) — the
        panel-route integration with the generic editor shell: typed PanelType
        forms when registered, raw JSON config editing as the fallback.
    """
    from tap_web.models import Panel

    # Primary grid.read gate (same finding class as panel_view / object_edit_view):
    # authorize before resolving the Panel so a capability-less caller gets a clean
    # 403 rather than tripping the ORM read backstop deep in rendering. handle_save
    # additionally re-gates grid.write/grid.delete on the POST path.
    _authorize_grid_read("panel_edit_view")

    entity_uuid = parse_panel_url_id(panel_url_id)
    if entity_uuid is None:
        return _panel_error(request, f"Invalid panel URL: '{panel_url_id}'")

    try:
        panel = Panel.objects.select_related("entity").get(entity__pk=entity_uuid)
    except Panel.DoesNotExist:
        return _panel_error(request, f"Panel '{entity_uuid}' not found.")

    panel_type = _get_panel_type_for_panel(panel)
    form_class = getattr(panel_type, "form_class", None) if panel_type else None
    editor_template = getattr(panel_type, "editor_view", "") if panel_type else ""

    if request.method == "POST":
        if form_class is not None:
            form = form_class(request.POST)
            if form.is_valid():
                with _web_edit_batch_label(request, f"panel {panel.get_name() or panel.entity_id}", "panel editor"):
                    if panel_type and hasattr(panel_type, "handle_save"):
                        panel_type.handle_save(form, panel, request)
                    else:
                        from tap_grid.services import patch_node

                        patch_node(
                            target=panel.entity.pk,
                            payload=_form_to_patch_payload(form, panel),
                            caller_context=require_caller_context(),
                        )
                return redirect("panel-edit", panel_url_id=panel_url_id)
            return render(
                request,
                "tap_web/editor.html",
                _panel_editor_context(panel_url_id, panel, form=form, editor_template=editor_template),
            )
        # Generic fallback — no registered PanelType; raw JSON config editing.
        payload: dict[str, Any] = {
            "name": request.POST.get("name", panel.name),
            "description": request.POST.get("description", panel.description),
        }
        raw_config = request.POST.get("config", "")
        if raw_config:
            try:
                payload["config"] = json.loads(raw_config)
            except json.JSONDecodeError as exc:
                return render(
                    request,
                    "tap_web/editor.html",
                    _panel_editor_context(panel_url_id, panel, config_error=str(exc)),
                )
        from tap_grid.services import patch_node

        with _web_edit_batch_label(request, f"panel {panel.get_name() or panel.entity_id}", "panel JSON editor"):
            patch_node(
                target=panel.entity.pk,
                payload=payload,
                caller_context=require_caller_context(),
            )
        return redirect("panel-edit", panel_url_id=panel_url_id)

    # GET
    form = None
    if form_class is not None:
        if panel_type and hasattr(panel_type, "get_editor_initial"):
            initial = panel_type.get_editor_initial(panel)
        else:
            initial = {"name": panel.name, "description": panel.description, **panel.config}
        form = form_class(initial=initial)

    return render(
        request,
        "tap_web/editor.html",
        _panel_editor_context(panel_url_id, panel, form=form, editor_template=editor_template),
    )


# ---------------------------------------------------------------------------
# Generic object editor + viewer
# ---------------------------------------------------------------------------


@require_http_methods(["GET", "POST"])
def object_edit_view(request: HttpRequest, entity_type: str, object_url_id: str) -> HttpResponse:
    """Generic editor for any registered TAP entity type.

    URL format: /object/<entity-type>/<slug>--<entity-uuid>/edit/
    GET renders via the synthetic page builder using the entity-editor GRIFT
    subgraph. POST is handled by the EditorPanelType via the panel endpoint
    when using persisted pages; for synthetic pages, POST falls back to the
    legacy editor path since synthetic panels are rendered inline.
    """
    from tap_auth import policy
    from tap_grid.caller_context import get_caller_context
    from tap_grid.registry import get_model_class

    # Gate the direct graph read below (req-tap-auth-service-boundary): authorize
    # before resolving the object so existence is not leaked to an unauthorized
    # caller. AuthzError is translated to 403 by CallerContextMiddleware.
    policy.authorize(get_caller_context(), READ_CAPABILITY, operation="object_edit_view")

    entity_uuid = parse_panel_url_id(object_url_id)
    if entity_uuid is None:
        raise Http404(f"Invalid object URL: '{object_url_id}'")

    try:
        model_cls = get_model_class(entity_type)
    except KeyError:
        raise Http404(f"Unknown entity type '{entity_type}'.") from None

    try:
        obj = model_cls.objects.select_related("entity").get(entity__pk=entity_uuid)
    except model_cls.DoesNotExist:
        raise Http404(f"{entity_type} '{entity_uuid}' not found.") from None

    # POST: handle form submission directly (synthetic panels render inline,
    # so the editor panel's HTMX post targets the object edit URL).
    if request.method == "POST":
        return _handle_object_edit_post(request, obj, entity_type, object_url_id)

    # GET: render via synthetic page builder.
    from tap_web.synthetic import load_subgraph, render_synthetic_page

    subgraph = load_subgraph("entity-editor")
    return render_synthetic_page(
        request,
        subgraph,
        extra_query_params={
            "entity_id": str(entity_uuid),
            "entity_type": entity_type,
            "subject_entity_id": str(entity_uuid),
        },
    )


@require_GET
def object_view(request: HttpRequest, entity_type: str, object_url_id: str) -> HttpResponse:
    """Generic viewer for any registered TAP entity type.

    URL format: /object/<entity-type>/<slug>--<entity-uuid>/
    Renders via the synthetic page builder using the entity-viewer GRIFT
    subgraph in tap_web/data/.
    """
    from tap_auth import policy
    from tap_grid.caller_context import get_caller_context
    from tap_grid.registry import get_model_class

    # Authorize before the direct graph read below (no existence leak); AuthzError
    # → 403 via CallerContextMiddleware (req-tap-auth-service-boundary).
    policy.authorize(get_caller_context(), READ_CAPABILITY, operation="object_view")

    entity_uuid = parse_panel_url_id(object_url_id)
    if entity_uuid is None:
        raise Http404(f"Invalid object URL: '{object_url_id}'")

    try:
        model_cls = get_model_class(entity_type)
    except KeyError:
        raise Http404(f"Unknown entity type '{entity_type}'.") from None

    try:
        model_cls.objects.select_related("entity").get(entity__pk=entity_uuid)
    except model_cls.DoesNotExist:
        raise Http404(f"{entity_type} '{entity_uuid}' not found.") from None

    from tap_web.synthetic import load_subgraph, render_synthetic_page

    subgraph = load_subgraph("entity-viewer")
    return render_synthetic_page(
        request,
        subgraph,
        extra_query_params={
            "entity_id": str(entity_uuid),
            "entity_type": entity_type,
            "subject_entity_id": str(entity_uuid),
        },
    )


@require_GET
def object_drawer_view(request: HttpRequest, entity_type: str, object_url_id: str) -> HttpResponse:
    """The resource drawer's body for one entity, as an HTML fragment (spec-web-drawer).

    TAP-IMPLEMENTS: req-web-drawer-fragment@4bc796834185/4b2f4649f92a (surface) — the drawer's route:
        the viewer's url_id grammar and READ_CAPABILITY, a fragment instead of a page.

    Same URL grammar and authorization as :func:`object_view`; a missing entity is a 404
    fragment with a sentence, so the drawer can show it in place.
    """
    from tap_auth import policy
    from tap_grid.caller_context import get_caller_context
    from tap_web.drawer import build_drawer_context

    policy.authorize(get_caller_context(), READ_CAPABILITY, operation="object_drawer_view")

    entity_uuid = parse_panel_url_id(object_url_id)
    context = build_drawer_context(entity_type, entity_uuid) if entity_uuid else None
    if context is None:
        return render(
            request,
            "tap_web/partials/drawer_missing.html",
            {"drawer_entity_type": entity_type},
            status=404,
        )
    return render(request, "tap_web/partials/drawer.html", context)


def _handle_object_edit_post(
    request: HttpRequest,
    obj: Any,
    entity_type: str,
    object_url_id: str,
) -> HttpResponse:
    """Handle POST for the generic object editor — validate and save via EditorDescriptor."""
    from tap_web.registry import get_editor

    descriptor = get_editor(entity_type)
    if descriptor is None:
        raise Http404(f"No editor registered for entity type '{entity_type}'.")

    form_class = descriptor.get_form_class(obj)
    if form_class is None:
        override = descriptor.get_extra_context(obj).get("edit_url_override")
        if override:
            return redirect(override)
        raise Http404(f"No form registered for entity type '{entity_type}'.")

    form = form_class(request.POST)
    if form.is_valid():
        subject = f"{entity_type} {getattr(obj.entity, 'name', '') or obj.entity_id}"
        with _web_edit_batch_label(request, subject, "object editor"):
            descriptor.handle_save(form, obj, request)
        return redirect("object-edit", entity_type=entity_type, object_url_id=object_url_id)

    # Validation failed — re-render the synthetic editor page with errors.
    # The editor panel will pick up form errors from the re-rendered context.
    from tap_web.synthetic import load_subgraph, render_synthetic_page

    subgraph = load_subgraph("entity-editor")
    return render_synthetic_page(
        request,
        subgraph,
        extra_query_params={
            "entity_id": str(obj.entity_id),
            "entity_type": entity_type,
            "subject_entity_id": str(obj.entity_id),
        },
    )


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------


def _panel_editor_context(
    panel_url_id: str,
    panel: object,
    form: object = None,
    editor_template: str = "",
    config_error: str = "",
) -> dict:
    """Build template context for the panel editor page."""
    from tap_web.models import Panel

    assert isinstance(panel, Panel)
    graph_ctx = _get_neighborhood_context(panel.entity_id)
    # Editor assets come from the panel type, not the panel instance.
    panel_type = _get_panel_type_for_panel(panel)
    editor_css: dict[str, None] = dict.fromkeys(getattr(panel_type, "editor_css", []) or [])
    editor_js: dict[str, None] = dict.fromkeys(getattr(panel_type, "editor_js", []) or [])
    view_url = reverse(
        "object-view", kwargs={"entity_type": "panel", "object_url_id": build_url_id(panel.slug, panel.entity_id)}
    )
    return {
        "obj": panel,
        "obj_name": panel.name or panel.slug,
        "entity_type": "panel",
        "object_url_id": panel_url_id,
        "form": form,
        "editor_template": editor_template,
        "editor_css_assets": list(editor_css),
        "editor_js_assets": list(editor_js),
        "config_json": json.dumps(panel.config or {}, indent=2),
        "config_error": config_error,
        "view_url": view_url,
        **graph_ctx,
    }


def _object_editor_context(
    entity_type: str,
    object_url_id: str,
    obj: object,
    form: object,
    *,
    editor_template: str = "",
    view_url: str = "",
    extra: dict | None = None,
) -> dict:
    """Build template context for the generic object editor page."""
    graph_ctx = _get_neighborhood_context(obj.entity_id)
    return {
        "obj": obj,
        "obj_name": str(obj),
        "entity_type": entity_type,
        "object_url_id": object_url_id,
        "form": form,
        "editor_template": editor_template,
        "editor_css_assets": [],
        "editor_js_assets": [],
        "config_json": None,
        "config_error": "",
        "view_url": view_url,
        **(extra or {}),
        **graph_ctx,
    }


# ---------------------------------------------------------------------------
# Graph context helper (replaces neighborhood.py)
# ---------------------------------------------------------------------------


def _get_neighborhood_context(entity_id: object) -> dict[str, Any]:
    """Return Cytoscape graph context for the panel/object editor templates.

    Executes a transient gryphon hub-and-spoke search using the same Search
    definition as the synthetic entity page subgraph.
    """
    from tap_grid.models import Search
    from tap_grid.search import execute_search
    from tap_web.utils import graph_script_ids

    search = Search(
        search_type="gryphon",
        root="node",
        name="hub-and-spoke",
        definition={
            "query": [
                "MATCH (hub)-[e]-(neighbor)",
                "WHERE hub.entity_id = $entity_id",
                "RETURN hub, e, neighbor",
            ]
        },
        default_limit=200,
        max_limit=500,
    )
    try:
        result = execute_search(search, inputs={"entity_id": str(entity_id)}, layer="extended")
        envelope = result.get("results", result)
        nodes_raw = envelope.get("nodes", [])
        edges_raw = envelope.get("edges", [])
    except AuthzError:
        # An authorization denial must surface as a 403/no-access (translated by
        # CallerContextMiddleware), not be swallowed into a graph-data error.
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("[f200] hub-and-spoke search failed for entity %s", entity_id)
        return {
            "graph_nodes": [],
            "graph_edges": [],
            "graph_placement": "cytoscape:cose",
            "graph_error": f"Graph context failed: {exc}",
            "graph_context_id": str(entity_id),
            **graph_script_ids(entity_id),
        }

    return {
        "graph_nodes": nodes_raw,
        "graph_edges": edges_raw,
        "graph_placement": "cytoscape:cose",
        "graph_error": None,
        "graph_context_id": str(entity_id),
        **graph_script_ids(entity_id),
    }


# ---------------------------------------------------------------------------
# Standard panel helpers
# ---------------------------------------------------------------------------

_STANDARD_PANEL_FIELDS = frozenset({"name", "description"})


def _form_to_patch_payload(form: object, panel: object) -> dict[str, Any]:
    """Return a patch payload dict from validated form cleaned_data for use with patch_node()."""
    from django.forms import BaseForm

    from tap_web.models import Panel

    assert isinstance(form, BaseForm)
    assert isinstance(panel, Panel)
    cleaned = form.cleaned_data
    payload: dict[str, Any] = {"name": cleaned["name"]}
    if "description" in cleaned:
        payload["description"] = cleaned["description"]
    config_updates = {k: v for k, v in cleaned.items() if k not in _STANDARD_PANEL_FIELDS}
    if config_updates:
        payload["config"] = config_updates
    return payload


def _get_panel_type_for_panel(panel: object) -> type | None:
    """Return the registered PanelType whose view matches panel.view, or None."""
    from tap_web.registry import panel_type_registry

    panel_view = getattr(panel, "view", None)
    for scope_data in panel_type_registry.all().values():
        for panel_type_cls in scope_data.values():
            if getattr(panel_type_cls, "view", None) == panel_view:
                return panel_type_cls
    return None


# ---------------------------------------------------------------------------
# Internal page rendering helpers
# ---------------------------------------------------------------------------


def _render_page(
    request: HttpRequest,
    page: object,
    extra_query_params: dict[str, str] | None = None,
) -> HttpResponse:
    """Render a Page using the page template.

    TAP-IMPLEMENTS: req-web-render-process@4b4b75d90752/7e71ee9bceca (derivation) — the one
        page-rendering pipeline, riding Django's own machinery end to end:
        layout processing, panel-type asset collection, template render.
    TAP-IMPLEMENTS: req-web-rendering-pagesan.sec@6982e35b4c0b/7e71ee9bceca (enforcement) —
        every page renders through Django's autoescaping template pipeline
        (render → page.html); no page content path bypasses it.
    """
    panel_slots: list[PanelSlot] = get_page_slots(page)  # type: ignore[arg-type]

    panels_by_id: dict[str, str] = {}
    inputs_by_id: dict[str, dict[str, str]] = {}
    for slot in panel_slots:
        panels_by_id[slot.panel_id] = build_url_id(slot.panel.slug, slot.panel.entity_id)
        inputs_by_id[slot.panel_id] = slot.inputs

    # Static assets come exclusively from the panel type. Panel instances do
    # not declare assets — a panel is identified by its `view` and the type
    # owns all css/js the panel needs to render.
    css: dict[str, None] = {}
    js: dict[str, None] = {}
    for slot in panel_slots:
        panel_type = _get_panel_type_for_panel(slot.panel)
        for asset_path in getattr(panel_type, "css", []):
            css[asset_path] = None
        for asset_path in getattr(panel_type, "js", []):
            js[asset_path] = None

    query_params = request.GET.copy()
    if extra_query_params:
        query_params.update(extra_query_params)

    layout = getattr(page, "layout", {}) or {}
    full_bleed = bool(layout.get("full_bleed", False))
    processed_columns = _process_layout(layout, panels_by_id, query_params, inputs_by_id, full_bleed=full_bleed)
    bands = group_bands(processed_columns)
    fill_viewport = any(band["has_fill"] for band in bands)

    context = {
        "page": page,
        "processed_columns": processed_columns,
        "bands": bands,
        "multi_band": len(bands) > 1,
        "css_assets": list(css),
        "js_assets": list(js),
        "query_params": query_params,
        # Declarative per-page opt-in (layout.full_bleed): drop the centered
        # max-width container so the page fills the viewport width. base.html
        # honors this; non-page views leave it unset and stay centered.
        "full_bleed": full_bleed,
        # A `fill` row makes the page a viewport-height column (req-web-page-row-heights-3).
        "fill_viewport": fill_viewport,
    }
    return render(request, "tap_web/page.html", context)


# Column widths that are not already a CSS track: `compact` is a capped, shrinkable track that packs at the start
# of the band (req-web-page-compact-width).
COLUMN_TRACKS: dict[str, str] = {"compact": "minmax(0, 16rem)"}


def _process_layout(
    layout: dict[str, Any],
    panels_by_id: dict[str, str],
    query_params: QueryDict | None = None,
    inputs_by_id: dict[str, dict[str, str]] | None = None,
    *,
    full_bleed: bool = False,
) -> list[dict[str, Any]]:
    """Convert raw layout JSON into a sorted structure the template can iterate.

    Each row carries its own ``query_string``: the page's query parameters with the
    slot's fixed inputs (``USES_PANEL.properties.inputs``) laid over them, so the
    template prints one string per slot and never re-derives the overlay
    (req-web-page-plink-10/-11).
    """
    processed: list[dict[str, Any]] = []
    layout_rows = iter_layout_rows(layout)
    # Bands stack in normal flow, so the page has no definite height to divide: with
    # more than one band an `Nfr` row falls back to intrinsic height exactly as it does
    # off a full_bleed page (req-web-page-bands-5). A single band is unchanged.
    multi_band = len({column_band(col_data) for _, col_data, _ in layout_rows}) > 1
    for col_key, col_data, rows in layout_rows:
        processed_rows: list[dict[str, Any]] = []
        for row_key, row_data in rows:
            panel_id = row_data.get("panel-id", "")
            height = str(row_data.get("height", "auto"))
            slot_params = slot_query_params(query_params, (inputs_by_id or {}).get(panel_id))
            processed_rows.append(
                {
                    "key": row_key,
                    "panel_id": panel_id,
                    "panel_url_id": panels_by_id.get(panel_id),
                    "query_string": slot_params.urlencode(),
                    "row_span": row_data.get("row_span", 1),
                    "col_span": row_data.get("col_span", 1),
                    "height": height,
                    # An `Nfr` row divides the space REMAINING in a container with a
                    # definite height. Only a full_bleed page has one (its body is
                    # `h-full`); elsewhere the body is `min-h-full`, the height stays
                    # indefinite, and the row's share of it is zero — which the slot's
                    # own `overflow-y: auto` then clips to an invisible page
                    # (req-web-page-row-heights, tap#416). The template reads this to
                    # fall back to intrinsic height instead. Derived here, once, rather
                    # than re-spelled in template conditions.
                    "unbounded_fr": height.endswith("fr") and (multi_band or not full_bleed),
                    # `fill` takes the viewport height left under the chrome and the other
                    # bands. It is bounded on every page because the page itself becomes a
                    # viewport-height flex column when any row asks for it
                    # (req-web-page-row-heights-2/-3).
                    "is_fill": height == "fill",
                }
            )

        processed.append(
            {
                "key": col_key,
                "band": column_band(col_data),
                "width": col_data.get("width", "1fr"),
                # The CSS grid track for the width (req-web-page-compact-width-2).
                "track": COLUMN_TRACKS.get(col_data.get("width", "1fr"), col_data.get("width", "1fr")),
                "rows": processed_rows,
            }
        )

    return processed


def _panel_error(request: HttpRequest, message: str) -> HttpResponse:
    """Render the shared Panel Error fragment in place of a failed panel.

    TAP-IMPLEMENTS: req-web-render-missingpan@48d364a16601/efad03d5ab20 (surface) — a panel
        that cannot render populates its slot with the Panel Error fragment and
        an explanatory message; the caller logs the detail.
    """
    return render(request, "tap_web/panel_error.html", {"message": message})


def _render_grid_placeholder(request: HttpRequest, *, landing: LandingResolution) -> HttpResponse:
    """Render a live all-nodes + all-edges view when the root has no landing page.

    `landing` names WHY (undeclared / malformed / missing / slug_mismatch) so the
    page says what is wrong instead of a generic "not configured" (req-web-page-landing-11).
    """
    from tap_grid.models import Search
    from tap_grid.search import execute_search
    from tap_web.panels.table_panel import _safe_int

    node_limit = _safe_int(request.GET.get("limit"), 100)
    node_offset = _safe_int(request.GET.get("offset"), 0)
    edge_limit = _safe_int(request.GET.get("edge_limit"), 100)
    edge_offset = _safe_int(request.GET.get("edge_offset"), 0)

    node_search = Search(
        search_type="orm",
        root="node",
        definition={"filters": {}, "order_by": ["name"]},
        default_limit=100,
        max_limit=500,
    )
    edge_search = Search(
        search_type="orm",
        root="edge",
        definition={"filters": {}, "order_by": ["edge_type"]},
        default_limit=100,
        max_limit=500,
    )

    _empty_ctx: dict[str, Any] = {
        "nodes": [],
        "meta": {},
        "edges": [],
        "edges_meta": {},
        "table_error": None,
        "landing": {"state": landing.state, "detail": landing.describe()},
    }

    try:
        node_result = execute_search(node_search, limit=node_limit, offset=node_offset, layer="extended")
        edge_result = execute_search(edge_search, limit=edge_limit, offset=edge_offset, layer="extended")
    except Exception as exc:  # noqa: BLE001
        logger.exception("[5c03] Grid placeholder search failed")
        return render(
            request,
            "tap_web/setup_placeholder.html",
            {**_empty_ctx, "table_error": str(exc)},
        )

    # --- Nodes ---
    if "results" in node_result:
        nodes: list[dict[str, Any]] = node_result["results"].get("nodes", [])
        n_lim: int = node_result["limit"]
        n_off: int = node_result["offset"]
        n_count: int = node_result["count"]
        meta: dict[str, Any] = {
            "count": n_count,
            "limit": n_lim,
            "offset": n_off,
            "has_prev": n_off > 0,
            "has_next": n_off + n_lim < n_count,
            "prev_offset": max(0, n_off - n_lim),
            "next_offset": n_off + n_lim,
            "display_end": min(n_off + n_lim, n_count),
        }
    else:
        nodes = node_result.get("nodes", [])
        meta = {}

    # --- Edges ---
    if "results" in edge_result:
        edges: list[dict[str, Any]] = edge_result["results"].get("edges", [])
        e_lim: int = edge_result["limit"]
        e_off: int = edge_result["offset"]
        e_count: int = edge_result["results"]["info"].get("total_count", len(edges))
        edges_meta: dict[str, Any] = {
            "count": e_count,
            "limit": e_lim,
            "offset": e_off,
            "has_prev": e_off > 0,
            "has_next": e_off + e_lim < e_count,
            "prev_offset": max(0, e_off - e_lim),
            "next_offset": e_off + e_lim,
            "display_end": min(e_off + e_lim, e_count),
        }
    else:
        edges = edge_result.get("edges", [])
        edges_meta = {}

    return render(
        request,
        "tap_web/setup_placeholder.html",
        {
            **_empty_ctx,
            "nodes": nodes,
            "meta": meta,
            "edges": edges,
            "edges_meta": edges_meta,
        },
    )


# ---------------------------------------------------------------------------
# Navigation index
# ---------------------------------------------------------------------------


# GET + HEAD, not require_GET: this is the documented machine-readable affordance
# (req-web-nav-index-endpoint), and Django's require_GET answers HEAD with 405. A monitor
# or agent probing the endpoint should get headers, not a method error.
@require_http_methods(["GET", "HEAD"])
def nav_index_view(request: HttpRequest) -> JsonResponse:
    """Return the machine-readable nav index per req-web-nav-index-endpoint.

    TAP-IMPLEMENTS: req-web-nav-index-endpoint@5403a317ba14/4607d95360a5 (surface) — the
        /__nav-index.json affordance: every discoverable Page with its canonical
        breadcrumb path, for AI agents and automation.

    Enumerates every registered Page with its canonical breadcrumb path so
    AI agents, automation, and tooling can reason about the platform's
    navigation surface without scraping HTML.

    Schema is documented in spec-web-navigation §Machine-Readable Nav Index.
    Computed on request (no caching) for v0 per `req-web-nav-index-endpoint`.

    Requires grid.read (finding cs-tap-web-page-002): the index is graph-backed
    (Page rows: names, descriptions, slugs, panel URL ids) and so is a graph read
    like any other. It already sits behind the login wall (not in
    TAP_LOGIN_EXEMPT_PREFIXES), so this only additionally requires the authenticated
    actor to hold grid.read — closing the metadata-enumeration leak to no-cap users.
    To restore a deliberately public nav index, wrap the read in
    `tap_grid.read_guard.unguarded_read()` instead of authorizing here.
    """
    _authorize_grid_read("nav_index_view")

    # Filter `discoverable=True` per req-web-nav-page-discoverable — parameterized
    # pages (e.g. /samsite/finding/<entity_id>) opt out of discovery surfaces
    # because clicking them without a parameter produces a broken render. They
    # still resolve on direct visit; only browse-style discovery is gated.
    #
    # Sort by (-nav_weight, slug) per req-web-nav-page-weight — higher weight
    # floats up; ties resolve alphabetically by slug. Consumers (palette tree,
    # chevron popovers, column view) read the index in document order and
    # therefore inherit the same sort without re-sorting client-side.
    pages_qs = Page.objects.filter(discoverable=True).order_by("-nav_weight", "slug")
    # Each entry's breadcrumb is the page's EFFECTIVE path: explicit NESTS_UNDER
    # parents first, URL-derived parents elsewhere (req-web-nav-explicit-parent-edge).
    # The palette tree, sibling popover and column view all derive a page's parent
    # from this breadcrumb, so reading the edges once here moves every surface.
    explicit_parents = load_explicit_parents()
    entries: list[dict[str, Any]] = []
    for page in pages_qs:
        breadcrumb = build_breadcrumb(page.slug, explicit_parents=explicit_parents)
        entries.append(
            {
                "url": page.slug,
                "name": page.name,
                "description": page.description or "",
                "nav_weight": page.nav_weight,
                "breadcrumb": [{"label": seg.label, "url": seg.url} for seg in breadcrumb],
            }
        )
    return JsonResponse(
        {
            "version": "0",
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "pages": entries,
        },
        json_dumps_params={"indent": 2},
    )


@require_GET
def entity_search_view(request: HttpRequest) -> JsonResponse:
    """Domain entities matching ``?q=`` for the command palette (req-web-nav-command-palette-3).

    TAP-IMPLEMENTS: req-web-nav-command-palette@feedec1b917b/b5853066596e (surface) — /__entity-search.json, the palette's
        entity index: name, natural key or entity id, behind grid.read like every graph read.
    """
    from tap_web.entity_search import DEFAULT_LIMIT, search_entities

    _authorize_grid_read("entity_search_view")
    try:
        limit = int(request.GET.get("limit", DEFAULT_LIMIT))
    except ValueError:
        limit = DEFAULT_LIMIT
    hits = search_entities(request.GET.get("q", ""), limit=limit)
    return JsonResponse(
        {
            "version": "0",
            "entities": [
                {
                    "entity_id": h.entity_id,
                    "entity_type": h.entity_type,
                    "type_name": h.type_name,
                    "name": h.name,
                    "drawer_url": h.drawer_url,
                    "page_url": h.page_url,
                }
                for h in hits
            ],
        }
    )


@require_http_methods(["GET", "POST"])
def scope_view(request: HttpRequest) -> HttpResponse:
    """Read or set the reader's scope lens (req-web-nav-scope-lens).

    TAP-IMPLEMENTS: req-web-nav-scope-lens@9ae1bb8256e2/d91b82a8ba76 (surface) — GET /__scope.json returns the active scope and
        the offered options; POST sets one offered ``key``/``value`` or clears it (``clear=1``). The scope is
        session state; nothing is written to the grid.

    A POST from a form (no ``Accept: application/json``) redirects back to a same-origin ``next`` URL.
    """
    from tap_web.scope import read_scope, resolve_option, scope_options, write_scope

    _authorize_grid_read("scope_view")
    if request.method == "POST":
        if request.POST.get("clear"):
            write_scope(request.session, None)
        else:
            scope = resolve_option(request.POST.get("key", ""), request.POST.get("value", ""))
            if scope is None:
                return JsonResponse({"error": "That scope is not one of the offered options."}, status=400)
            write_scope(request.session, scope)
        if "application/json" not in request.headers.get("Accept", ""):
            target = request.POST.get("next") or "/"
            if not url_has_allowed_host_and_scheme(
                target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
            ):
                target = "/"
            return redirect(target)
    active = read_scope(request.session)
    return JsonResponse(
        {
            "version": "0",
            "active": (
                {"key": active.key, "value": active.value, "label": active.label, "text": active.text}
                if active
                else None
            ),
            "options": [{"key": o.key, "value": o.value, "label": o.label, "count": o.count} for o in scope_options()],
        }
    )


def _query_type_choices() -> list[dict[str, Any]]:
    """Domain types that hold at least one entity, most populated first, for the builder's "Find" list."""
    from django.db.models import Count

    from tap_grid.models import Entity
    from tap_web.drawer import _type_names
    from tap_web.entity_search import domain_types

    types = domain_types()
    counts = dict(
        Entity.objects.filter(entity_type__in=types, deleted_at__isnull=True)
        .values("entity_type")
        .annotate(n=Count("pk"))
        .values_list("entity_type", "n")
    )
    names = _type_names(set(counts))
    rows = [{"slug": t, "name": names.get(t, t), "count": n} for t, n in counts.items() if n]
    return sorted(rows, key=lambda r: r["name"].lower())


def _query_from_request(request: HttpRequest) -> tuple[Any, str]:
    """The builder query in the URL, with an added hop or condition folded in; (spec, error)."""
    from tap_web.query_builder import QueryError, QuerySpec, parse

    params = request.GET.copy()
    params.setlist("h", [h for h in params.getlist("h") if h])
    params.setlist("c", [c for c in params.getlist("c") if c])
    if params.get("cv") and params.get("co"):
        params.appendlist("c", f"{params['cv']}:{params['co']}:{params.get('cval', '')}")
    try:
        return parse(params), ""
    except QueryError as exc:
        return QuerySpec(), str(exc)


@require_GET
def query_builder_view(request: HttpRequest) -> HttpResponse:
    """The visual query builder page (req-web-query-builder): find a type, follow edges, add conditions, run.

    TAP-IMPLEMENTS: req-web-query-builder@9d5123c46bcd/b5aab19e30fa (surface) — /__query/, read-only: the query runs through
        execute_search behind grid.read and the page never writes; saving is a GRIFT download.
    """
    from tap_web import query_builder as qb
    from tap_web.drawer import _type_names, object_urls
    from tap_web.panels.viewer_panel import humanize

    _authorize_grid_read("query_builder_view")
    spec, error = _query_from_request(request)
    canonical = spec.query_string() if spec.start_type else ""
    if not error and (request.GET.get("cv") or "" in request.GET.getlist("h")):
        return redirect(f"{request.path}?{canonical}" if canonical else request.path)

    var_types = spec.var_types() if spec.start_type else {}
    names = _type_names(set(var_types.values()))
    hops = []
    for i, hop in enumerate(spec.hops):
        kept = qb.QuerySpec(spec.start_type, spec.hops[:i], [c for c in spec.conditions if c.var in qb.VARS[: i + 1]])
        hops.append(
            {
                "var": qb.VARS[i + 1],
                "type_name": names.get(hop.target_type, hop.target_type),
                "label": next(
                    (
                        o.label
                        for o in qb.edge_options(var_types[qb.VARS[i]])
                        if o.value == f"{hop.edge_type}:{hop.direction}:{hop.target_type}"
                    ),
                    hop.edge_type,
                ),
                "remove": kept.query_string(),
            }
        )
    conditions = []
    for i, c in enumerate(spec.conditions):
        rest = qb.QuerySpec(spec.start_type, spec.hops, [x for j, x in enumerate(spec.conditions) if j != i])
        conditions.append(
            {
                "text": f"{names.get(var_types[c.var], var_types[c.var])} {humanize(c.field)} {qb.OPERATOR_LABELS[c.op]} {c.value}".strip(),
                "remove": rest.query_string(),
            }
        )
    last_type = var_types[qb.VARS[len(spec.hops)]] if spec.start_type else ""
    hop_options = qb.edge_options(last_type) if spec.start_type and len(spec.hops) < qb.MAX_HOPS else []
    field_groups = (
        [
            {"var": v, "type_name": names.get(t, t), "fields": [(f, humanize(f)) for f in qb.model_fields(t)]}
            for v, t in var_types.items()
        ]
        if len(spec.conditions) < qb.MAX_CONDITIONS
        else []
    )

    result: dict[str, Any] = qb.run(spec) if spec.start_type and not error else {"rows": [], "lines": [], "error": ""}
    columns = [{"key": "name", "title": names.get(spec.start_type, "Result"), "type": spec.start_type, "id": "id"}]
    columns += [
        {"key": f"{h['var']}_name", "title": h["type_name"], "type": var_types[h["var"]], "id": f"{h['var']}_id"}
        for h in hops
    ]
    columns += [
        {
            "key": f"{c.var}_{c.field}",
            "title": f"{names.get(var_types[c.var], '')} {humanize(c.field)}",
            "type": "",
            "id": "",
        }
        for c in sorted(
            {(c.var, c.field): c for c in spec.conditions if c.field not in qb.BUILTIN_FIELDS}.values(),
            key=lambda c: (c.var, c.field),
        )
    ]
    rows = []
    for r in result["rows"]:
        cells = []
        for col in columns:
            value = r.get(col["key"])
            cell = {"text": "" if value is None else str(value), "page_url": "", "drawer_url": ""}
            if col["type"] and r.get(col["id"]):
                cell["drawer_url"], cell["page_url"] = object_urls(
                    col["type"], cell["text"] or str(r[col["id"]]), r[col["id"]]
                )
            cells.append(cell)
        rows.append(cells)

    context = {
        "page_title": "Query builder",
        "spec": spec,
        "canonical": canonical,
        "error": error or result["error"],
        "types": _query_type_choices(),
        "start_name": names.get(spec.start_type, ""),
        "hops": hops,
        "hop_options": hop_options,
        "conditions": conditions,
        "field_groups": field_groups,
        "operators": list(qb.OPERATOR_LABELS.items()),
        "columns": columns,
        "rows": rows,
        "row_limit": qb.RESULT_LIMIT,
        "gryphon": "\n".join(result["lines"]),
        "hidden_hops": [f"{h.edge_type}:{h.direction}:{h.target_type}" for h in spec.hops],
        "hidden_conditions": [f"{c.var}.{c.field}:{c.op}:{c.value}" for c in spec.conditions],
    }
    return render(request, "tap_web/query_builder.html", context)


@require_GET
def query_export_view(request: HttpRequest) -> HttpResponse:
    """The builder query as a GRIFT file holding one saved Gryphon search (req-web-query-builder-4)."""
    from tap_web import query_builder as qb

    _authorize_grid_read("query_export_view")
    spec, error = _query_from_request(request)
    if error or not spec.start_type:
        return HttpResponse(error or "Pick a type first.", status=400, content_type="text/plain; charset=utf-8")
    name = (request.GET.get("name") or "").strip()[:120] or "Saved query"
    response = HttpResponse(qb.dumps(qb.export_grift(spec, name)), content_type="application/json")
    response["Content-Disposition"] = f'attachment; filename="{qb.export_filename(name)}"'
    return response
