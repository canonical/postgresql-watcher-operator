#!/usr/bin/env -S LD_LIBRARY_PATH=lib python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charmed Machine Operator for the PostgreSQL database."""

import dataclasses
import json
import logging
import pathlib
import platform
from typing import Literal

import charm_refresh
import ops.log
import tomli
from charmlibs import snap
from charms.data_platform_libs.v1.data_models import TypedCharmBase
from ops import (
    BlockedStatus,
    JujuVersion,
    Relation,
    SecretRemoveEvent,
    main,
)
from single_kernel_postgresql.config.literals import PEER

from config import CharmConfig
from raft_controller import install_service
from relations.watcher_requirer import WatcherRequirerHandler

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("boto3").setLevel(logging.WARNING)
logging.getLogger("botocore").setLevel(logging.WARNING)

SCOPES = Literal["app", "unit"]


@dataclasses.dataclass(eq=False)
class _PostgreSQLRefresh(charm_refresh.CharmSpecificMachines):
    _charm: "PostgresqlWatcherCharm"

    @staticmethod
    def run_pre_refresh_checks_after_1_unit_refreshed() -> None:
        pass

    def run_pre_refresh_checks_before_any_units_refreshed(self) -> None:
        if self._charm._peers is None:
            # This should not happen since `charm_refresh.PeerRelationNotReady` should've been
            # raised, so this code would not run
            raise ValueError

    @classmethod
    def is_compatible(
        cls,
        *,
        old_charm_version: charm_refresh.CharmVersion,
        new_charm_version: charm_refresh.CharmVersion,
        old_workload_version: str,
        new_workload_version: str,
    ) -> bool:
        # Check charm version compatibility
        if not super().is_compatible(
            old_charm_version=old_charm_version,
            new_charm_version=new_charm_version,
            old_workload_version=old_workload_version,
            new_workload_version=new_workload_version,
        ):
            return False

        # Check workload version compatibility
        old_major, old_minor = (int(component) for component in old_workload_version.split("."))
        new_major, new_minor = (int(component) for component in new_workload_version.split("."))
        if old_major != new_major:
            return False
        return new_minor >= old_minor

    def refresh_snap(
        self, *, snap_name: str, snap_revision: str, refresh: charm_refresh.Machines
    ) -> None:
        pass


