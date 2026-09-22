# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""File containing constants to be used in the charm."""

# Snap constants.
SNAP_COMMON_PATH = "/var/snap/charmed-postgresql/common"
SNAP_VITALITY_HINT = "resilience.vitality-hint"
SNAP_VITALITY_MAX_SNAPS = 100
SNAP_OOM_SCORE_ADJUST_MIN = -900

RAFT_PORT = 2222
RAFT_PARTNER_PREFIX = "partner_node_status_server_"
