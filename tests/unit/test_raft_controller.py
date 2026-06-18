# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path
from unittest.mock import MagicMock, patch

from jinja2 import Template
from pytest import fixture
from single_kernel_postgresql.config.enums import Substrates
from tenacity import stop_after_delay, wait_fixed

from constants import RAFT_PARTNER_PREFIX
from raft_controller import SERVICE_FILE, RaftController, install_service


@fixture
def controller(tmp_path: Path) -> RaftController:
    controller = RaftController(MagicMock(), instance_id="rel42")
    controller.data_dir = str(tmp_path / "watcher-raft" / "rel42")
    controller.config_file = str(tmp_path / "watcher-raft" / "rel42" / "patroni-raft.yaml")
    controller.service_name = "watcher-raft-rel42"
    controller.service_file = str(tmp_path / "watcher-raft-rel42.service")
    return controller


def test_configure(tmp_path: Path, controller: RaftController):
    with open("templates/watcher.yml.j2") as file:
        contents = file.read()
        template = Template(contents)

    expected_content = template.render(
        self_addr="10.0.0.1",
        self_port=2222,
        partner_addrs=["10.0.0.2"],
        password="secret",
        data_dir=f"{tmp_path}/watcher-raft/rel42",
    )
    with (
        patch("raft_controller.render_file") as _render_file,
        patch("raft_controller.create_directory") as _create_directory,
        patch("raft_controller.RaftController.restart") as _restart,
        patch(
            "raft_controller.RaftController.check_watcher_connection"
        ) as _check_watcher_connection,
    ):
        assert controller.configure(2222, "10.0.0.1", ["10.0.0.2"], "secret")

        assert _create_directory.call_count == 2
        _create_directory.assert_any_call(Substrates.VM, f"{tmp_path}/watcher-raft/rel42", 0o700)
        _create_directory.assert_any_call(
            Substrates.VM, f"{tmp_path}/watcher-raft/rel42/raft", 0o700
        )
        _render_file.assert_called_once_with(
            Substrates.VM,
            f"{tmp_path}/watcher-raft/rel42/patroni-raft.yaml",
            expected_content,
            0o600,
        )
        _restart.assert_called_once_with()
        _check_watcher_connection.assert_called_once_with("10.0.0.1:2222", "secret", ["10.0.0.2"])


def test_remove_service_disables_unit_and_deletes_dir(tmp_path: Path, controller: RaftController):
    Path(controller.service_file).write_text("[Unit]\nDescription=test\n")

    with (
        patch("raft_controller.service_running") as _service_running,
        patch("raft_controller.service_stop") as _service_stop,
        patch("raft_controller.service_disable") as _service_disable,
        patch("raft_controller.rmtree") as _rmtree,
    ):
        assert controller.remove_service()
        _service_running.assert_called_once_with(controller.service_name)
        _service_stop.assert_called_once_with(controller.service_name)
        _service_disable.assert_called_once_with(controller.service_name)
        _rmtree.assert_called_once_with(controller.data_dir)


def test_install_service_uses_patroni_profile_execstart(
    tmp_path: Path, controller: RaftController
):
    with open("templates/watcher.service.j2") as file:
        contents = file.read()
        template = Template(contents)

    expected_content = template.render(
        config_file="/var/snap/charmed-postgresql/common/watcher-raft"
    )

    with (
        patch("raft_controller.daemon_reload") as _daemon_reload,
        patch("raft_controller.render_file") as _render_file,
        patch("raft_controller.create_directory"),
    ):
        install_service()

    _render_file.assert_called_once_with(
        Substrates.VM, SERVICE_FILE, expected_content, 0o644, change_owner=False
    )
    _daemon_reload.assert_called_once_with()


def test_check_watcher_connection(controller: RaftController):
    with (
        patch("raft_controller.RaftController.restart") as _restart,
        patch("raft_controller.TcpUtility") as _tcputility,
        patch("raft_controller.wait_fixed", return_value=wait_fixed(0)),
        patch("raft_controller.stop_after_attempt", return_value=stop_after_delay(0)),
    ):
        # No partners
        controller.check_watcher_connection("1.1.1.1:2223", "testpass", [])

        assert not _tcputility.called

        # Can't get watcher status
        _tcputility.return_value.executeCommand.side_effect = [{}]

        controller.check_watcher_connection("1.1.1.1:2223", "testpass", ["2.2.2.2", "3.3.3.3"])

        _tcputility.assert_called_once_with(password="testpass", timeout=3)
        _tcputility.return_value.executeCommand.assert_called_once_with("1.1.1.1:2223", ["status"])
        assert not _restart.called
        _tcputility.reset_mock()
        _tcputility.return_value.executeCommand.reset_mock()

        # One partner is online
        raft_status = {
            f"{RAFT_PARTNER_PREFIX}2.2.2.2:2222": 0,
            f"{RAFT_PARTNER_PREFIX}3.3.3.3:2222": 2,
        }
        _tcputility.return_value.executeCommand.side_effect = [raft_status]

        controller.check_watcher_connection("1.1.1.1:2223", "testpass", ["2.2.2.2", "3.3.3.3"])

        _tcputility.assert_called_once_with(password="testpass", timeout=3)
        _tcputility.return_value.executeCommand.assert_called_once_with("1.1.1.1:2223", ["status"])
        assert not _restart.called
        _tcputility.reset_mock()
        _tcputility.return_value.executeCommand.reset_mock()

        # Partners not connectable
        raft_status = {
            f"{RAFT_PARTNER_PREFIX}2.2.2.2:2222": 0,
            f"{RAFT_PARTNER_PREFIX}3.3.3.3:2222": 0,
        }
        _tcputility.return_value.executeCommand.side_effect = [raft_status, Exception, Exception]

        controller.check_watcher_connection("1.1.1.1:2223", "testpass", ["2.2.2.2", "3.3.3.3"])

        _tcputility.assert_called_once_with(password="testpass", timeout=3)
        assert _tcputility.return_value.executeCommand.call_count == 3
        _tcputility.return_value.executeCommand.assert_any_call("1.1.1.1:2223", ["status"])
        _tcputility.return_value.executeCommand.assert_any_call("2.2.2.2:2222", ["status"])
        _tcputility.return_value.executeCommand.assert_any_call("3.3.3.3:2222", ["status"])
        assert not _restart.called
        _tcputility.reset_mock()
        _tcputility.return_value.executeCommand.reset_mock()

        # Stuck raft
        raft_status = {
            f"{RAFT_PARTNER_PREFIX}2.2.2.2:2222": 0,
            f"{RAFT_PARTNER_PREFIX}3.3.3.3:2222": 0,
        }
        _tcputility.return_value.executeCommand.side_effect = [raft_status, Exception, {1: 2}]

        controller.check_watcher_connection("1.1.1.1:2223", "testpass", ["2.2.2.2", "3.3.3.3"])

        _tcputility.assert_called_once_with(password="testpass", timeout=3)
        assert _tcputility.return_value.executeCommand.call_count == 3
        _tcputility.return_value.executeCommand.assert_any_call("1.1.1.1:2223", ["status"])
        _tcputility.return_value.executeCommand.assert_any_call("2.2.2.2:2222", ["status"])
        _tcputility.return_value.executeCommand.assert_any_call("3.3.3.3:2222", ["status"])
        _restart.assert_called_once_with()
        _tcputility.reset_mock()
        _tcputility.return_value.executeCommand.reset_mock()
