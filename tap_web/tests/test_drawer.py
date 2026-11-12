"""Tests for spec-web-drawer: the resource drawer's region, fragment route and categories."""

from __future__ import annotations

import re
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from tap.pytest_harness import make_admin_client
from tap_web.drawer import EdgeGroup, Neighbour, base_edge_type, context_rows, edge_label, is_finding_edge
from tap_web.models import Page
from tap_web.page import build_url_id

_MINIMAL_LAYOUT = {"columns": {"col-1": {"width": "1fr", "rows": {"row-1": {"panel-id": "x"}}}}}
CATEGORIES = ["overview", "findings", "relationships", "configuration", "tags", "provenance", "raw"]


def _section(body: str, category: str) -> str:
    match = re.search(rf'data-tap-drawer-category="{category}".*?</section>', body, re.S)
    assert match, f"no {category} section"
    return match.group(0)


def _drawer_url(entity_type: str, name: str, entity_id: object) -> str:
    return f"/object/{entity_type}/{build_url_id(name, entity_id)}/drawer/"


class TestFindingRule:
    @pytest.mark.spec("req-web-drawer-categories-2")
    def test_incoming_concerns_resource_is_a_finding(self):
        assert is_finding_edge("CONCERNS_RESOURCE__aws_core", "in", None)
        assert not is_finding_edge("CONCERNS_RESOURCE__aws_core", "out", None)

    @pytest.mark.spec("req-web-drawer-categories-2")
    def test_outgoing_finding_holder_is_a_finding(self):
        assert is_finding_edge("HOLDS_SECURITYHUB_FINDING__aws_core", "out", None)
        assert not is_finding_edge("OWNS_IAM_ROLE__aws_core", "out", None)

    @pytest.mark.spec("req-web-drawer-categories-2")
    def test_declared_finding_model_is_a_finding(self):
        class Finding:
            DRAWER_ROLE = "finding"

        assert is_finding_edge("ANY_LINK", "out", Finding)

    def test_edge_labels_drop_the_plugin_suffix(self):
        assert base_edge_type("RESIDES_IN_VPC__aws_core") == "RESIDES_IN_VPC"
        assert edge_label("RESIDES_IN_VPC__aws_core") == "Resides in vpc"


class TestOverview:
    @staticmethod
    def _group(edge_type: str, direction: str, total: int = 1) -> EdgeGroup:
        n = Neighbour(
            name="gcc-fed-stage",
            entity_type="aws_core__aws_account",
            drawer_url="",
            page_url="",
            type_name="AWS Account",
        )
        return EdgeGroup(
            label=edge_label(edge_type), direction=direction, neighbours=[n], total=total, edge_type=edge_type
        )

    @pytest.mark.spec("req-web-drawer-categories-4")
    def test_a_single_outgoing_account_edge_is_an_overview_row(self):
        rows = context_rows(
            [self._group("BELONGS_TO_ACCOUNT__aws_core", "out"), self._group("RESIDES_IN_VPC__aws_core", "out")]
        )
        assert [(r.label, r.text) for r in rows] == [("AWS Account", "gcc-fed-stage")]

    @pytest.mark.spec("req-web-drawer-categories-4")
    def test_the_resource_a_finding_concerns_leads_the_overview(self):
        bucket = Neighbour(
            name="logs-bucket", entity_type="aws_core__aws_s3_bucket", drawer_url="", page_url="", type_name="S3 Bucket"
        )
        concerns = EdgeGroup(
            label="Concerns resource",
            direction="out",
            neighbours=[bucket],
            total=1,
            edge_type="CONCERNS_RESOURCE__aws_core",
        )
        rows = context_rows([self._group("BELONGS_TO_ACCOUNT__aws_core", "out"), concerns])
        assert [(r.label, r.text) for r in rows] == [
            ("Resource", "logs-bucket (S3 Bucket)"),
            ("AWS Account", "gcc-fed-stage"),
        ]

    @pytest.mark.spec("req-web-drawer-categories-4")
    def test_labels_keep_acronyms(self):
        from types import SimpleNamespace

        from tap_web.panels.viewer_panel import field_label

        assert field_label(SimpleNamespace(verbose_name="private_ip", name="private_ip")) == "Private IP"
        assert field_label(SimpleNamespace(verbose_name="", name="ec2_type")) == "EC2 Type"
        assert field_label(SimpleNamespace(verbose_name="first observed at", name="x")) == "First Observed At"
        assert field_label(SimpleNamespace(verbose_name="resource_ids", name="resource_ids")) == "Resource IDs"

    @pytest.mark.spec("req-web-drawer-categories-4")
    def test_incoming_or_many_account_edges_are_not(self):
        assert (
            context_rows([self._group("BELONGS_TO_ACCOUNT__aws_core", "in"), self._group("IN_ACCOUNT", "out", total=3)])
            == []
        )


