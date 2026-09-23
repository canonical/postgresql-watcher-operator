# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the watcher requirer relation handler (AZ co-location, port allocation)."""

import json
import socket
from unittest.mock import MagicMock, patch

from ops import ActiveStatus, BlockedStatus, WaitingStatus

from constants import RAFT_PORT
from src.relations.watcher_requirer import WatcherRequirerHandler


def create_mock_charm(profile="testing"):
    """Create a mock charm for watcher requirer testing."""
    mock_charm = MagicMock()
    mock_charm.config = MagicMock()
    mock_charm.config.profile = profile
    mock_charm.unit.name = "pg-watcher/0"
    return mock_charm


def create_mock_relation(units_with_az=None):
    """Create a mock relation with units that have AZ data.

    Args:
        units_with_az: Dict mapping unit names to their AZ values.
            Example: {"postgresql/0": "az1", "postgresql/1": "az2"}
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
    mock_data[mock_relation.app] = {}
    mock_relation.data = mock_data
    return mock_relation


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


class TestPortAllocation:
    """Tests for port assignment when ports are taken by another process."""

    def _handler(self, allocations=None):
        """Create a handler whose peer data is a plain dict."""
        mock_charm = create_mock_charm()
        mock_charm.app_peer_data = {"port-allocations": json.dumps(allocations or {})}
        with patch.object(WatcherRequirerHandler, "__init__", return_value=None):
            handler = WatcherRequirerHandler.__new__(WatcherRequirerHandler)
        handler.charm = mock_charm
        return handler

    def test_port_probe_detects_listener(self):
        """A port with a listening socket on it is reported as taken."""
        handler = self._handler()
        with socket.socket() as other:
            other.bind(("127.0.0.1", 0))
            other.listen(1)
            with patch.object(WatcherRequirerHandler, "unit_ip", "127.0.0.1"):
                assert handler._port_probe(other.getsockname()[1]) is True

    def test_port_probe_free_port(self):
        """A port nobody is listening on is reported as free."""
        handler = self._handler()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
        with patch.object(WatcherRequirerHandler, "unit_ip", "127.0.0.1"):
            assert handler._port_probe(free) is False

    def test_port_probe_treats_errors_as_free(self):
        """An OSError while probing (e.g. socket.gaierror) counts as a free port."""
        handler = self._handler()
        with (
            patch.object(WatcherRequirerHandler, "unit_ip", "10.0.0.1"),
            patch("socket.socket") as _socket,
        ):
            _socket.return_value.__enter__.return_value.connect_ex.side_effect = socket.gaierror
            assert handler._port_probe(RAFT_PORT) is False

    def test_get_valid_port_returns_raft_port_when_free(self):
        """RAFT_PORT is returned when it is neither allocated nor in use."""
        handler = self._handler()
        with patch.object(WatcherRequirerHandler, "_port_probe", return_value=False) as port_probe:
            assert handler._get_valid_port({}) == RAFT_PORT

        port_probe.assert_called_once_with(RAFT_PORT)

    def test_get_valid_port_skips_ports_in_use(self):
        """Ports with something listening on them are skipped."""
        handler = self._handler()
        with patch.object(WatcherRequirerHandler, "_port_probe", side_effect=[True, True, False]):
            assert handler._get_valid_port({}) == RAFT_PORT + 2

    def test_get_valid_port_skips_allocated_ports_without_probing(self):
        """Ports allocated to other relations are skipped without probing them."""
        handler = self._handler()
        with patch.object(WatcherRequirerHandler, "_port_probe", return_value=False) as port_probe:
            assert handler._get_valid_port({"1": RAFT_PORT, "2": RAFT_PORT + 1}) == RAFT_PORT + 2

        port_probe.assert_called_once_with(RAFT_PORT + 2)

    def test_get_valid_port_skips_port_in_use_after_allocated_one(self):
        """A port in use right after an allocated one is also skipped."""
        handler = self._handler()
        with patch.object(
            WatcherRequirerHandler, "_port_probe", side_effect=lambda port: port == RAFT_PORT + 1
        ):
            assert handler._get_valid_port({"1": RAFT_PORT}) == RAFT_PORT + 2

    def test_assigns_valid_port_and_persists_it(self):
        """A new relation gets the port from _get_valid_port and it is persisted."""
        handler = self._handler()
        with patch.object(WatcherRequirerHandler, "_get_valid_port", return_value=RAFT_PORT + 2):
            assert handler._get_port_for_relation(7) == RAFT_PORT + 2

        assert handler.charm.app_peer_data["port-allocations"] == json.dumps({"7": RAFT_PORT + 2})

    def test_keeps_other_relations_allocations(self):
        """Assigning a port for a new relation keeps the existing allocations."""
        handler = self._handler({"1": RAFT_PORT})
        seen_allocations = []

        def get_valid_port(allocations):
            # Copy it: _get_port_for_relation mutates the dict after the call.
            seen_allocations.append(dict(allocations))
            return RAFT_PORT + 1

        with patch.object(WatcherRequirerHandler, "_get_valid_port", side_effect=get_valid_port):
            assert handler._get_port_for_relation(3) == RAFT_PORT + 1

        assert seen_allocations == [{"1": RAFT_PORT}]

        assert handler.charm.app_peer_data["port-allocations"] == json.dumps({
            "1": RAFT_PORT,
            "3": RAFT_PORT + 1,
        })

    def test_reuses_existing_allocation_without_probing(self):
        """An already allocated relation keeps its port, without probing it again."""
        handler = self._handler({"7": RAFT_PORT + 5})
        with patch.object(WatcherRequirerHandler, "_get_valid_port") as get_valid_port:
            assert handler._get_port_for_relation(7) == RAFT_PORT + 5

        get_valid_port.assert_not_called()
