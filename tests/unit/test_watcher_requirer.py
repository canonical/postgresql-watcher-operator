# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the watcher requirer relation handler."""

import json
from unittest.mock import MagicMock, patch

import pytest
from ops import ActiveStatus, BlockedStatus, WaitingStatus

from src.relations.watcher_requirer import WatcherRequirerHandler

MODULE = "src.relations.watcher_requirer"


def create_mock_charm(profile="testing"):
    """Create a mock charm for watcher requirer testing."""
    mock_charm = MagicMock()
    mock_charm.config = MagicMock()
    mock_charm.config.profile = profile
    mock_charm.unit.name = "pg-watcher/0"
    return mock_charm


def create_mock_relation(units_with_az=None, app_data=None, charm=None, local_app_data=None):
    """Create a mock relation with units that have AZ data.

    Args:
        units_with_az: Dict mapping unit names to their AZ values.
            Example: {"postgresql/0": "az1", "postgresql/1": "az2"}
        app_data: Provider (PostgreSQL) application databag contents.
        charm: When given, add real dict databags for the charm's own app and unit
            so writes like relation.data[charm.app]["raft-status"] can be asserted.
        local_app_data: Initial contents of the charm's own app databag.
    """
    mock_relation = MagicMock()
    mock_relation.id = 42

    if units_with_az is None:
        units_with_az = {}

    mock_units = []
    mock_data = {}
    for unit_name, az in units_with_az.items():
        mock_unit = MagicMock()
        mock_unit.name = unit_name
        mock_units.append(mock_unit)
        unit_data = {}
        if az is not None:
            unit_data["unit-az"] = az
        mock_data[mock_unit] = unit_data

    mock_relation.units = set(mock_units)
    mock_relation.app = MagicMock()
    mock_relation.app.name = "postgresql"
    mock_data[mock_relation.app] = dict(app_data) if app_data else {}
    if charm is not None:
        mock_data[charm.app] = dict(local_app_data) if local_app_data else {}
        mock_data[charm.unit] = {}
    mock_relation.data = mock_data
    return mock_relation


def create_handler(charm, relations):
    """Create a WatcherRequirerHandler wired to mocked relations."""
    with patch.object(WatcherRequirerHandler, "__init__", return_value=None):
        handler = WatcherRequirerHandler.__new__(WatcherRequirerHandler)
    handler.charm = charm
    mock_framework = MagicMock()
    mock_framework.model = charm.model
    handler.framework = mock_framework
    charm.model.relations.get.return_value = relations
    handler._get_raft_password = MagicMock(return_value="raft-pwd")
    handler._get_port_for_relation = MagicMock(return_value=2223)
    handler._update_unit_address_if_changed = MagicMock()
    return handler


def partner_addrs(count):
    """Return a list of fake PostgreSQL unit IPs."""
    return [f"10.0.0.{i + 1}" for i in range(count)]


def relation_for_units(charm, count, local_app_data=None, provider_extra=None):
    """Create a mock relation whose provider databag lists `count` PG units."""
    app_data = {"raft-partner-addrs": json.dumps(partner_addrs(count))}
    if provider_extra:
        app_data.update(provider_extra)
    return create_mock_relation({}, app_data=app_data, charm=charm, local_app_data=local_app_data)


