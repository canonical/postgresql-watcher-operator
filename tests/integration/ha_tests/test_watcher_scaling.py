#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for watcher behavior across even/odd PostgreSQL unit counts.

Per the stereo mode documentation, the watcher must vote whenever the
PostgreSQL unit count is even (2, 4, 6, ...) and stand down (stop its Raft
service and leave the quorum) at odd counts of three or more (3, 5, 7, ...),
rejoining automatically when the count becomes even again.

Test scenario:
1. Deploy 2 PostgreSQL units + watcher (3 Raft voters)
2. Scale to 3 units: watcher stands down (3 Raft voters, all PostgreSQL)
3. Scale to 4 units: watcher rejoins (5 Raft voters)
4. Scale back to 2 units: watcher keeps voting (3 Raft voters)
Continuous writes must keep flowing through every transition.
"""

import logging

import pytest
from pytest_operator.plugin import OpsTest
from tenacity import Retrying, stop_after_delay, wait_fixed

from ..helpers import APPLICATION_NAME, DATABASE_APP_NAME
from .helpers import (
    are_writes_increasing,
    check_writes,
    get_primary,
    start_writes,
    verify_raft_cluster_health,
)

WATCHER_APP_NAME = "postgresql-watcher"

logger = logging.getLogger(__name__)


async def assert_watcher_stood_down(ops_test: OpsTest) -> None:
    """Assert the watcher stood down: warning status and no running Raft service."""
    watcher_unit = ops_test.model.applications[WATCHER_APP_NAME].units[0]
    assert watcher_unit.workload_status == "active", (
        f"Watcher must stay active while standing down, got {watcher_unit.workload_status}"
    )
    assert "odd number units" in watcher_unit.workload_status_message, (
        f"Expected stand-down warning in status, got: {watcher_unit.workload_status_message}"
    )

    return_code, stdout, _ = await ops_test.juju(
        "exec",
        "--unit",
        watcher_unit.name,
        "--",
        "systemctl",
        "list-units",
        "--no-legend",
        "--state=active",
        "watcher-raft@*",
    )
    assert return_code == 0, f"Failed to list watcher services on {watcher_unit.name}"
    assert not stdout.strip(), f"Watcher Raft service still active: {stdout}"


async def assert_watcher_voting(ops_test: OpsTest) -> None:
    """Assert the watcher is voting: active status without the stand-down warning."""
    watcher_unit = ops_test.model.applications[WATCHER_APP_NAME].units[0]
    assert watcher_unit.workload_status == "active", (
        f"Watcher must be active, got {watcher_unit.workload_status}"
    )
    assert "odd number units" not in watcher_unit.workload_status_message, (
        f"Stand-down warning must be cleared, got: {watcher_unit.workload_status_message}"
    )


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest, charm) -> None:
    """Deploy 2 PostgreSQL units, the watcher and the test application."""
    async with ops_test.fast_forward():
        logger.info("Deploying PostgreSQL charm with 2 units...")
        await ops_test.model.deploy(
            DATABASE_APP_NAME,
            application_name=DATABASE_APP_NAME,
            num_units=2,
            series="noble",
            channel="16/edge",
            config={"profile": "testing", "synchronous-mode-strict": False},
        )
        logger.info("Deploying watcher...")
        await ops_test.model.deploy(
            charm,
            application_name=WATCHER_APP_NAME,
            num_units=1,
            series="noble",
            config={"profile": "testing"},
        )
        logger.info("Deploying test application...")
        await ops_test.model.deploy(
            APPLICATION_NAME,
            application_name=APPLICATION_NAME,
            series="noble",
            channel="edge",
        )

        logger.info("Relating PostgreSQL to watcher and test application")
        await ops_test.model.integrate(
            f"{DATABASE_APP_NAME}:watcher-offer", f"{WATCHER_APP_NAME}:watcher"
        )
        await ops_test.model.integrate(DATABASE_APP_NAME, f"{APPLICATION_NAME}:database")

        await ops_test.model.wait_for_idle(status="active", timeout=1800)

    assert len(ops_test.model.applications[DATABASE_APP_NAME].units) == 2
    assert len(ops_test.model.applications[WATCHER_APP_NAME].units) == 1


@pytest.mark.abort_on_fail
async def test_even_odd_scaling_transitions(ops_test: OpsTest, continuous_writes) -> None:
    """Scale PostgreSQL 2->3->4->2 and verify the watcher stands down and rejoins."""
    await start_writes(ops_test)

    # Baseline: 2 PostgreSQL units + watcher = 3 Raft voters
    await verify_raft_cluster_health(
        ops_test, DATABASE_APP_NAME, WATCHER_APP_NAME, expected_members=3
    )

    async with ops_test.fast_forward():
        logger.info("Scaling PostgreSQL to 3 units; the watcher must stand down")
        await ops_test.model.applications[DATABASE_APP_NAME].add_unit(count=1)
        await ops_test.model.wait_for_idle(
            apps=[DATABASE_APP_NAME, WATCHER_APP_NAME],
            status="active",
            timeout=1800,
            idle_period=30,
        )
        for attempt in Retrying(stop=stop_after_delay(600), wait=wait_fixed(10), reraise=True):
            with attempt:
                await verify_raft_cluster_health(
                    ops_test,
                    DATABASE_APP_NAME,
                    WATCHER_APP_NAME,
                    expected_members=3,
                    expect_watcher_absent=True,
                )
                await assert_watcher_stood_down(ops_test)
        await are_writes_increasing(ops_test)

        logger.info("Scaling PostgreSQL to 4 units; the watcher must rejoin and vote")
        await ops_test.model.applications[DATABASE_APP_NAME].add_unit(count=1)
        await ops_test.model.wait_for_idle(
            apps=[DATABASE_APP_NAME, WATCHER_APP_NAME],
            status="active",
            timeout=1800,
            idle_period=30,
        )
        for attempt in Retrying(stop=stop_after_delay(600), wait=wait_fixed(10), reraise=True):
            with attempt:
                await verify_raft_cluster_health(
                    ops_test, DATABASE_APP_NAME, WATCHER_APP_NAME, expected_members=5
                )
                await assert_watcher_voting(ops_test)
        await are_writes_increasing(ops_test)

        logger.info("Scaling PostgreSQL back to 2 units; the watcher must keep voting")
        primary = await get_primary(ops_test, DATABASE_APP_NAME)
        units_to_remove = [
            unit.name
            for unit in ops_test.model.applications[DATABASE_APP_NAME].units
            if unit.name != primary
        ][:2]
        await ops_test.model.destroy_units(*units_to_remove)
        await ops_test.model.wait_for_idle(
            apps=[DATABASE_APP_NAME, WATCHER_APP_NAME],
            status="active",
            timeout=1800,
            idle_period=30,
        )
        for attempt in Retrying(stop=stop_after_delay(600), wait=wait_fixed(10), reraise=True):
            with attempt:
                await verify_raft_cluster_health(
                    ops_test, DATABASE_APP_NAME, WATCHER_APP_NAME, expected_members=3
                )
                await assert_watcher_voting(ops_test)

    await check_writes(ops_test)
