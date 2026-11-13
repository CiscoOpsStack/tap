# TAP Integrations Roadmap: ServiceNow and Jira

## Philosophy

An integration is worth building when it moves evidence, not when it moves tickets. Ops Stack's
vulnerability record of decision lives in ServiceNow (`sn_vul_*`, the PAIN method section 10.2) and its
engineering work lives in Jira. TAP holds the cloud graph neither has. Reading both into the grid lets
TAP answer questions none of the three can answer alone: which finding has no ticket, which CI has no
cloud resource, and which PAIN score lacks the CVSS vector that ServiceNow already holds.

The order is deliberate: **ingest first, reconcile second, act last.** Reading is reversible and
needs no new trust. Writing to a system of record is neither, so it waits for a governed action surface
that TAP does not have yet.

## Assessment: does TAP have an integrations paradigm?

Yes for inbound integrations. There is none yet for outbound ones.

| Piece | What exists | Where |
| --- | --- | --- |
| Plugins | A Django app with a `tap-plugin.toml` manifest declaring models, edges, editors, searches, falsifiers and GRIFT | `tap_plugins/specs/spec-tap-plugin-architecture.md`, `spec-tap-plugin-manifest-v0.md` |
| Collectors | An on-grid `Collector` node plus a registry key, one `run_collection()` entry point, self-test and readiness checks, a `CollectionJob` record, and writes through the GRIFT importer only | `tap_cares/specs/spec-tap-cares-collector.md` |
| Scheduling and increments | Scheduler, watermarks and early cutoff for progressive collection | `spec-tap-cares-scheduler.md`, `spec-tap-cares-progressive-collection.md` |
| Secrets | `*.secret.json` under `TAP_SECRETS_ROOT`, scoped by consumer, plugin-defined kinds, redaction and leak guards | `spec-tap-cares-secrets.md` |
| Correlation | Typed, constrained edges between plugin types, and reconcile on import | `tap_grid` edge declarations, `tap_grid/reconcile.py` |
| Policy | An OPA sidecar and the policy plugin, evaluating Rego rules as a collector (added by this work) | `_dev-plugins/policy/specs/spec-policy-v0.md` |

What is missing:

- **An outbound action surface.** No core path writes to an external system. Collectors only read, and
  the GRIFT importer only writes TAP. A write-back would have to be smuggled into a collector, which would
  put a side effect under a lifecycle built for idempotent reads.
- **Capability declarations.** The manifest cannot yet say "this plugin calls `https://<instance>` and
  needs secret kind X". That is Backlog in the manifest spec. The policy plugin's OPA dependency works
  around it with a spec section, as these plugins would.
- **Per-source rate and budget control.** Collectors pace themselves. There is no shared limiter for a
  SaaS API with a tenant-wide quota.

Conclusion: ServiceNow and Jira ingest fit the collector model with no new framework. Write-back needs
the governed action surface of phase 3 first.

## Goals

|   |   |   |
| :---: | --- | --- |
| 1. | Read before write | Phases 1 and 2 never write to ServiceNow or Jira. |
| 2. | Correlate, don't copy | Tickets and CIs become grid nodes linked to cloud resources by explicit, typed edges. |
| 3. | Feed PAIN | ServiceNow's CVSS vector, KEV flag and domain fill the PAIN inputs TAP lacks today. |
| 4. | Least privilege | Read-only API identities, scoped secrets, field allowlists, TLS verified. |
| 5. | Governed actions only | Any write is declared, dry-run by default, approved, audited and reversible where the target allows. |

## Requirement Status