class PostgresqlWatcherCharm(TypedCharmBase[CharmConfig]):
    """Charmed Operator for the PostgreSQL database."""

    config_type = CharmConfig

    def __init__(self, *args):
        super().__init__(*args)
        # Show logger name (module name) in logs
        root_logger = logging.getLogger()
        for handler in root_logger.handlers:
            if isinstance(handler, ops.log.JujuLogHandler):
                handler.setFormatter(logging.Formatter("{name}:{message}", style="{"))

        # Watcher mode: lightweight Raft witness, no PostgreSQL
        self._init_watcher_mode()
        # Set tracing_endpoint for @trace_charm decorator compatibility
        self.tracing_endpoint = None

        self.refresh: charm_refresh.Machines | None
        try:
            self.refresh = charm_refresh.Machines(
                _PostgreSQLRefresh(
                    workload_name="PostgreSQL", charm_name="postgresql", _charm=self
                )
            )
        except (charm_refresh.UnitTearingDown, charm_refresh.PeerRelationNotReady):
            self.refresh = None
        self._reconcile_refresh_status()

        if self.refresh is not None and not self.refresh.next_unit_allowed_to_refresh:
            if self.refresh.in_progress:
                self._post_snap_refresh(self.refresh)
            else:
                self.refresh.next_unit_allowed_to_refresh = True

    def _init_watcher_mode(self):
        """Initialize the charm in watcher mode (lightweight Raft witness)."""
        self.watcher_requirer = WatcherRequirerHandler(self)
        # Watcher mode delegates all event handling to WatcherRequirerHandler.
        # We still observe leader_elected to persist the role in peer data.

    def _post_snap_refresh(self, refresh: charm_refresh.Machines):
        """Start PostgreSQL, check if this app and unit are healthy, and allow next unit to refresh.

        Called after snap refresh
        """
        install_service()
        refresh.next_unit_allowed_to_refresh = True

    def set_unit_status(
        self, status: ops.StatusBase, /, *, refresh: charm_refresh.Machines | None = None
    ):
        """Set unit status without overriding higher priority refresh status."""
        if refresh is None:
            refresh = self.refresh
        if refresh is not None and refresh.unit_status_higher_priority:
            return
        if (
            isinstance(status, ops.ActiveStatus)
            and refresh is not None
            and (refresh_status := refresh.unit_status_lower_priority())
        ):
            self.unit.status = refresh_status
            pathlib.Path(".last_refresh_unit_status.json").write_text(
                json.dumps(refresh_status.message)
            )
            return
        self.unit.status = status

    def _reconcile_refresh_status(self, _=None):
        # Workaround for other unit statuses being set in a stateful way (i.e. unable to recompute
        # status on every event)
        path = pathlib.Path(".last_refresh_unit_status.json")
        try:
            last_refresh_unit_status = json.loads(path.read_text())
        except FileNotFoundError:
            last_refresh_unit_status = None
        new_refresh_unit_status = None
        if self.refresh is not None and self.refresh.unit_status_higher_priority:
            self.unit.status = self.refresh.unit_status_higher_priority
            new_refresh_unit_status = self.refresh.unit_status_higher_priority.message
        elif self.unit.status.message == last_refresh_unit_status:
            if self.refresh is not None and (
                refresh_status := self.refresh.unit_status_lower_priority()
            ):
                self.unit.status = refresh_status
                new_refresh_unit_status = refresh_status.message
        elif (
            isinstance(self.unit.status, ops.ActiveStatus)
            and self.refresh is not None
            and (refresh_status := self.refresh.unit_status_lower_priority())
        ):
            self.unit.status = refresh_status
            new_refresh_unit_status = refresh_status.message
        path.write_text(json.dumps(new_refresh_unit_status))

    @property
    def app_peer_data(self) -> dict:
        """Application peer relation data object."""
        return self.all_peer_data.get(self.app, {})

    @property
    def unit_peer_data(self) -> dict:
        """Unit peer relation data object."""
        return self.all_peer_data.get(self.unit, {})

    @property
    def all_peer_data(self) -> dict:
        """Return all peer data if available."""
        if self._peers is None:
            return {}

        # RelationData has dict like API
        return self._peers.data  # type: ignore

    def _on_secret_remove(self, event: SecretRemoveEvent) -> None:
        if self.model.juju_version < JujuVersion("3.6.11"):
            logger.warning(
                "Skipping secret revision removal due to https://github.com/juju/juju/issues/20782"
            )
            return

        # A secret removal (entire removal, not just a revision removal) causes
        # https://github.com/juju/juju/issues/20794. This check is to avoid the
        # errors that would happen if we tried to remove the revision in that case
        # (in the revision removal, the label is present).
        if event.secret.label is None:
            logger.debug("Secret with no label cannot be removed")
            return
        logger.debug(f"Removing secret with label {event.secret.label} revision {event.revision}")
        event.remove_revision()

    @property
    def is_blocked(self) -> bool:
        """Returns whether the unit is in a blocked state."""
        return isinstance(self.unit.status, BlockedStatus)

    def _install_snap_package(
        self, *, revision: str | None, refresh: charm_refresh.Machines | None = None
    ) -> None:
        """Installs PostgreSQL snap.

        Args:
            revision: snap revision to install.
            refresh: refresh class; will refresh installed snap if not `None`
        """
        if revision is None:
            if refresh is not None:
                raise ValueError
            # TODO: consider using `self.refresh.pinned_snap_revision` instead (requires waiting
            # for refresh peer relation to be ready before installing snap)
            with pathlib.Path("refresh_versions.toml").open("rb") as file:
                revisions = tomli.load(file)["snap"]["revisions"]
            try:
                revision = revisions[platform.machine()]
            except KeyError:
                logger.error("Unavailable snap architecture %s", platform.machine())
                raise
        try:
            snap_cache = snap.SnapCache()
            snap_package = snap_cache[charm_refresh.snap_name()]
            if not snap_package.present or refresh is not None:
                snap_package.ensure(snap.SnapState.Present, revision=revision)
                if refresh is not None:
                    refresh.update_snap_revision()
                snap_package.hold()
        except (snap.SnapError, snap.SnapNotFoundError) as e:
            logger.error(
                "An exception occurred when installing %s. Reason: %s",
                charm_refresh.snap_name(),
                str(e),
            )
            raise

    @property
    def _peers(self) -> Relation | None:
        """Fetch the peer relation.

        Returns:
             A:class:`ops.model.Relation` object representing
             the peer relation.
        """
        return self.model.get_relation(PEER)


if __name__ == "__main__":
    main(PostgresqlWatcherCharm)
