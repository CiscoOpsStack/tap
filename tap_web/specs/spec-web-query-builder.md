# Web Query Builder Specification

## Philosophy

Gryphon answers any question the grid can, but only for a reader who writes Gryphon. Most questions an
operator asks have one shape: "show me the X that are connected to a Y, where some field is Z". The
**query builder** asks that question step by step (find a type, follow a relationship the grid actually
has, add a condition) and shows the answer, with the Gryphon it ran. A query is held in the address, so it
can be shared and Back undoes the last step.

Cloud-security consoles converged on this shape: pick a resource type, add related objects, add
conditions, save the query. TAP's version is read-only: it runs through the same
`execute_search` path and `grid.read` gate as every panel, and saving is an export of one search as GRIFT.

## Goals

|   |   |   |
| :---: | --- | --- |
| 1. | No query language needed | Every step is a choice from what the grid declares or holds. |
| 2. | Nothing typed becomes query text | Names are checked against the grid; values travel as `$param` inputs. |
| 3. | A query is a link | The full query is in the URL; the page renders from it alone. |
| 4. | Read-only | The builder never writes; a saved query is a GRIFT download an operator imports. |

## Requirement Status

| RID | Name | Status | Notes |
| --- | --- | :---: | --- |
| req-web-query-builder | [Query Builder](#query-builder) | Implemented | `/__query/` and `/__query/export.grift.json`; `tap_web/query_builder.py` |

## Requirements

### Query Builder
----
RID: `req-web-query-builder`

Status: `Implemented`

#### Implementation

- `GET /__query/?t=<type>&h=<EDGE>:<out|in>:<type>&c=<var>.<field>:<op>:<value>` renders the builder for
  that query and runs it; `t` is the type to find, each `h` one relationship step (at most three), each `c`
  one condition (at most six) on the start type (`n0`) or a step's type (`n1`…`n3`).
- The type list is the plugin domain types holding at least one entity. The relationship list for a step is
  the edge types and directions present in the grid from the previous step's type, with counts. The field
  list is the type's name, entity id and model scalar fields. The tests are is, is not, contains, starts
  with, ends with, greater than, less than, is set, is not set.
- A type, edge type, field or test the grid does not declare is refused with a sentence; nothing runs.
- The query compiles to Gryphon with every value as a `$param` input and runs through `execute_search`
  (so `grid.read` and the read-only search alias apply). The page shows the first 200 rows, each named
  entity a drawer link, and the Gryphon it ran.
- `GET /__query/export.grift.json?…&name=` returns a GRIFT document holding one Gryphon search with the
  values written in as escaped literals; a value holding a line break is refused.
- The palette offers "Build a query" (`req-web-nav-no-hamburger-3`); no chrome element is added.

#### Acceptance Criteria

| ACID | Title | Status | Description | Notes |
| --- | --- | :---: | --- | --- |
| req-web-query-builder-1 | Declared Names Only | Implemented | An undeclared type, relationship, field or test is refused with a sentence and nothing runs. | `tap_web/tests/test_query_builder.py` |
| req-web-query-builder-2 | Values Are Parameters | Implemented | Condition values reach Gryphon as `$param` inputs; a value holding a quote changes no query text. | `tap_web/tests/test_query_builder.py` |
| req-web-query-builder-3 | Runs Read-Gated | Implemented | The page and the export require `grid.read`; the query runs through `execute_search`. | `tap_web/tests/test_query_builder.py` |
| req-web-query-builder-4 | Export Is One Search | Implemented | The export is a GRIFT document with one Gryphon search whose literals are escaped; a line break is refused. | `tap_web/tests/test_query_builder.py` |
| req-web-query-builder-5 | The URL Is The Query | Implemented | Adding a step or condition redirects to the canonical URL; removing one is a link; the page renders from the URL alone. | `tap_web/tests/test_query_builder.py` |

#### Future

Saved queries as issue rules (the posture plugin's toxic combinations are the first consumer), OR groups,
and a graph view of the results.
