import logging
from typing import Any, Dict, Optional, Tuple, Union

import attr
from synapse.events import EventBase
from synapse.module_api import ModuleApi
from synapse.types import StateMap, UserID

from synapse_room_code.constants import (
    ACCESS_CODE_JOIN_RULE_CONTENT_KEY,
    ACCESS_CODE_KNOCK_EVENT_CONTENT_KEY,
    DEFAULT_INVITE_POWER_LEVEL,
    DEFAULT_USERS_DEFAULT_POWER_LEVEL,
    EVENT_TYPE_M_ROOM_JOIN_RULES,
    EVENT_TYPE_M_ROOM_MEMBER,
    EVENT_TYPE_M_ROOM_POWER_LEVELS,
    INVITE_POWER_LEVEL_KEY,
    MEMBERSHIP_CONTENT_KEY,
    MEMBERSHIP_INVITE,
    MEMBERSHIP_KNOCK,
    USERS_DEFAULT_POWER_LEVEL_KEY,
    USERS_POWER_LEVEL_KEY,
)

logger = logging.getLogger(__name__)


@attr.s(auto_attribs=True, frozen=True)
class SynapseRoomCodeConfig:
    pass


class SynapseRoomCode:
    def __init__(self, config: SynapseRoomCodeConfig, api: ModuleApi):
        # Keep a reference to the config and Module API
        self._api = api
        self._config = config

        # Register the method to intercept membership events
        self._api.register_third_party_rules_callbacks(on_new_event=self.on_new_event)

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

    async def on_new_event(
        self, event: EventBase, room_state: StateMap[EventBase]
    ) -> None:
        """
        Called when a new event is received.

        Args:
            event: The event that has just been received.
            room_state: The current state of the room at the event.
        """

        # Short-circuit if the event is not a membership event with a knock membership and an access code
        if (
            event.type != EVENT_TYPE_M_ROOM_MEMBER
            or event.content.get(MEMBERSHIP_CONTENT_KEY) != MEMBERSHIP_KNOCK
            or not isinstance(
                event.content.get(ACCESS_CODE_KNOCK_EVENT_CONTENT_KEY), str
            )
        ):
            return

        room_id = event.room_id

        # Proceed only if the room has a join rule of "knock" and the access code is defined in its content
        access_code: Union[str, None] = None
        join_rules_state_events = await self._api.get_room_state(
            room_id,
            event_filter=[(EVENT_TYPE_M_ROOM_JOIN_RULES, None)],
        )
        for state_event in join_rules_state_events.values():
            if state_event.type == EVENT_TYPE_M_ROOM_JOIN_RULES and isinstance(
                state_event.content.get(ACCESS_CODE_JOIN_RULE_CONTENT_KEY), str
            ):
                access_code = state_event.content.get(ACCESS_CODE_JOIN_RULE_CONTENT_KEY)
                break
        if not isinstance(access_code, str):
            return

        # Compare the class codes
        if event.content.get(ACCESS_CODE_KNOCK_EVENT_CONTENT_KEY) != access_code:
            return

        # Codes match, invite the user to the room
        await self.invite_user_to_room(event.sender, room_id)

    async def invite_user_to_room(self, user_id: str, room_id: str) -> None:
        # Get a user with permission to invite
        inviter_user = await self.get_inviter_user(room_id)
        if inviter_user is None:
            return

        content = {MEMBERSHIP_CONTENT_KEY: MEMBERSHIP_INVITE}
        await self._api.create_and_send_event_into_room(
            {
                "type": EVENT_TYPE_M_ROOM_MEMBER,
                "sender": inviter_user.to_string(),
                "state_key": user_id,
                "room_id": room_id,
                "content": content,
            }
        )

    async def get_inviter_user(self, room_id: str) -> Optional[UserID]:
        # extract room power levels
        power_levels_state_events = await self._api.get_room_state(
            room_id=room_id,
            event_filter=[(EVENT_TYPE_M_ROOM_POWER_LEVELS, None)],
        )
        power_levels = None
        for state_event in power_levels_state_events.values():
            if state_event.type != EVENT_TYPE_M_ROOM_POWER_LEVELS:
                continue
            power_levels = state_event.content
            break
        if not power_levels:
            return None

        # extract power required to invite
        try:
            invite_power = int(
                power_levels.get(
                    INVITE_POWER_LEVEL_KEY,
                    DEFAULT_INVITE_POWER_LEVEL,
                )
            )
        except ValueError:
            invite_power = DEFAULT_INVITE_POWER_LEVEL

        # extract default power level
        try:
            users_default = int(
                power_levels.get(
                    USERS_DEFAULT_POWER_LEVEL_KEY,
                    DEFAULT_USERS_DEFAULT_POWER_LEVEL,
                )
            )
        except ValueError:
            users_default = DEFAULT_USERS_DEFAULT_POWER_LEVEL

        # extract users power levels
        users_power_level = power_levels.get(USERS_POWER_LEVEL_KEY, None)
        if not isinstance(users_power_level, dict):
            users_power_level = {}

        # Find the user with the highest power level
        user_id_with_highest_power = None
        highest_power = users_default
        for user_id, power_level in users_power_level.items():
            # ensure power level is an integer
            try:
                power_level = int(power_level)
            except ValueError:
                continue

            # ensure user_id is a string
            if not isinstance(user_id, str):
                continue

            if power_level > highest_power:
                highest_power = power_level
                user_id_with_highest_power = user_id

        # Check if the user with the highest power level can invite
        if user_id_with_highest_power is None:
            return None

        if highest_power < invite_power:
            return None

        return UserID.from_string(user_id_with_highest_power)
