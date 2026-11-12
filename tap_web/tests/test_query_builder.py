"""Tests for spec-web-query-builder: parsing, compilation, the read gate and the export."""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.test import Client

from tap.pytest_harness import make_admin_client
from tap_web.query_builder import Condition, Hop, QueryError, QuerySpec, compile_gryphon, parse

SOURCE = "grid_fixtures__constrained_source"
TARGET = "grid_fixtures__constrained_target"
LINK = "CONSTRAINED_LINK__grid_fixtures"


def _seed() -> tuple[Any, Any]:
    from tap_plugin.grid_fixtures.models import ConstrainedSource, ConstrainedTarget

    from tap_grid.services import create_edge

    a = ConstrainedSource.objects.create(name="alpha")
    ConstrainedSource.objects.create(name="bravo")
    t = ConstrainedTarget.objects.create(name="target-a")
    create_edge(a.entity, t.entity, LINK)
    return a, t


class TestCompile:
    @pytest.mark.spec("req-web-query-builder-2")
    def test_values_become_parameters(self):
        spec = QuerySpec(
            SOURCE,
            [Hop(LINK, "out", TARGET)],
            [Condition("n0", "name", "eq", 'a" OR 1=1'), Condition("n1", "name", "starts", "tar")],
        )
        lines, inputs, schema = compile_gryphon(spec)
        assert lines[0] == f"MATCH (n0:{SOURCE})-[:{LINK}]->(n1:{TARGET})"
        assert lines[1] == "WHERE n0.name = $p0 AND n1.name STARTS_WITH $p1"
        assert inputs == {"p0": 'a" OR 1=1', "p1": "tar"}
        assert schema["required"] == ["p0", "p1"]
        assert "OR 1=1" not in "\n".join(lines)

    @pytest.mark.spec("req-web-query-builder-4")
    def test_literal_export_escapes_quotes_and_backslashes(self):
        spec = QuerySpec(SOURCE, [], [Condition("n0", "name", "eq", 'a"b\\c'), Condition("n0", "name", "set")])
        lines, inputs, _ = compile_gryphon(spec, literal=True)
        assert lines[1] == 'WHERE n0.name = "a\\"b\\\\c" AND n0.name IS NOT NULL'
        assert inputs == {}

    def test_inbound_steps_and_typed_values(self):
        spec = QuerySpec(TARGET, [Hop(LINK, "in", SOURCE)], [Condition("n0", "name", "gt", "3")])
        lines, inputs, schema = compile_gryphon(spec)
        assert lines[0] == f"MATCH (n0:{TARGET})<-[:{LINK}]-(n1:{SOURCE})"
        assert inputs == {"p0": 3} and schema["properties"]["p0"] == {"type": "integer"}


@pytest.mark.django_db
class TestParse:
    @pytest.mark.spec("req-web-query-builder-1")
    def test_undeclared_names_are_refused(self):
        for raw in (
            "t=page",
            f"t={SOURCE}&h=NOPE:sideways:{TARGET}",
            f"t={SOURCE}&c=n0.password:eq:x",
            f"t={SOURCE}&c=n0.name:drop:x",
            f"t={SOURCE}&c=n5.name:eq:x",
        ):
            with pytest.raises(QueryError):
                parse(QueryDict(raw))

    @pytest.mark.spec("req-web-query-builder-4")
    def test_a_line_break_is_refused(self):
        with pytest.raises(QueryError):
            parse(QueryDict(f"t={SOURCE}&c=n0.name:eq:a%0Ab"))

    def test_a_declared_query_parses(self):
        spec = parse(QueryDict(f"t={SOURCE}&h={LINK}:out:{TARGET}&c=n1.name:contains:tar"))
        assert spec.hops == [Hop(LINK, "out", TARGET)] and spec.conditions == [
            Condition("n1", "name", "contains", "tar")
        ]


# transaction=True: the page executes through the `search_readonly` connection, which sees only
# committed rows.
@pytest.mark.django_db(transaction=True, databases=["default", "search_readonly"])
class TestPage:
    @pytest.mark.spec("req-web-query-builder-3")
    def test_runs_and_links_results_into_the_drawer(self):
        _seed()
        body = (
            make_admin_client(username="qb-admin").get(f"/__query/?t={SOURCE}&h={LINK}:out:{TARGET}").content.decode()
        )
        assert "alpha" in body and "bravo" not in body.split("tap-qb-table", 1)[-1]
        assert f'data-tap-drawer="/object/{TARGET}/target-a--' in body
        assert "MATCH (n0:" in body

    @pytest.mark.spec("req-web-query-builder-3")
    def test_requires_grid_read(self):
        user, _ = get_user_model().objects.get_or_create(username="qb-nocaps")
        client = Client()
        client.force_login(user)
        assert client.get(f"/__query/?t={SOURCE}").status_code == 403
        assert client.get(f"/__query/export.grift.json?t={SOURCE}").status_code == 403

    @pytest.mark.spec("req-web-query-builder-5")
    def test_adding_a_condition_redirects_to_the_canonical_url(self):
        _seed()
        resp = make_admin_client(username="qb-admin").get(f"/__query/?t={SOURCE}&h=&cv=n0.name&co=contains&cval=alp")
        assert resp.status_code == 302 and resp["Location"] == f"/__query/?t={SOURCE}&c=n0.name%3Acontains%3Aalp"

    @pytest.mark.spec("req-web-query-builder-5")
    def test_relationship_choices_come_from_the_grid(self):
        _seed()
        body = make_admin_client(username="qb-admin").get(f"/__query/?t={SOURCE}").content.decode()
        assert f'<option value="{LINK}:out:{TARGET}">' in body

    @pytest.mark.spec("req-web-query-builder-4")
    def test_export_is_one_gryphon_search(self):
        _seed()
        resp = make_admin_client(username="qb-admin").get(
            f"/__query/export.grift.json?t={SOURCE}&c=n0.name:eq:alpha&name=Alpha only"
        )
        assert resp.status_code == 200 and 'filename="alpha-only.grift.json"' in resp["Content-Disposition"]
        nodes = json.loads(resp.content)["batches"][0]["nodes"]
        assert len(nodes) == 1 and nodes[0]["entity"]["entity_type"] == "search"
        assert nodes[0]["node"]["definition"]["query"][1] == 'WHERE n0.name = "alpha"'
