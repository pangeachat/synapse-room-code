import logging
from typing import Any, Dict

import attr
from synapse.module_api import ModuleApi

from synapse_room_code.knock_with_code import (
    KnockWithCode as KnockWithCodeResource,
)

logger = logging.getLogger(f"synapse.module.{__name__}")


@attr.s(auto_attribs=True, frozen=True)
class SynapseRoomCodeConfig:
    pass


class SynapseRoomCode:
    def __init__(self, config: SynapseRoomCodeConfig, api: ModuleApi):
        # Keep a reference to the config and Module API
        self._api = api
        self._config = config

        # Initiate resources
        self.resource = KnockWithCodeResource(api)

        # Register the HTTP endpoint
        api.register_web_resource(
            path="/_matrix/_pangea/v1/client/knock_with_code",
            resource=self.resource,
        )

    @staticmethod
    def parse_config(config: Dict[str, Any]) -> SynapseRoomCodeConfig:
        # Parse the module's configuration here.
        # If there is an issue with the configuration, raise a
        # synapse.module_api.errors.ConfigError.
        #
        # Example:
        #
        #     some_option = config.get("some_option")
        #     if some_option is None:
        #          raise ConfigError("Missing option 'some_option'")
        #      if not isinstance(some_option, str):
        #          raise ConfigError("Config option 'some_option' must be a string")
        #
        return SynapseRoomCodeConfig()
