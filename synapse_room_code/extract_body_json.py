import json
from typing import Any

from synapse.http.site import SynapseRequest


async def extract_body_json(request: SynapseRequest) -> Any:
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