| RID | Name | Status | Notes |
| --- | --- | :---: | --- |
| req-integrations-jira-ingest | [Jira Ingest](#jira-ingest) | Proposed | Phase 1, `tap_plugin.jira` |
| req-integrations-servicenow-ingest | [ServiceNow Ingest](#servicenow-ingest) | Proposed | Phase 1, `tap_plugin.servicenow` |
| req-integrations-correlation | [Correlation](#correlation) | Proposed | Phase 1 |
| req-integrations-reconcile | [Reconciliation](#reconciliation) | Backlog | Phase 2 |
| req-integrations-actions | [Governed Actions](#governed-actions) | Backlog | Phase 3, a new `tap_cares` spec |

## Requirements

### Jira Ingest
----
RID: `req-integrations-jira-ingest`

Status: `Proposed`

#### Implementation

- A `jira_issues` collector reads `GET /rest/api/3/search/jql` with a JQL scope from the collector's
  configuration (for example `project in (OPSTK) AND updated >= <watermark>`), paging with
  `nextPageToken`, with `updated` as the watermark.
- `JiraIssue` nodes carry key, summary, type, status, status category, priority, labels, components,
  assignee display name, created, updated, resolution and URL. The description and comments are not
  ingested: they are free text, may hold CUI, and nothing reads them.
- Self-test calls `GET /rest/api/3/myself` and a one-row search. It fails with a sentence on a 401 or 403
  and never prints the token.
- Secret kind `jira_api_token` (site URL, account email, token), resolved through `SecretRef`.

#### Acceptance Criteria

| ACID | Title | Status | Description | Notes |
| --- | --- | :---: | --- | --- |
| req-integrations-jira-ingest-1 | Incremental | Proposed | A second run reads only issues updated since the watermark. | |
| req-integrations-jira-ingest-2 | Allowlisted Fields | Proposed | Only the listed fields land; description and comments never do. | |
| req-integrations-jira-ingest-3 | Read-Only Identity | Proposed | Self-test confirms the identity and the collector issues GET requests only. | |

### ServiceNow Ingest
----
RID: `req-integrations-servicenow-ingest`

Status: `Proposed`

#### Implementation

- Collectors over the Table API (`GET /api/now/table/<table>`) with `sysparm_fields` set to an allowlist,
  `sysparm_query=sys_updated_on>=<watermark>^ORDERBYsys_updated_on`, and paging by `sysparm_offset`:
  - `servicenow_cmdb`: `cmdb_ci` classes in scope (`cmdb_ci_ec2_instance`, `cmdb_ci_cloud_*`), keeping
    `correlation_id`, object id, name, class, install status and `sys_domain`;
  - `servicenow_itsm`: `incident` and `change_request`, keeping number, state, priority, CI, opened,
    closed and short description;
  - `servicenow_vr`: `sn_vul_vulnerable_item`, `sn_vul_entry` and `sn_vul_nvd_entry`, keeping the CVSS
    v3 vector, `cisa_exists` (KEV), state, CI, first found and the PAIN fields once recorded.
- Self-test reads one row of each table and reports which tables the identity cannot read.
- Secret kind `servicenow_oauth` (instance URL, client id, client secret, using the client-credentials
  grant where the instance allows it, otherwise a read-only integration user). Never a personal account.

#### Acceptance Criteria

| ACID | Title | Status | Description | Notes |
| --- | --- | :---: | --- | --- |
| req-integrations-servicenow-ingest-1 | Incremental | Proposed | Each table reads forward from `sys_updated_on`. | |
| req-integrations-servicenow-ingest-2 | Allowlisted Fields | Proposed | `sysparm_fields` is always set; a table with no allowlist is not read. | |
| req-integrations-servicenow-ingest-3 | PAIN Inputs | Proposed | CVSS v3 vector, KEV and domain reach the PAIN worksheet for a correlated finding. | The posture plugin's PAIN requirement (`spec-posture-v0.md`, outside this tree) |

### Correlation
----
RID: `req-integrations-correlation`

Status: `Proposed`

#### Implementation

- Edges from tickets and CIs to TAP resources, each with the key it matched on, so a wrong match can be
  seen:
  - `REPRESENTS_RESOURCE__servicenow` from a CI to a resource, matched on instance id, ARN or
    `correlation_id`;
  - `TRACKS_RESOURCE__jira` from a Jira issue to a resource, matched on an ARN or instance id in a label
    or a custom field (configured, not parsed from free text);
  - `TRACKS_FINDING__servicenow` from a vulnerable item to a TAP finding or issue, matched on CVE plus CI.
- An ambiguous match (one key, several resources) creates no edge and is reported as a warning.
- The drawer gets a "Tickets" section: linked Jira issues and ServiceNow records, with state and a link.

#### Acceptance Criteria

| ACID | Title | Status | Description | Notes |
| --- | --- | :---: | --- | --- |
| req-integrations-correlation-1 | Explicit Keys | Proposed | Every edge records its matching key; free-text matching is never used. | |
| req-integrations-correlation-2 | Ambiguity Refused | Proposed | A key matching several resources makes no edge and warns. | |

### Reconciliation
----
RID: `req-integrations-reconcile`

Status: `Backlog`

#### Implementation

- CMDB drift: CIs with no live cloud resource, and cloud resources with no CI, per account.
- Ticket coverage: findings and issues with no open ticket, and open tickets whose resource is gone.
- Freshness SLOs per source, shown on the inventory freshness column.
- These are read-only issue rules and pages in the posture plugin, built on the correlation edges.

### Governed Actions
----
RID: `req-integrations-actions`

Status: `Backlog`

#### Implementation

- A new `tap_cares` action spec, not an extension of collectors. An action is declared in the manifest
  with its target, secret kind and the fields it may write.
- Dry-run by default: an action first renders exactly what it would send. Sending requires an approval
  recorded against a named approver, separate from the requester.
- Every send is audited (who, what, when, target record id, response) and idempotent on a key, so a
  retry cannot create a second ticket.
- Then Jira and ServiceNow write-back, modelled on automation rules (trigger source, filter, action):
  create or update a Jira issue from an issue; create or update a ServiceNow vulnerable item, or a POA&M
  entry, carrying the PAIN worksheet fields of method section 10.3.
- Write identities are separate from read identities and are scoped to the tables or projects named.

## Non-Goals

- A general-purpose iPaaS or bidirectional sync.
- Ingesting ticket free text (descriptions, comments, work notes).
- Any write before the action surface exists.

## Security

- No credentials in code, boot files or GRIFT. Every credential is a `*.secret.json` kind resolved
  through `SecretRef`, which the leak and credential-pattern guards already cover.
- TLS is verified on every call. No certificate is embedded; a private CA would come from the
  container trust store.
- ServiceNow and Jira data is treated as CUI. It stays on the instance and is not exported to logs or
  evidence bundles beyond record keys.
