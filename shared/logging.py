from datetime import datetime, timezone


def request_line(service, path):
    return f"{datetime.now(timezone.utc).isoformat()} service={service} path={path}"
