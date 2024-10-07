import json
import logging
from typing import Any

from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi
from synapse.storage.databases.main.room import RoomStore
from twisted.internet import defer
from twisted.web.resource import Resource

from synapse_room_code.constants import (
    ACCESS_CODE_KNOCK_EVENT_CONTENT_KEY,
    MEMBERSHIP_CONTENT_KEY,
    MEMBERSHIP_KNOCK,
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
            sent_rooms: list[str] = []
            for room_id in room_ids:
                try:
                    await self.send_knock_with_code(
                        room_id=room_id,
                        user_id=requester_id,
                        access_code=access_code,
                    )
                    sent_rooms.append(room_id)
                except Exception as e:
                    logger.error(f"Error sending knock with code to {room_id}: {e}")
            respond_with_json(
                request,
                200,
                {"message": f"Sent invites to {', '.join(sent_rooms)}"},
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

    async def _get_rooms_with_access_code(self, access_code: str) -> list[str]:
        """
        Query the Synapse database for rooms that have a state event `m.room.join_rules`
        with content that includes the provided access code.

        :param access_code: The access code to search for.
        :return: A list of room IDs where the `access_code` matches. None if there was an error
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
            params = [access_code]  # Use a list with placeholders

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
        room_ids: list[str] = []
        for row in rows:
            if isinstance(row, str):
                room_ids.append(row)
            if isinstance(row, tuple) and len(row) > 0:
                room_ids.append(row[0])
        return room_ids

    async def send_knock_with_code(self, room_id: str, user_id: str, access_code: str):
        """
        Send a knock event to a room with an access code.

        :param room_id: The room ID to send the knock event to.
        :param user_id: The user ID that is sending the knock event.
        :param access_code: The access code to include in the knock event.
        """
        await self._api.update_room_membership(
            room_id=room_id,
            sender=user_id,
            target=user_id,
            new_membership=MEMBERSHIP_KNOCK,
            content={
                MEMBERSHIP_CONTENT_KEY: MEMBERSHIP_KNOCK,
                ACCESS_CODE_KNOCK_EVENT_CONTENT_KEY: access_code,
            },
        )
