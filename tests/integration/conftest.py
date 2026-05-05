# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
import logging

import pytest

from . import architecture

logger = logging.getLogger(__name__)


@pytest.fixture(scope="session")
def charm():
    # Return str instead of pathlib.Path since python-libjuju's model.deploy(), juju deploy, and
    # juju bundle files expect local charms to begin with `./` or `/` to distinguish them from
    # Charmhub charms.
    return f"./postgresql-watcher_ubuntu@24.04-{architecture.architecture}.charm"