class TestAZColocation:
    """Tests for AZ co-location detection and enforcement."""

    def test_check_az_colocation_no_az_set(self):
        """No warning when JUJU_AVAILABILITY_ZONE is not set."""
        mock_charm = create_mock_charm()
        relation = create_mock_relation({"postgresql/0": "az1"})

        with patch.object(WatcherRequirerHandler, "__init__", return_value=None):
            handler = WatcherRequirerHandler.__new__(WatcherRequirerHandler)
            handler.charm = mock_charm

            with patch.dict("os.environ", {}, clear=True):
                result = handler._check_az_colocation(relation)
                assert result is None

    def test_check_az_colocation_different_az(self):
        """No warning when watcher is in a different AZ."""
        mock_charm = create_mock_charm()
        relation = create_mock_relation({"postgresql/0": "az1", "postgresql/1": "az2"})

        with patch.object(WatcherRequirerHandler, "__init__", return_value=None):
            handler = WatcherRequirerHandler.__new__(WatcherRequirerHandler)
            handler.charm = mock_charm

            with patch.dict("os.environ", {"JUJU_AVAILABILITY_ZONE": "az3"}, clear=False):
                result = handler._check_az_colocation(relation)
                assert result is None

    def test_check_az_colocation_same_az(self):
        """Warning returned when watcher shares AZ with a PostgreSQL unit."""
        mock_charm = create_mock_charm()
        relation = create_mock_relation({"postgresql/0": "az1", "postgresql/1": "az2"})

        with patch.object(WatcherRequirerHandler, "__init__", return_value=None):
            handler = WatcherRequirerHandler.__new__(WatcherRequirerHandler)
            handler.charm = mock_charm

            with patch.dict("os.environ", {"JUJU_AVAILABILITY_ZONE": "az1"}, clear=False):
                result = handler._check_az_colocation(relation)
                assert result is not None
                assert "az1" in result
                assert "postgresql/0" in result

    def test_check_az_colocation_multiple_colocated(self):
        """Warning lists all co-located units."""
        mock_charm = create_mock_charm()
        relation = create_mock_relation({"postgresql/0": "az1", "postgresql/1": "az1"})

        with patch.object(WatcherRequirerHandler, "__init__", return_value=None):
            handler = WatcherRequirerHandler.__new__(WatcherRequirerHandler)
            handler.charm = mock_charm

            with patch.dict("os.environ", {"JUJU_AVAILABILITY_ZONE": "az1"}, clear=False):
                result = handler._check_az_colocation(relation)
                assert result is not None
                assert "postgresql/0" in result
                assert "postgresql/1" in result

    def test_check_az_colocation_pg_unit_no_az(self):
        """No warning when PostgreSQL unit has no AZ set."""
        mock_charm = create_mock_charm()
        relation = create_mock_relation({"postgresql/0": None})

        with patch.object(WatcherRequirerHandler, "__init__", return_value=None):
            handler = WatcherRequirerHandler.__new__(WatcherRequirerHandler)
            handler.charm = mock_charm

            with patch.dict("os.environ", {"JUJU_AVAILABILITY_ZONE": "az1"}, clear=False):
                result = handler._check_az_colocation(relation)
                assert result is None


