#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
from collections.abc import Callable

import jubilant
from jubilant import Juju
from jubilant.statustypes import Status, UnitStatus
from tenacity import Retrying, stop_after_delay, wait_fixed

from constants import PEER

from ..helpers import execute_queries_on_unit

MINUTE_SECS = 60
SERVER_CONFIG_USERNAME = "operator"

JujuModelStatusFn = Callable[[Status], bool]
JujuAppsStatusFn = Callable[[Status, str], bool]


def check_db_units_writes_increment(
    juju: Juju,
    app_name: str,
    app_units: list[str] | None = None,
    db_name: str = "postgresql_test_app_database",
) -> None:
    """Ensure that continuous writes is incrementing on all units.

    Also, ensure that all continuous writes up to the max written value is available
    on all units (ensure that no committed data is lost).
    """
    if not app_units:
        app_units = get_app_units(juju, app_name)

    app_primary = get_db_primary_unit(juju, app_name)
    app_max_value = get_db_max_written_value(juju, app_name, app_primary, db_name)

    for unit_name in app_units:
        for attempt in Retrying(
            reraise=True,
            stop=stop_after_delay(5 * MINUTE_SECS),
            wait=wait_fixed(10),
        ):
            with attempt:
                unit_max_value = get_db_max_written_value(juju, app_name, unit_name, db_name)
                assert unit_max_value > app_max_value, "Writes not incrementing"
                app_max_value = unit_max_value


def get_app_leader(juju: Juju, app_name: str) -> str:
    """Get the leader unit for the given application."""
    model_status = juju.status()
    app_status = model_status.apps[app_name]
    for name, status in app_status.units.items():
        if status.leader:
            return name

    raise Exception("No leader unit found")


def get_app_units(juju: Juju, app_name: str) -> dict[str, UnitStatus]:
    """Get the units for the given application."""
    model_status = juju.status()
    app_status = model_status.apps[app_name]
    return app_status.units


def get_unit_ip(juju: Juju, app_name: str, unit_name: str) -> str:
    """Get the application unit IP."""
    model_status = juju.status()
    app_status = model_status.apps[app_name]
    for name, status in app_status.units.items():
        if name == unit_name:
            return status.public_address

    raise Exception("No application unit found")


def get_db_primary_unit(juju: Juju, app_name: str) -> str:
    """Get the current primary node of the cluster."""
    postgresql_primary = get_app_leader(juju, app_name)
    task = juju.run(unit=postgresql_primary, action="get-primary", wait=5 * MINUTE_SECS)
    task.raise_on_failure()

    primary = task.results.get("primary")
    if primary != "None":
        return primary

    raise Exception("No primary node found")


def get_db_max_written_value(
    juju: Juju, app_name: str, unit_name: str, db_name: str = "postgresql_test_app_database"
) -> int:
    """Retrieve the max written value in the PostgreSQL database.

    Args:
        juju: The Juju model.
        app_name: The application name.
        unit_name: The unit name.
        db_name: The database to connect to.
    """
    password = get_user_password(juju, app_name, SERVER_CONFIG_USERNAME)

    output = execute_queries_on_unit(
        get_unit_ip(juju, app_name, unit_name),
        SERVER_CONFIG_USERNAME,
        password,
        ["SELECT MAX(number) FROM continuous_writes;"],
        db_name,
    )
    return output[0]


def wait_for_apps_status(jubilant_status_func: JujuAppsStatusFn, *apps: str) -> JujuModelStatusFn:
    """Waits for Juju agents to be idle, and for applications to reach a certain status.

    Args:
        jubilant_status_func: The Juju apps status function to wait for.
        apps: The applications to wait for.

    Returns:
        Juju model status function.
    """
    return lambda status: all((
        jubilant.all_agents_idle(status, *apps),
        jubilant_status_func(status, *apps),
    ))


# PG helpers


def get_user_password(juju: Juju, app_name: str, user: str) -> str | None:
    """Get a system user's password."""
    for secret in juju.secrets():
        if secret.label == f"{PEER}.{app_name}.app":
            revealed_secret = juju.show_secret(secret.uri, reveal=True)
            return revealed_secret.content.get(f"{user}-password")
