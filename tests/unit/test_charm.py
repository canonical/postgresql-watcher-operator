# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from types import MethodType
from unittest.mock import MagicMock, call, patch

import pytest
from charmlibs import snap

from charm import PostgresqlWatcherCharm, _PostgreSQLRefresh


@pytest.fixture
def snap_api():
    with (
        patch("charm.charm_refresh.snap_name", return_value="configured-snap") as name,
        patch("charm.snap.list_one") as query,
        patch("charm.snap.ensure_installed", return_value=False) as ensure,
        patch("charm.snap.hold") as hold,
    ):
        calls = MagicMock()
        calls.attach_mock(query, "query")
        calls.attach_mock(ensure, "ensure")
        calls.attach_mock(hold, "hold")
        yield name, calls


@pytest.mark.parametrize("installed,refreshing", [(False, False), (True, False), (True, True)])
def test_install_snap_package(snap_api, installed, refreshing):
    name, calls = snap_api
    if not installed:
        calls.query.side_effect = snap.NotInstalledError(
            "missing", kind="snap-not-found", value=None
        )
    refresh = MagicMock() if refreshing else None
    if refresh is not None:
        calls.attach_mock(refresh.update_snap_revision, "update")

    PostgresqlWatcherCharm._install_snap_package(MagicMock(), revision="428", refresh=refresh)

    name.assert_called_once_with()
    expected = [call.query("configured-snap")]
    if not installed or refreshing:
        expected.append(call.ensure("configured-snap", revision="428"))
        if refreshing:
            expected.append(call.update())
        expected.append(call.hold("configured-snap"))
    assert calls.mock_calls == expected


@pytest.mark.parametrize(
    "error",
    [snap.ConnectionError("unreachable"), snap.APIError("failed", kind="unknown", value=None)],
)
def test_install_query_failure_propagates(snap_api, error):
    name, calls = snap_api
    calls.query.side_effect = error
    refresh = MagicMock()

    with pytest.raises(type(error)) as caught:
        PostgresqlWatcherCharm._install_snap_package(MagicMock(), revision="428", refresh=refresh)

    assert caught.value is error
    name.assert_called_once_with()
    assert calls.mock_calls == [call.query("configured-snap")]
    refresh.update_snap_revision.assert_not_called()


def test_failed_refresh_install_does_not_start_services(snap_api):
    _, calls = snap_api
    error = snap.ConnectionError("install failed")
    calls.ensure.side_effect = error
    charm = MagicMock()
    charm._install_snap_package = MethodType(PostgresqlWatcherCharm._install_snap_package, charm)
    refresh = MagicMock()
    callback = _PostgreSQLRefresh(
        workload_name="PostgreSQL", charm_name="postgresql", _charm=charm
    )

    with patch("charm._change_owner"), pytest.raises(snap.ConnectionError) as caught:
        callback.refresh_snap(snap_name="configured-snap", snap_revision="428", refresh=refresh)

    assert caught.value is error
    assert calls.mock_calls == [
        call.query("configured-snap"),
        call.ensure("configured-snap", revision="428"),
    ]
    refresh.update_snap_revision.assert_not_called()
    charm.watcher_requirer.stop_services.assert_called_once_with()
    charm._post_snap_refresh.assert_not_called()