class TestAZProfileEnforcement:
    """Tests for profile-based AZ enforcement (testing=warning, production=blocked)."""

    def _setup_handler_with_relations(self, profile, watcher_az, pg_units_az):
        """Create a handler with mocked relations for update_status testing.

        Args:
            profile: "testing" or "production"
            watcher_az: The watcher's AZ or None
            pg_units_az: Dict of unit_name -> az for PostgreSQL units
        """
        mock_charm = create_mock_charm(profile=profile)
        mock_relation = create_mock_relation(pg_units_az)

        with patch.object(WatcherRequirerHandler, "__init__", return_value=None):
            handler = WatcherRequirerHandler.__new__(WatcherRequirerHandler)
            handler.charm = mock_charm

            # Mock framework.model to make self.model work
            mock_framework = MagicMock()
            mock_framework.model = mock_charm.model
            handler.framework = mock_framework

            # Mock model.relations
            mock_charm.model.relations.get.return_value = [mock_relation]

            # Mock _get_pg_endpoints
            handler._get_pg_endpoints = MagicMock(return_value=list(pg_units_az.keys()))
            handler._update_unit_address_if_changed = MagicMock()

            return handler, mock_charm, watcher_az

    def test_testing_profile_same_az_sets_active_with_warning(self):
        """With profile=testing and same AZ, status is Active with WARNING."""
        handler, mock_charm, _ = self._setup_handler_with_relations(
            profile="testing",
            watcher_az="az1",
            pg_units_az={"postgresql/0": "az1", "postgresql/1": "az2"},
        )

        with (
            patch.dict("os.environ", {"JUJU_AVAILABILITY_ZONE": "az1"}, clear=False),
            patch(
                "relations.watcher_requirer.RaftController.get_status",
                return_value={"connected": True},
            ),
        ):
            handler._on_update_status(MagicMock())

        status = mock_charm.unit.status
        assert isinstance(status, ActiveStatus), (
            f"Expected ActiveStatus, got {type(status)}: {status}"
        )
        assert "WARNING" in status.message

    def test_production_profile_same_az_sets_blocked(self):
        """With profile=production and same AZ, status is Blocked."""
        handler, mock_charm, _ = self._setup_handler_with_relations(
            profile="production",
            watcher_az="az1",
            pg_units_az={"postgresql/0": "az1", "postgresql/1": "az2"},
        )

        with (
            patch.dict("os.environ", {"JUJU_AVAILABILITY_ZONE": "az1"}, clear=False),
            patch(
                "relations.watcher_requirer.RaftController.get_status",
                return_value={"connected": True},
            ),
        ):
            handler._on_update_status(MagicMock())

        status = mock_charm.unit.status
        assert isinstance(status, BlockedStatus), (
            f"Expected BlockedStatus, got {type(status)}: {status}"
        )
        assert "AZ co-location" in status.message

    def test_production_profile_different_az_sets_active(self):
        """With profile=production and different AZ, status is Active (no block)."""
        handler, mock_charm, _ = self._setup_handler_with_relations(
            profile="production",
            watcher_az="az3",
            pg_units_az={"postgresql/0": "az1", "postgresql/1": "az2"},
        )

        with (
            patch.dict("os.environ", {"JUJU_AVAILABILITY_ZONE": "az3"}, clear=False),
            patch(
                "relations.watcher_requirer.RaftController.get_status",
                return_value={"connected": True},
            ),
        ):
            handler._on_update_status(MagicMock())

        status = mock_charm.unit.status
        assert isinstance(status, ActiveStatus), (
            f"Expected ActiveStatus, got {type(status)}: {status}"
        )
        assert "WARNING" not in status.message

    def test_no_az_no_block(self):
        """When JUJU_AVAILABILITY_ZONE is not set, no blocking regardless of profile."""
        handler, mock_charm, _ = self._setup_handler_with_relations(
            profile="production",
            watcher_az=None,
            pg_units_az={"postgresql/0": "az1", "postgresql/1": "az2"},
        )

        env = {k: v for k, v in __import__("os").environ.items() if k != "JUJU_AVAILABILITY_ZONE"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch(
                "relations.watcher_requirer.RaftController.get_status",
                return_value={"connected": True},
            ),
        ):
            handler._on_update_status(MagicMock())

        status = mock_charm.unit.status
        assert isinstance(status, ActiveStatus), (
            f"Expected ActiveStatus, got {type(status)}: {status}"
        )

    def test_no_raft_connection_sets_waiting(self):
        """When Raft is not connected, status is Waiting regardless of AZ."""
        mock_charm = create_mock_charm(profile="production")
        mock_relation = create_mock_relation({"postgresql/0": "az1"})

        with (
            patch.object(WatcherRequirerHandler, "__init__", return_value=None),
            patch("raft_controller.service_running") as _service_running,
        ):
            handler = WatcherRequirerHandler.__new__(WatcherRequirerHandler)
            handler.charm = mock_charm
            handler._raft_controllers = {}
            mock_framework = MagicMock()
            mock_framework.model = mock_charm.model
            handler.framework = mock_framework
            mock_charm.model.relations.get.return_value = [mock_relation]

            mock_raft = MagicMock()
            mock_raft.get_status.return_value = {"connected": False}
            handler._raft_controllers[mock_relation.id] = mock_raft
            handler._get_pg_endpoints = MagicMock(return_value=[])
            handler._update_unit_address_if_changed = MagicMock()

            with patch.dict("os.environ", {"JUJU_AVAILABILITY_ZONE": "az1"}, clear=False):
                handler._on_update_status(MagicMock())

            status = mock_charm.unit.status
            assert isinstance(status, WaitingStatus)


class TestWatcherRelationLifecycle:
    """Tests for watcher relation lifecycle cleanup."""

    def test_relation_broken_removes_port(self):
        """Relation-broken removes the Raft service and releases the allocated port."""
        mock_charm = create_mock_charm()
        mock_relation = MagicMock()
        mock_relation.id = 42
        mock_event = MagicMock()
        mock_event.relation = mock_relation

        with (
            patch.object(WatcherRequirerHandler, "__init__", return_value=None),
            patch("relations.watcher_requirer.RaftController.remove_service") as _remove_service,
            patch.object(WatcherRequirerHandler, "_get_raft_partner_addrs", return_value=[]),
        ):
            handler = WatcherRequirerHandler.__new__(WatcherRequirerHandler)
            handler.charm = mock_charm
            handler._release_port_for_relation = MagicMock()

            mock_framework = MagicMock()
            mock_framework.model = mock_charm.model
            handler.framework = mock_framework

            mock_charm.model.relations.get.return_value = []

            handler._on_watcher_relation_broken(mock_event)

            _remove_service.assert_called_once_with()
            handler._release_port_for_relation.assert_called_once_with(42)


class TestShouldWatcherVote:
    """The watcher votes at even unit counts (and a single unit), stands down at odd >= 3."""

    @pytest.mark.parametrize(
        ("count", "expected"),
        [
            (0, True),
            (1, True),
            (2, True),
            (3, False),
            (4, True),
            (5, False),
            (6, True),
            (7, False),
        ],
    )
    def test_should_watcher_vote(self, count, expected):
        with patch.object(WatcherRequirerHandler, "__init__", return_value=None):
            handler = WatcherRequirerHandler.__new__(WatcherRequirerHandler)
        assert handler._should_watcher_vote(partner_addrs(count)) is expected


