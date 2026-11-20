"""Superuser-only search-role GUCs when boot is not a superuser (req-grid-search-readonly-role.sec-6).

On managed PostgreSQL (RDS, Cloud SQL) the app role that boot connects as is not a
superuser, so ``ALTER ROLE … SET temp_file_limit`` fails with "permission denied to set
parameter". Provisioning must skip such a parameter, log it, and still pin the
user-settable ones. The decision is made from ``has_parameter_privilege``, not from the
``pg_settings.context`` label, so the skip holds whatever context the server reports.
The superuser path is covered authentically by
``test_search_role.py::TestProvisionedRoleEnforcement::test_resource_gucs_are_role_pinned``;
these tests drive the non-superuser branch with a scripted cursor, because the test
database connects as a superuser.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import pytest

from tap_grid import search_role

# Parameters that Postgres only lets a superuser SET (PGC_SUSET), e.g. temp_file_limit.
_SUPERUSER_ONLY = {"temp_file_limit"}
_KNOWN = {"statement_timeout", "lock_timeout", "work_mem", "temp_file_limit"}
_GUCS = {"statement_timeout": "30s", "lock_timeout": "5s", "work_mem": "64MB", "temp_file_limit": "1GB"}


class _Cursor:
    def __init__(self, *, superuser: bool) -> None:
        self.superuser = superuser
        self.statements: list[str] = []
        self._row: tuple[Any, ...] | None = None

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: list[str] | None = None) -> None:
        self.statements.append(sql)
        if sql.startswith("SELECT 1 FROM pg_roles"):
            self._row = (1,)
        elif sql.startswith("SELECT has_parameter_privilege"):
            name = params[0] if params else ""
            if name in _KNOWN:
                self._row = (self.superuser or name not in _SUPERUSER_ONLY,)
            else:
                self._row = None
        else:
            self._row = None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class _Connection:
    alias = "default"

    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _Cursor:
        return self._cursor


def _pinned(cursor: _Cursor) -> set[str]:
    prefix = f'ALTER ROLE "{search_role.search_role_name()}" SET '
    return {s[len(prefix) :].split(" = ", 1)[0] for s in cursor.statements if s.startswith(prefix)}


@pytest.fixture
def _no_db(monkeypatch):
    monkeypatch.setattr("tap_grid.search_role.transaction.atomic", lambda **_: contextlib.nullcontext())
    monkeypatch.setattr("tap_grid.grid_tables.classified_but_absent", lambda cur, declared: [])


@pytest.mark.usefixtures("_no_db")
class TestSuperuserOnlyGucs:
    def _provision(self, *, superuser: bool) -> _Cursor:
        cur = _Cursor(superuser=superuser)
        search_role.provision_search_role(
            _Connection(cur), password="pw", database="tap", gucs=_GUCS, tables=["tap_entity"]
        )
        return cur

    def test_non_superuser_skips_temp_file_limit_and_pins_the_rest(self, caplog):
        with caplog.at_level(logging.WARNING, logger="tap_grid.search_role"):
            cur = self._provision(superuser=False)
        assert _pinned(cur) == {"statement_timeout", "lock_timeout", "work_mem"}
        assert any("temp_file_limit is superuser-only" in r.getMessage() for r in caplog.records)

    def test_superuser_pins_every_guc(self, caplog):
        with caplog.at_level(logging.WARNING, logger="tap_grid.search_role"):
            cur = self._provision(superuser=True)
        assert _pinned(cur) == set(_GUCS)
        assert not any("superuser-only" in r.getMessage() for r in caplog.records)

    def test_unknown_guc_is_treated_as_user_settable(self):
        cur = _Cursor(superuser=False)
        search_role.provision_search_role(
            _Connection(cur), password="pw", database="tap", gucs={"idle_session_timeout": "60s"}, tables=[]
        )
        assert _pinned(cur) == {"idle_session_timeout"}
