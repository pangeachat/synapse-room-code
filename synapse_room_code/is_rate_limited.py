import time
from typing import Dict, List

request_log: Dict[str, List[str]] = {}


def is_rate_limited(user_id: str, window_s: int = 300, max_req: int = 5) -> bool:
    current_time = time.time()

    # Get the list of request timestamps for the user, or create an empty list if new user
    if user_id not in request_log:
        request_log[user_id] = []

    # Filter out requests that are older than the time window
    request_log[user_id] = [
        timestamp
        for timestamp in request_log[user_id]
        if current_time - timestamp <= window_s
    ]

    # Check if the number of requests in the time window exceeds the max limit
    if len(request_log[user_id]) >= max_req:
        return True

    # If not rate-limited, record the new request timestamp
    request_log[user_id].append(current_time)

    return False