class TestVoteTransitions:
    """Stand-down/rejoin transitions on relation-changed events."""

    def test_relation_changed_stands_down_at_three_units(self):
        """At 3 PG units the watcher stops voting, publishes 'disabled' and warns."""
        charm = create_mock_charm()
        relation = relation_for_units(charm, 3, local_app_data={"raft-status": "connected"})
        handler = create_handler(charm, [relation])

        with (
            patch(f"{MODULE}.RaftController.cleanup_raft_cluster"),
            patch(f"{MODULE}.RaftController.configure") as configure,
            patch(f"{MODULE}.RaftController.remove_service") as remove_service,
            patch(f"{MODULE}.RaftController.remove_raft_member") as remove_member,
            patch.dict("os.environ", {}, clear=True),
        ):
            handler._on_watcher_relation_changed(MagicMock())

        remove_service.assert_called_once()
        remove_member.assert_called_once()
        configure.assert_not_called()
        assert relation.data[charm.app]["raft-status"] == "disabled"
        status = charm.unit.status
        assert isinstance(status, ActiveStatus), f"Expected ActiveStatus, got {status}"
        assert "odd number units" in status.message

    def test_relation_changed_reenables_at_four_units(self):
        """At 4 PG units a previously stood-down watcher rejoins and votes."""
        charm = create_mock_charm()
        relation = relation_for_units(charm, 4, local_app_data={"raft-status": "disabled"})
        handler = create_handler(charm, [relation])

        with (
            patch(f"{MODULE}.RaftController.cleanup_raft_cluster"),
            patch(f"{MODULE}.RaftController.configure") as configure,
            patch(f"{MODULE}.RaftController.remove_service") as remove_service,
            patch(f"{MODULE}.RaftController.remove_raft_member"),
            patch(f"{MODULE}.service_running", return_value=True),
            patch.dict("os.environ", {}, clear=True),
        ):
            handler._on_watcher_relation_changed(MagicMock())

        configure.assert_called_once()
        remove_service.assert_not_called()
        assert relation.data[charm.app]["raft-status"] == "connected"
        assert isinstance(charm.unit.status, ActiveStatus)

    def test_relation_changed_continues_past_relation_without_details(self):
        """A relation without raft details must not starve other relations."""
        charm = create_mock_charm()
        rel_no_details = create_mock_relation({}, app_data={}, charm=charm)
        rel_no_details.id = 1
        rel_healthy = relation_for_units(charm, 2)
        rel_healthy.id = 2
        handler = create_handler(charm, [rel_no_details, rel_healthy])

        with (
            patch(f"{MODULE}.RaftController.cleanup_raft_cluster"),
            patch(f"{MODULE}.RaftController.configure") as configure,
            patch(f"{MODULE}.RaftController.remove_service"),
            patch(f"{MODULE}.RaftController.remove_raft_member"),
            patch(f"{MODULE}.service_running", return_value=True),
            patch.dict("os.environ", {}, clear=True),
        ):
            handler._on_watcher_relation_changed(MagicMock())

        configure.assert_called_once()
        assert rel_healthy.data[charm.app]["raft-status"] == "connected"

    def test_relation_changed_continues_past_disabled_relation(self):
        """Standing down for one cluster must not starve other relations."""
        charm = create_mock_charm()
        rel_odd = relation_for_units(charm, 3)
        rel_odd.id = 1
        rel_even = relation_for_units(charm, 2)
        rel_even.id = 2
        handler = create_handler(charm, [rel_odd, rel_even])

        with (
            patch(f"{MODULE}.RaftController.cleanup_raft_cluster"),
            patch(f"{MODULE}.RaftController.configure") as configure,
            patch(f"{MODULE}.RaftController.remove_service"),
            patch(f"{MODULE}.RaftController.remove_raft_member"),
            patch(f"{MODULE}.service_running", return_value=True),
            patch.dict("os.environ", {}, clear=True),
        ):
            handler._on_watcher_relation_changed(MagicMock())

        configure.assert_called_once()
        assert rel_odd.data[charm.app]["raft-status"] == "disabled"
        assert rel_even.data[charm.app]["raft-status"] == "connected"


