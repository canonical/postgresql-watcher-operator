# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from unittest.mock import MagicMock, call, patch

import pytest
from charmlibs import snap
from ops import ActiveStatus, MaintenanceStatus
from single_kernel_postgresql.config.enums import Substrates

from charm import PostgresqlWatcherCharm, _PostgreSQLRefresh
from constants import SNAP_COMMON_PATH
from relations.watcher_requirer import WatcherRequirerHandler


@pytest.mark.parametrize("present,refreshing", [(False, False), (True, False), (True, True)])
def test_install_snap_protects_before_install_or_refresh(present, refreshing):
    charm = MagicMock()
    refresh = MagicMock() if refreshing else None
    package = MagicMock(present=present)
    ordered = MagicMock()
    with (
        patch("charm.ensure_snap_oom_protection") as protect,
        patch("charm.snap.SnapCache") as cache,
    ):
        cache.return_value.__getitem__.return_value = package
        ordered.attach_mock(protect, "protect")
        ordered.attach_mock(cache, "cache")
        PostgresqlWatcherCharm._install_snap_package(charm, revision="416", refresh=refresh)

    assert ordered.mock_calls[:3] == [
        call.protect("charmed-postgresql"),
        call.cache(),
        call.cache().__getitem__("charmed-postgresql"),
    ]
    if not present or refreshing:
        package.ensure.assert_called_once_with(snap.SnapState.Present, revision="416")
        package.hold.assert_called_once_with()
    else:
        package.ensure.assert_not_called()
        package.hold.assert_not_called()
    if refresh is not None:
        refresh.update_snap_revision.assert_called_once_with()
    package.start.assert_not_called()
    package.restart.assert_not_called()


def test_install_snap_reads_pinned_revision():
    package = MagicMock(present=False)
    with (
        patch("charm.ensure_snap_oom_protection") as protect,
        patch("charm.snap.SnapCache") as cache,
        patch("charm.platform.machine", return_value="x86_64"),
    ):
        cache.return_value.__getitem__.return_value = package
        PostgresqlWatcherCharm._install_snap_package(MagicMock(), revision=None)
    protect.assert_called_once_with("charmed-postgresql")
    package.ensure.assert_called_once_with(snap.SnapState.Present, revision="416")


def test_install_snap_propagates_protection_failure():
    with (
        patch("charm.ensure_snap_oom_protection", side_effect=snap.SnapError("failed")),
        patch("charm.snap.SnapCache") as cache,
        pytest.raises(snap.SnapError),
    ):
        PostgresqlWatcherCharm._install_snap_package(MagicMock(), revision="416")
    cache.assert_not_called()


def test_install_event_keeps_snap_and_service_order():
    handler = MagicMock()
    ordered = MagicMock()
    ordered.attach_mock(handler.charm._install_snap_package, "install_snap")
    with (
        patch("relations.watcher_requirer._change_owner") as change_owner,
        patch("relations.watcher_requirer.install_service") as install_service,
    ):
        ordered.attach_mock(change_owner, "change_owner")
        ordered.attach_mock(install_service, "install_service")
        WatcherRequirerHandler._on_install(handler, MagicMock())
    assert ordered.mock_calls == [
        call.install_snap(revision=None),
        call.change_owner(Substrates.VM, SNAP_COMMON_PATH),
        call.install_service(),
    ]
    handler.charm.watcher_requirer.start_services.assert_not_called()


def test_refresh_keeps_existing_lifecycle():
    charm = MagicMock()
    refresh = MagicMock()
    specific = _PostgreSQLRefresh(
        workload_name="PostgreSQL", charm_name="postgresql", _charm=charm
    )
    ordered = MagicMock()
    ordered.attach_mock(charm, "charm")
    with patch("charm._change_owner") as change_owner:
        ordered.attach_mock(change_owner, "change_owner")
        specific.refresh_snap(snap_name="charmed-postgresql", snap_revision="416", refresh=refresh)
    assert ordered.mock_calls == [
        call.charm.set_unit_status(MaintenanceStatus("refreshing the snap"), refresh=refresh),
        call.charm.watcher_requirer.stop_services(),
        call.change_owner(Substrates.VM, SNAP_COMMON_PATH),
        call.charm._install_snap_package(revision="416", refresh=refresh),
        call.charm._post_snap_refresh(refresh),
    ]


def test_post_refresh_renders_service_before_existing_start():
    charm = MagicMock()
    refresh = MagicMock(next_unit_allowed_to_refresh=False)
    ordered = MagicMock()
    ordered.attach_mock(charm, "charm")
    with patch("charm.install_service") as install_service:
        ordered.attach_mock(install_service, "install_service")
        PostgresqlWatcherCharm._post_snap_refresh(charm, refresh)
    assert ordered.mock_calls == [
        call.install_service(),
        call.charm.watcher_requirer.start_services(),
        call.charm.set_unit_status(ActiveStatus(), refresh=refresh),
    ]
    assert refresh.next_unit_allowed_to_refresh
