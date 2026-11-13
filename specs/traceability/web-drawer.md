<!-- GENERATED TRACEABILITY FRAGMENT — manage.py guards --sync-accounting / --sync-evidence; do not hand-edit -->

# `tap_web/specs/spec-web-drawer.md`

| Bucket | Count |
| --- | ---: |
| mapped | 4 |
| excluded | 1 |
| 0-ACID (payable) | 0 |

## Exclusions

Reasons verbatim from each `Trace:` line; ⚠ marks zero-ACID exempt.

| RID | Category | 0-ACID | Reason |
| --- | --- | :---: | --- |
| `req-web-drawer-open` | non-python |  | tap_web/static/tap_web/js/drawer.js |

## Evidence

| Requirement | Declared | Derived | Implementation | Verified by |
| --- | --- | --- | --- | --- |
| `req-web-drawer` | Implemented | Tested | — | `req-web-drawer-1` |
| `req-web-drawer-categories` | Implemented | Verified | `is_finding_edge` | `req-web-drawer-categories-1`, `req-web-drawer-categories-2`, `req-web-drawer-categories-3`, `req-web-drawer-categories-4`, `req-web-drawer-categories-5` |
| `req-web-drawer-fragment` | Implemented | Verified | `build_drawer_context`, `object_drawer_view` | `req-web-drawer-fragment-1`, `req-web-drawer-fragment-2`, `req-web-drawer-fragment-3` |
| `req-web-drawer-sections` | Implemented | Verified | `plugin_sections` | `req-web-drawer-sections-1`, `req-web-drawer-sections-2`, `req-web-drawer-sections-3` |
