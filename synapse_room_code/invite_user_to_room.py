from synapse.module_api import ModuleApi

from synapse_room_code.constants import (
    MEMBERSHIP_CONTENT_KEY,
    MEMBERSHIP_INVITE,
)
from synapse_room_code.get_inviter_user import get_inviter_user


async def invite_user_to_room(api: ModuleApi, user_id: str, room_id: str) -> None:
    # Get a user with permission to invite
    inviter_user = await get_inviter_user(api=api, room_id=room_id)
    if inviter_user is None:
        return
    inviter_user_id = inviter_user.to_string()
    content = {MEMBERSHIP_CONTENT_KEY: MEMBERSHIP_INVITE}
    await api.update_room_membership(
        sender=inviter_user_id,
        target=user_id,
        room_id=room_id,
        new_membership=MEMBERSHIP_INVITE,
        content=content,
    )