class TestUpdateStatusStandDown:
    """Stand-down, self-heal and status reporting on update-status."""

    def test_update_status_stands_down_at_three_units(self):
        """Update-status tears down (service first), publishes 'disabled' and warns."""
        charm = create_mock_charm()
        relation = relation_for_units(charm, 3, local_app_data={"raft-status": "connected"})
        handler = create_handler(charm, [relation])
        manager = MagicMock()

        with (
            patch(
                f"{MODULE}.RaftController.get_status",
                return_value={"connected": True, "running": True},
            ),
            patch(f"{MODULE}.RaftController.remove_service", manager.remove_service),
            patch(f"{MODULE}.RaftController.remove_raft_member", manager.remove_raft_member),
            patch(f"{MODULE}.RaftController.configure") as configure,
            patch(f"{MODULE}.service_running", return_value=False),
            patch.dict("os.environ", {}, clear=True),
        ):
            handler._on_update_status(MagicMock())

        manager.remove_service.assert_called_once()
        manager.remove_raft_member.assert_called_once()
        call_names = [name for name, _, _ in manager.mock_calls]
        assert call_names.index("remove_service") < call_names.index("remove_raft_member"), (
            "service must be stopped before removing the raft member"
        )
        configure.assert_not_called()
        assert relation.data[charm.app]["raft-status"] == "disabled"
        status = charm.unit.status
        assert isinstance(status, ActiveStatus), f"Expected ActiveStatus, got {status}"
        assert status.message.startswith("Watcher standing down")
        assert "odd number units" in status.message
        assert "Raft connected" not in status.message

    def test_update_status_stand_down_is_idempotent(self):
        """While already stood down, update-status must not re-run the teardown."""
        charm = create_mock_charm()
        relation = relation_for_units(charm, 3, local_app_data={"raft-status": "disabled"})
        handler = create_handler(charm, [relation])

        with (
            patch(
                f"{MODULE}.RaftController.get_status",
                return_value={"connected": False, "running": False},
            ),
            patch(f"{MODULE}.RaftController.remove_service") as remove_service,
            patch(f"{MODULE}.RaftController.remove_raft_member") as remove_member,
            patch(f"{MODULE}.RaftController.configure") as configure,
            patch(f"{MODULE}.service_running", return_value=False),
            patch.dict("os.environ", {}, clear=True),
        ):
            handler._on_update_status(MagicMock())

        remove_service.assert_not_called()
        remove_member.assert_not_called()
        configure.assert_not_called()
        status = charm.unit.status
        assert isinstance(status, ActiveStatus)
        assert "odd number units" in status.message

    def test_update_status_self_heals_at_four_units(self):
        """If the rejoin event was missed, update-status reconfigures and reconnects."""
        charm = create_mock_charm()
        relation = relation_for_units(charm, 4, local_app_data={"raft-status": "disabled"})
        handler = create_handler(charm, [relation])

        with (
            patch(
                f"{MODULE}.RaftController.get_status",
                return_value={"connected": False, "running": False},
            ),
            patch(f"{MODULE}.RaftController.remove_service") as remove_service,
            patch(f"{MODULE}.RaftController.remove_raft_member"),
            patch(f"{MODULE}.RaftController.configure") as configure,
            patch(f"{MODULE}.service_running", return_value=True),
            patch.dict("os.environ", {}, clear=True),
        ):
            handler._on_update_status(MagicMock())

        configure.assert_called_once()
        remove_service.assert_not_called()
        assert relation.data[charm.app]["raft-status"] == "connected"
        status = charm.unit.status
        assert isinstance(status, ActiveStatus)
        assert "monitoring 4 PostgreSQL endpoints" in status.message

    def test_update_status_provider_disable_not_resurrected(self):
        """A provider-disabled watcher is torn down and never self-healed."""
        charm = create_mock_charm()
        relation = relation_for_units(
            charm,
            2,
            local_app_data={"raft-status": "connected"},
            provider_extra={"disable-watcher": "true"},
        )
        handler = create_handler(charm, [relation])

        with (
            patch(
                f"{MODULE}.RaftController.get_status",
                return_value={"connected": False, "running": False},
            ),
            patch(f"{MODULE}.RaftController.remove_service") as remove_service,
            patch(f"{MODULE}.RaftController.remove_raft_member") as remove_member,
            patch(f"{MODULE}.RaftController.configure") as configure,
            patch(f"{MODULE}.service_running", return_value=True),
            patch.dict("os.environ", {}, clear=True),
        ):
            handler._on_update_status(MagicMock())

        configure.assert_not_called()
        remove_service.assert_called_once()
        remove_member.assert_called_once()
        assert relation.data[charm.app]["raft-status"] == "disabled"
        status = charm.unit.status
        assert isinstance(status, ActiveStatus)
        assert "odd number units" not in status.message
