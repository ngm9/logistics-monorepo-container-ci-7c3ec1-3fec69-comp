import json
from datetime import datetime, timezone


def response(service, status="ok", **data):
    payload = {
        "service": service,
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(data)
    return json.dumps(payload).encode("utf-8")
