# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import subprocess
from unittest.mock import Mock, call

import pytest
from charmlibs import snap

import oom
from constants import SNAP_VITALITY_HINT

SNAP_NAME = "charmed-postgresql"


def config(hint):
    return json.dumps({"resilience": {"vitality-hint": hint}})


@pytest.fixture
def commands(monkeypatch):
    read = Mock()
    write = Mock()
    monkeypatch.setattr(oom.subprocess, "check_output", read)
    monkeypatch.setattr(oom.subprocess, "check_call", write)
    return read, write


@pytest.mark.parametrize("initial", ["{}", '{"resilience": {}}', config("")])
def test_missing_or_empty_hint(initial, commands):
    read, write = commands
    read.side_effect = [initial, config(SNAP_NAME)]
    assert oom.ensure_snap_oom_protection(SNAP_NAME) == -899
    assert read.call_args_list == [call(["/usr/bin/snap", "get", "system", "-d"], text=True)] * 2
    write.assert_called_once_with([
        "/usr/bin/snap",
        "set",
        "system",
        f"{SNAP_VITALITY_HINT}={SNAP_NAME}",
    ])


@pytest.mark.parametrize(
    "hint,rank",
    [
        ("postgresql", 2),
        ("postgresql,other", 3),
        ("postgresql,postgresql", 3),
        ("charmed-postgresql-other,charmed-postgresqlx", 3),
        ("postgresql,,other", 4),
        ("postgresql, other", 3),
        ("postgresql,", 3),
    ],
)
def test_append_preserves_exact_hint(hint, rank, commands):
    read, write = commands
    updated = f"{hint},{SNAP_NAME}"
    read.side_effect = [config(hint), config(updated), config(updated)]
    assert oom.ensure_snap_oom_protection(SNAP_NAME) == -900 + rank
    assert oom.ensure_snap_oom_protection(SNAP_NAME) == -900 + rank
    write.assert_called_once_with([
        "/usr/bin/snap",
        "set",
        "system",
        f"{SNAP_VITALITY_HINT}={updated}",
    ])


@pytest.mark.parametrize(
    "hint,rank",
    [
        (SNAP_NAME, 1),
        (f"{SNAP_NAME},postgresql", 1),
        (f"postgresql,{SNAP_NAME}", 2),
        (f"{SNAP_NAME},postgresql,{SNAP_NAME}", 3),
        (",".join(["other"] * 99 + [SNAP_NAME]), 100),
    ],
)
def test_existing_snap_uses_last_rank_without_write(hint, rank, commands):
    read, write = commands
    read.return_value = config(hint)
    assert oom.ensure_snap_oom_protection(SNAP_NAME) == -900 + rank
    assert oom.ensure_snap_oom_protection(SNAP_NAME) == -900 + rank
    assert read.call_count == 2
    write.assert_not_called()


def test_ninety_nine_entries_can_append(commands):
    read, write = commands
    hint = ",".join(["other"] * 99)
    read.side_effect = [config(hint), config(f"{hint},{SNAP_NAME}")]
    assert oom.ensure_snap_oom_protection(SNAP_NAME) == -800
    write.assert_called_once()


@pytest.mark.parametrize("count", [100, 101])
def test_full_hint_fails_without_write(count, commands):
    read, write = commands
    read.return_value = config(",".join(["other"] * count))
    with pytest.raises(snap.SnapError):
        oom.ensure_snap_oom_protection(SNAP_NAME)
    write.assert_not_called()


@pytest.mark.parametrize(
    "data",
    [
        "not json",
        "null",
        "[]",
        "0",
        '{"resilience": null}',
        '{"resilience": []}',
        config(None),
        config(0),
        config(False),
        config([]),
        config({}),
    ],
)
def test_unexpected_configuration_fails_without_write(data, commands):
    read, write = commands
    read.return_value = data
    with pytest.raises(snap.SnapError):
        oom.ensure_snap_oom_protection(SNAP_NAME)
    write.assert_not_called()


@pytest.mark.parametrize(
    "error", [OSError("read failed"), subprocess.CalledProcessError(1, "/usr/bin/snap")]
)
def test_read_failure_is_not_an_absent_hint(error, commands):
    read, write = commands
    read.side_effect = error
    with pytest.raises(snap.SnapError):
        oom.ensure_snap_oom_protection(SNAP_NAME)
    write.assert_not_called()


@pytest.mark.parametrize(
    "error", [OSError("write failed"), subprocess.CalledProcessError(1, "/usr/bin/snap")]
)
def test_write_failure_does_not_retry_or_replace(error, commands):
    read, write = commands
    read.return_value = config("postgresql")
    write.side_effect = error
    with pytest.raises(snap.SnapError):
        oom.ensure_snap_oom_protection(SNAP_NAME)
    read.assert_called_once()
    write.assert_called_once_with([
        "/usr/bin/snap",
        "set",
        "system",
        f"{SNAP_VITALITY_HINT}=postgresql,{SNAP_NAME}",
    ])


@pytest.mark.parametrize(
    "verified",
    [
        config("postgresql,other"),
        config(SNAP_NAME),
        config(f"other,postgresql,{SNAP_NAME}"),
        config(f"postgresql,{SNAP_NAME},other"),
        "not json",
        "{}",
        subprocess.CalledProcessError(1, "/usr/bin/snap"),
    ],
)
def test_failed_verification_does_not_rewrite(verified, commands):
    read, write = commands
    read.side_effect = [config("postgresql,other"), verified]
    with pytest.raises(snap.SnapError):
        oom.ensure_snap_oom_protection(SNAP_NAME)
    assert read.call_count == 2
    write.assert_called_once()


def test_verification_uses_final_rank(commands):
    read, _ = commands
    read.side_effect = [config("postgresql"), config(f"postgresql,{SNAP_NAME},other,{SNAP_NAME}")]
    assert oom.ensure_snap_oom_protection(SNAP_NAME) == -896
