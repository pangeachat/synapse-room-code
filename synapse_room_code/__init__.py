import logging
from typing import Any, Dict

import attr
from synapse.module_api import ModuleApi

from synapse_room_code.knock_with_code import (
    KnockWithCode as KnockWithCodeResource,
)
from synapse_room_code.request_room_code import RequestRoomCode

logger = logging.getLogger(f"synapse.module.{__name__}")


@attr.s(auto_attribs=True, frozen=True)
class SynapseRoomCodeConfig:
    knock_with_code_requests_per_burst: int = 10
    knock_with_code_burst_duration_seconds: int = 60


class SynapseRoomCode:
    def __init__(self, config: SynapseRoomCodeConfig, api: ModuleApi):
        # Keep a reference to the config and Module API
        self._api = api
        self._config = config

        # Initiate resources
        self.knock_with_code_resource = KnockWithCodeResource(api, config)
        self.request_code_resource = RequestRoomCode(api, config)

        # Register the HTTP endpoint for knock_with_code
        api.register_web_resource(
            path="/_synapse/client/pangea/v1/knock_with_code",
            resource=self.knock_with_code_resource,
        )

        # Register the HTTP endpoint for generate_access_code
        api.register_web_resource(
            path="/_synapse/client/pangea/v1/request_room_code",
            resource=self.request_code_resource,
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
