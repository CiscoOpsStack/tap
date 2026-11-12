"""Tests for the palette's entity index (req-web-nav-command-palette-3) and the scope lens (req-web-nav-scope-lens)."""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client

from tap.pytest_harness import make_admin_client
from tap_web.scope import Scope, apply_scope, register_scope_labeler, scope_options


def _viewer() -> Client:
    user, _ = get_user_model().objects.get_or_create(username="scope-viewer")
    user.groups.add(Group.objects.get(name="tap_viewer"))
    client = Client()
    client.force_login(user)
    return client


def _source(name: str, dims: dict[str, str] | None = None) -> Any:
    from tap_grid.models import Entity
    from tap_plugin.grid_fixtures.models import ConstrainedSource

    obj = ConstrainedSource.objects.create(name=name)
    if dims is not None:
        Entity.objects.filter(pk=obj.entity_id).update(dimensions=dims)
    return obj


@pytest.mark.django_db
class TestEntitySearch:
    @pytest.mark.spec("req-web-nav-command-palette-3")
    def test_finds_by_name_substring_and_ranks_exact_first(self):
        _source("splunkds1-old")
        _source("splunkds1")
        _source("unrelated")
        payload = _viewer().get("/__entity-search.json?q=splunkds1").json()
        names = [e["name"] for e in payload["entities"]]
        assert names == ["splunkds1", "splunkds1-old"]
        hit = payload["entities"][0]
        assert hit["entity_type"] == "grid_fixtures__constrained_source"
        assert hit["drawer_url"].endswith("/drawer/") and hit["page_url"].startswith("/object/")

    @pytest.mark.spec("req-web-nav-command-palette-3")
    def test_finds_by_arn_fragment_and_entity_id(self):
        role = _source("arn:aws-us-gov:iam::123456789012:role/Ops")
        client = _viewer()
        by_arn = client.get("/__entity-search.json?q=123456789012:role").json()["entities"]
        by_id = client.get(f"/__entity-search.json?q={role.entity_id}").json()["entities"]
        assert [e["entity_id"] for e in by_arn] == [str(role.entity_id)]
        assert [e["entity_id"] for e in by_id] == [str(role.entity_id)]

    @pytest.mark.spec("req-web-nav-command-palette-3")
    def test_platform_rows_and_short_terms_are_not_offered(self):
        from tap_web.models import Page

        Page.objects.create(name="splunk page", slug="/splunk", layout={"columns": {"col-1": {"width": "1fr", "rows": {"row-1": {"panel-id": "x"}}}}})
        client = _viewer()
        assert client.get("/__entity-search.json?q=splunk").json()["entities"] == []
        assert client.get("/__entity-search.json?q=s").json()["entities"] == []

    @pytest.mark.spec("req-web-nav-command-palette-3")
    def test_requires_grid_read(self):
        user, _ = get_user_model().objects.get_or_create(username="no-caps")
        client = Client()
        client.force_login(user)
        assert client.get("/__entity-search.json?q=abc").status_code == 403


class TestApplyScope:
    @pytest.mark.spec("req-web-nav-scope-lens-2")
    def test_rows_without_an_entity_or_the_key_stay(self, monkeypatch):
        import tap_web.scope as scope_mod

        dims = {"a": {"aws_account": "111"}, "b": {"aws_account": "222"}, "c": {"aws_region": "x"}}
        monkeypatch.setattr(scope_mod, "_dimensions", lambda ids: {i: dims[i] for i in ids if i in dims})
        rows = [{"entity_id": "a"}, {"entity_id": "b"}, {"entity_id": "c"}, {"name": "no entity"}]
        kept, hidden = apply_scope(rows, Scope("aws_account", "111", "stage"))
        assert [r.get("entity_id", r.get("name")) for r in kept] == ["a", "c", "no entity"]
        assert hidden == 1

    @pytest.mark.spec("req-web-nav-scope-lens-2")
    def test_the_drawer_id_field_names_the_entity(self, monkeypatch):
        import tap_web.scope as scope_mod

        monkeypatch.setattr(scope_mod, "_dimensions", lambda ids: {"h1": {"aws_account": "222"}})
        kept, hidden = apply_scope([{"host_id": "h1"}], Scope("aws_account", "111", "111"), id_field="host_id")
        assert kept == [] and hidden == 1

    def test_no_scope_is_a_no_op(self):
        rows = [{"entity_id": "a"}]
        assert apply_scope(rows, None) == (rows, 0)


@pytest.mark.django_db
class TestScopeOptionsAndEndpoint:
    @pytest.mark.spec("req-web-nav-scope-lens-1")
    def test_options_skip_reserved_and_single_valued_keys(self):
        _source("a", {"aws_account": "111", "cloud": "aws", "tap.cloud": "aws"})
        _source("b", {"aws_account": "222", "cloud": "aws", "tap.cloud": "aws"})
        options = [(o.key, o.value) for o in scope_options()]
        assert ("aws_account", "111") in options and ("aws_account", "222") in options
        assert not any(k in ("cloud", "tap.cloud") for k, _ in options)

    @pytest.mark.spec("req-web-nav-scope-lens-1")
    def test_a_labeler_names_values(self, monkeypatch):
        import tap_web.scope as scope_mod

        monkeypatch.setattr(scope_mod, "_LABELERS", {})
        register_scope_labeler("aws_account", lambda values: {"111": "ops stack stage"})
        _source("a", {"aws_account": "111"})
        _source("b", {"aws_account": "222"})
        labels = {o.value: o.label for o in scope_options() if o.key == "aws_account"}
        assert labels == {"111": "ops stack stage", "222": "222"}

    @pytest.mark.spec("req-web-nav-scope-lens-3")
    def test_set_and_clear_round_trip_in_the_session(self):
        _source("a", {"aws_account": "111"})
        _source("b", {"aws_account": "222"})
        client = make_admin_client(username="scope-admin")
        set_resp = client.post("/__scope.json", {"key": "aws_account", "value": "111"}, HTTP_ACCEPT="application/json")
        assert set_resp.status_code == 200
        assert set_resp.json()["active"]["value"] == "111"
        assert client.get("/__scope.json").json()["active"]["key"] == "aws_account"
        cleared = client.post("/__scope.json", {"clear": "1"}, HTTP_ACCEPT="application/json")
        assert cleared.json()["active"] is None

    @pytest.mark.spec("req-web-nav-scope-lens-3")
    def test_an_unoffered_value_is_refused(self):
        _source("a", {"aws_account": "111"})
        _source("b", {"aws_account": "222"})
        client = make_admin_client(username="scope-admin")
        resp = client.post("/__scope.json", {"key": "aws_account", "value": "999"}, HTTP_ACCEPT="application/json")
        assert resp.status_code == 400

    @pytest.mark.spec("req-web-nav-scope-lens-4")
    def test_form_post_redirects_to_a_same_origin_next_only(self):
        _source("a", {"aws_account": "111"})
        _source("b", {"aws_account": "222"})
        client = make_admin_client(username="scope-admin")
        ok = client.post("/__scope.json", {"clear": "1", "next": "/stage/hosts?x=1"})
        evil = client.post("/__scope.json", {"clear": "1", "next": "https://evil.example/"})
        assert ok.status_code == 302 and ok["Location"] == "/stage/hosts?x=1"
        assert evil["Location"] == "/"