@pytest.mark.django_db
class TestFragment:
    @staticmethod
    def _pair() -> tuple[Any, Any]:
        from tap_plugin.grid_fixtures.models import ConstrainedSource, ConstrainedTarget

        from tap_grid.services import create_edge

        source = ConstrainedSource.objects.create(
            name="arn:aws-us-gov:iam::123456789012:role/Ops Role",
            tags={"deployed_from": "infra/roles/main.tf", "team": "ops"},
        )
        target = ConstrainedTarget.objects.create(name="target-a")
        create_edge(source.entity, target.entity, "CONSTRAINED_LINK__grid_fixtures")
        return source, target

    @pytest.mark.spec("req-web-drawer-fragment-1")
    @pytest.mark.spec("req-web-drawer-categories-1")
    def test_fragment_renders_all_categories_for_a_slash_name(self):
        source, _ = self._pair()
        url = _drawer_url("grid_fixtures__constrained_source", source.entity.name, source.entity_id)
        response = make_admin_client(username="drawer-admin").get(url)
        assert response.status_code == 200
        body = response.content.decode()
        assert "<html" not in body
        assert re.findall(r'data-tap-drawer-category="([a-z]+)"', body) == CATEGORIES
        assert 'id="tap-drawer-title"' in body and "Ops Role" in body
        assert "Open full page" in body

    @pytest.mark.spec("req-web-drawer-categories-1")
    def test_relationships_tags_and_declared_in(self):
        source, _ = self._pair()
        url = _drawer_url("grid_fixtures__constrained_source", source.entity.name, source.entity_id)
        body = make_admin_client(username="drawer-admin").get(url).content.decode()
        relationships = _section(body, "relationships")
        assert "Constrained link" in relationships and "target-a" in relationships
        assert 'data-tap-drawer="/object/grid_fixtures__constrained_target/target-a--' in relationships

    @pytest.mark.spec("req-web-drawer-categories-4")
    def test_neighbours_show_their_type_name_not_its_slug(self):
        from tap_grid.models import EntityType

        source, _ = self._pair()
        EntityType.objects.update_or_create(
            slug="grid_fixtures__constrained_target", defaults={"name": "Constrained target"}
        )
        url = _drawer_url("grid_fixtures__constrained_source", source.entity.name, source.entity_id)
        body = make_admin_client(username="drawer-admin").get(url).content.decode()
        relationships = _section(body, "relationships")
        assert '<span class="tap-drawer-type">Constrained target</span>' in relationships
        assert "infra/roles/main.tf" in body  # the deployed_from tag lifted to "Declared in"
        assert re.search(r"<dt>team</dt><dd>ops</dd>", body)

    @pytest.mark.spec("req-web-drawer-categories-3")
    def test_empty_categories_say_what_was_looked_for(self):
        _, target = self._pair()
        url = _drawer_url("grid_fixtures__constrained_target", target.entity.name, target.entity_id)
        body = make_admin_client(username="drawer-admin").get(url).content.decode()
        findings = _section(body, "findings")
        assert "tap-drawer-empty" in findings and "concerns resource" in findings
        tags = _section(body, "tags")
        assert "No tags recorded" in tags

    @pytest.mark.spec("req-web-drawer-fragment-2")
    def test_anonymous_is_refused(self):
        source, _ = self._pair()
        url = _drawer_url("grid_fixtures__constrained_source", source.entity.name, source.entity_id)
        response = Client().get(url)
        assert response.status_code in (302, 401, 403)

    @pytest.mark.spec("req-web-drawer-categories-5")
    def test_the_resource_row_opens_the_resource(self):
        from django.template.loader import render_to_string

        bucket = Neighbour(
            name="logs-bucket",
            entity_type="aws_core__aws_s3_bucket",
            drawer_url="/object/b/logs--1/drawer/",
            page_url="/object/b/logs--1/",
            type_name="S3 Bucket",
        )
        concerns = EdgeGroup(
            label="Concerns resource",
            direction="out",
            neighbours=[bucket],
            total=1,
            edge_type="CONCERNS_RESOURCE__aws_core",
        )
        rows = context_rows([concerns])
        assert (rows[0].drawer_url, rows[0].page_url) == ("/object/b/logs--1/drawer/", "/object/b/logs--1/")
        body = render_to_string("tap_web/partials/drawer.html", {"drawer_overview": rows, "drawer_sections": []})
        assert (
            '<dt>Resource</dt><dd><a href="/object/b/logs--1/" data-tap-drawer="/object/b/logs--1/drawer/">logs-bucket (S3 Bucket)</a></dd>'
            in body
        )

    @pytest.mark.spec("req-web-drawer-sections-1")
    @pytest.mark.spec("req-web-drawer-sections-3")
    def test_a_registered_section_renders_with_a_tab(self, monkeypatch):
        import tap_web.drawer as drawer_mod
        from tap_web.drawer import DrawerSection, SectionRow

        monkeypatch.setattr(drawer_mod, "_SECTIONS", {})
        drawer_mod.register_drawer_section(
            "issues",
            lambda t, i, o: DrawerSection(
                "issues", "Issues", [SectionRow("Public and vulnerable", "High", page_url="/stage/issues")]
            ),
        )
        drawer_mod.register_drawer_section("absent", lambda t, i, o: None)
        source, _ = self._pair()
        body = (
            make_admin_client(username="drawer-admin")
            .get(_drawer_url("grid_fixtures__constrained_source", source.entity.name, source.entity_id))
            .content.decode()
        )
        assert re.findall(r'data-tap-drawer-category="([a-z]+)"', body) == CATEGORIES
        assert re.findall(r'data-tap-drawer-extra="([a-z]+)"', body) == ["issues"]
        assert 'href="#tap-dr-x-issues"' in body and '<a href="/stage/issues">High</a>' in body
        assert body.index('data-tap-drawer-category="findings"') < body.index('data-tap-drawer-extra="issues"')
        assert re.search(
            r'class="tap-drawer-graph" href="/object/grid_fixtures__constrained_source/[^"]+#slot-graph"', body
        )

    @pytest.mark.spec("req-web-drawer-sections-2")
    def test_a_failing_section_is_left_out(self, monkeypatch):
        import tap_web.drawer as drawer_mod

        def boom(*_):
            raise RuntimeError("plugin bug")

        monkeypatch.setattr(drawer_mod, "_SECTIONS", {"boom": boom})
        source, _ = self._pair()
        response = make_admin_client(username="drawer-admin").get(
            _drawer_url("grid_fixtures__constrained_source", source.entity.name, source.entity_id)
        )
        assert response.status_code == 200 and "data-tap-drawer-extra" not in response.content.decode()

    @pytest.mark.spec("req-web-drawer-fragment-3")
    def test_unknown_entity_is_a_404_sentence(self):
        url = _drawer_url("grid_fixtures__constrained_source", "gone", "0198c2f0-0000-7000-8000-00000000dead")
        response = make_admin_client(username="drawer-admin").get(url)
        assert response.status_code == 404
        assert "not in the grid any more" in response.content.decode()


@pytest.mark.django_db
class TestRegion:
    @pytest.mark.spec("req-web-drawer-1")
    def test_region_is_in_chrome_for_a_reader(self):
        Page.objects.create(name="Ops", slug="/ops", layout=_MINIMAL_LAYOUT)
        body = make_admin_client(username="drawer-admin").get("/ops").content.decode()
        assert re.search(
            r'<aside id="tap-drawer"[^>]*role="region"[^>]*aria-labelledby="tap-drawer-title"[^>]*hidden', body
        )
        assert "drawer.js" in body

    @pytest.mark.spec("req-web-drawer-1")
    def test_no_region_without_read(self):
        Page.objects.create(name="Ops", slug="/ops", layout=_MINIMAL_LAYOUT)
        user, _ = get_user_model().objects.get_or_create(username="drawer-noread")
        client = Client()
        client.force_login(user)
        assert 'id="tap-drawer"' not in client.get("/ops").content.decode()
