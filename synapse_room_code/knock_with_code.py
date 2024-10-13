import logging
from typing import List

from synapse.api.errors import (
    AuthError,
    InvalidClientCredentialsError,
    MissingClientTokenError,
    InvalidClientTokenError,
)
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi
from twisted.internet import defer
from twisted.web.resource import Resource

from synapse_room_code.extract_body_json import extract_body_json
from synapse_room_code.get_rooms_with_access_code import (
    get_rooms_with_access_code,
)
from synapse_room_code.invite_user_to_room import invite_user_to_room

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
            requester = await self._auth.get_user_by_req(request)
            body = await extract_body_json(request)
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
            if (
                len(access_code) != 7
                or not access_code.isalnum()
                or not any(char.isdigit() for char in access_code)  # At least one digit
            ):
                logger.error(f"Invalid 'access_code': {access_code}")
                respond_with_json(
                    request,
                    400,
                    {"error": f"Invalid 'access_code': {access_code}"},
                    send_cors=True,
                )
                return

            # Get the rooms with the access code
            room_ids = await get_rooms_with_access_code(
                access_code=access_code, room_store=self._datastores.main
            )
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
            requester_id = requester.user.to_string()
            invited_rooms: List[str] = []
            for room_id in room_ids:
                try:
                    await invite_user_to_room(
                        api=self._api,
                        user_id=requester_id,
                        room_id=room_id,
                    )
                    invited_rooms.append(room_id)
                except Exception as e:
                    logger.error(f"Error sending knock with code to {room_id}: {e}")
            respond_with_json(
                request,
                200,
                {"message": f"Invited {requester_id}", "rooms": invited_rooms},
                send_cors=True,
            )
        except (
            MissingClientTokenError,
            InvalidClientTokenError,
            InvalidClientCredentialsError,
            AuthError,
        ) as e:
            logger.error(f"Forbidden: {e}")
            respond_with_json(
                request,
                403,
                {"error": "Forbidden"},
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
