import json
import logging
from typing import Any, List, Optional

from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi
from synapse.storage.databases.main.room import RoomStore
from synapse.types import UserID
from twisted.internet import defer
from twisted.web.resource import Resource

from synapse_room_code.constants import (
    DEFAULT_INVITE_POWER_LEVEL,
    DEFAULT_USERS_DEFAULT_POWER_LEVEL,
    EVENT_TYPE_M_ROOM_POWER_LEVELS,
    INVITE_POWER_LEVEL_KEY,
    MEMBERSHIP_CONTENT_KEY,
    MEMBERSHIP_INVITE,
    USERS_DEFAULT_POWER_LEVEL_KEY,
    USERS_POWER_LEVEL_KEY,
)

logger = logging.getLogger("synapse.module.synapse_room_code.knock_with_code")


class KnockWithCode(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi):
        super().__init__()
        self._api = api
        self._auth = self._api._hs.get_auth()
        self._datastores = self._api._hs.get_datastores()

    def render_POST(self, request: SynapseRequest):
        defer.ensureDeferred(self._async_render_POST(request))
        return server.NOT_DONE_YET

    async def _async_render_POST(self, request: SynapseRequest):
        try:
            body = await self._extract_body_json(request)
            if not isinstance(body, dict):
                respond_with_json(
                    request,
                    400,
                    {"error": "Invalid JSON in request body"},
                    send_cors=True,
                )
                return

            # Check if the request body contains the access code
            if "access_code" not in body:
                logger.error("Missing 'access_code' in request body")
                respond_with_json(
                    request,
                    400,
                    {"error": "Missing 'access_code' in request body"},
                    send_cors=True,
                )
                return
            access_code = body["access_code"]

            # Check if the access code is a string and has the correct format
            if not isinstance(access_code, str):
                logger.error("'access_code' must be a string")
                respond_with_json(
                    request,
                    400,
                    {"error": "'access_code' must be a string"},
                    send_cors=True,
                )
                return
            if len(access_code) != 7 or not access_code.isalnum():
                logger.error("Invalid 'access_code'")
                respond_with_json(
                    request,
                    400,
                    {"error": "Invalid 'access_code'"},
                    send_cors=True,
                )
                return

            # Get the rooms with the access code
            room_ids = await self._get_rooms_with_access_code(access_code)
            if room_ids is None:
                respond_with_json(
                    request,
                    500,
                    {"error": "Internal server error"},
                    send_cors=True,
                )
                return
            if len(room_ids) == 0:
                respond_with_json(
                    request,
                    400,
                    {"error": "Invalid 'access_code'"},
                    send_cors=True,
                )
                return

            # Send knock with access code to the rooms as requester
            requester = await self._auth.get_user_by_req(request)
            requester_id = requester.user.to_string()
            invited_rooms: List[str] = []
            for room_id in room_ids:
                try:
                    await self._invite_user_to_room(requester_id, room_id)
                    invited_rooms.append(room_id)
                except Exception as e:
                    logger.error(f"Error sending knock with code to {room_id}: {e}")
            respond_with_json(
                request,
                200,
                {"message": f"Invited {requester_id} to {', '.join(invited_rooms)}"},
                send_cors=True,
            )
        except Exception as e:
            logger.error(f"Error processing request: {e}")
            respond_with_json(
                request,
                500,
                {"error": "Internal server error"},
                send_cors=True,
            )

    async def _extract_body_json(self, request: SynapseRequest) -> Any:
        content_type = request.getHeader("Content-Type")
        if content_type != "application/json":
            return None
        try:
            body = request.content.read()
            body_str = body.decode("utf-8")
            body_json = json.loads(body_str)
            return body_json
        except Exception:
            return None

    async def _get_rooms_with_access_code(self, access_code: str) -> List[str]:
        """
        Query the Synapse database for rooms that have a state event `m.room.join_rules`
        with content that includes the provided access code.

        :param access_code: The access code to search for.
        :return: A List of room IDs where the `access_code` matches. None if there was an error
        """
        # Access the database connection
        store: RoomStore = self._datastores.main
        # Execute the query and retrieve room IDs
        # Check which database backend we are using
        database_engine = store.db_pool.engine.module.__name__

        if "sqlite" in database_engine:
            # SQLite: use json_extract
            query = """
            SELECT e.room_id
            FROM events e
                JOIN state_events se ON e.event_id = se.event_id
                JOIN event_json ej ON e.event_id = ej.event_id
            WHERE
                e.type = 'm.room.join_rules'
                AND se.room_id = e.room_id
                AND se.type = 'm.room.join_rules'
                AND json_extract(ej.json, '$.content.access_code') = ?
            GROUP BY se.room_id
            HAVING MAX(e.origin_server_ts)
            """
            params = (access_code,)  # Use a List with placeholders

        else:
            # PostgreSQL: use jsonb_extract_path_text
            query = """
            SELECT e.room_id
            FROM events e
                JOIN state_events se ON e.event_id = se.event_id
                JOIN event_json ej ON e.event_id = ej.event_id
            WHERE
                e.type = 'm.room.join_rules'
                AND se.room_id = e.room_id
                AND se.type = 'm.room.join_rules'
                AND ej.json->'content'->>'access_code' = %s
            GROUP BY se.room_id
            HAVING MAX(e.origin_server_ts)
            """
            params = (access_code,)  # Use a tuple with placeholders

        rows = await store.db_pool.execute(
            "get_rooms_with_access_code",
            query,
            *params,
        )
        room_ids: List[str] = []
        for row in rows:
            if isinstance(row, str):
                room_ids.append(row)
            if isinstance(row, tuple) and len(row) > 0:
                room_ids.append(row[0])
        return room_ids

    async def _invite_user_to_room(self, user_id: str, room_id: str) -> None:
        # Get a user with permission to invite
        inviter_user = await self._get_inviter_user(room_id)
        if inviter_user is None:
            return
        inviter_user_id = inviter_user.to_string()
        content = {MEMBERSHIP_CONTENT_KEY: MEMBERSHIP_INVITE}
        event = await self._api.update_room_membership(
            sender=inviter_user_id,
            target=user_id,
            room_id=room_id,
            new_membership=MEMBERSHIP_INVITE,
            content=content,
        )
        logger.debug(
            f"{inviter_user_id} invited {user_id} to {room_id}: {event.get_dict()}"
        )

    async def _get_inviter_user(self, room_id: str) -> Optional[UserID]:
        # inviter must be local and have sufficient power to invite

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

            if power_level > highest_local_power and self._api.is_mine(user_id):
                highest_local_power = power_level
                local_user_id_with_highest_power = user_id

        # Check if the user with the highest power level can invite
        if local_user_id_with_highest_power is None:
            return None

        if highest_local_power < invite_power:
            return None

        return UserID.from_string(local_user_id_with_highest_power)
