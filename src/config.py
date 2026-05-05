#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Structured configuration for the PostgreSQL charm."""

from typing import Literal

from charms.data_platform_libs.v1.data_models import BaseConfigModel


class CharmConfig(BaseConfigModel):
    """Manager for the structured configuration."""

    profile: Literal["testing", "production"]

    @classmethod
    def keys(cls) -> list[str]:
        """Return config as list items."""
        return list(cls.model_fields.keys())

    @classmethod
    def plugin_keys(cls) -> filter:
        """Return plugin config names in a iterable."""
        return filter(lambda x: x.startswith("plugin_"), cls.keys())
