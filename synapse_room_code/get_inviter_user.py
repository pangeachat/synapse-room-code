from typing import Optional

from synapse.module_api import ModuleApi
from synapse.types import UserID

from synapse_room_code.constants import (
    DEFAULT_INVITE_POWER_LEVEL,
    DEFAULT_USERS_DEFAULT_POWER_LEVEL,
    EVENT_TYPE_M_ROOM_POWER_LEVELS,
    INVITE_POWER_LEVEL_KEY,
    USERS_DEFAULT_POWER_LEVEL_KEY,
    USERS_POWER_LEVEL_KEY,
)
from synapse_room_code.user_is_room_member import user_is_room_member


async def get_inviter_user(api: ModuleApi, room_id: str) -> Optional[UserID]:
    # inviter must be local and have sufficient power to invite

    # extract room power levels
    power_levels_state_events = await api.get_room_state(
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

    # Find the user with the highest power level that is still a member of the room
    local_user_id_with_highest_power = None
    highest_local_power = users_default
    for user_id, power_level in users_power_level.items():
        # ensure power level is an integer
        try:
            power_level = int(power_level)
        except ValueError:
            continue

        # ensure user_id is a string
        if not isinstance(user_id, str):
            continue

        # ensure user is a member of the room
        is_member = await user_is_room_member(api=api, user_id=user_id, room_id=room_id)
        if not is_member:
            continue

        if power_level > highest_local_power and api.is_mine(user_id):
            highest_local_power = power_level
            local_user_id_with_highest_power = user_id

    # Check if the user with the highest power level can invite
    if local_user_id_with_highest_power is None:
        return None

    if highest_local_power < invite_power:
        return None

    return UserID.from_string(local_user_id_with_highest_power)
