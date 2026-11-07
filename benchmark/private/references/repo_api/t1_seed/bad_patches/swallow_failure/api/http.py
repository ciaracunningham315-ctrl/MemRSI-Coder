def response(status, body):
    """Construct a fresh JSON-compatible HTTP response envelope."""
    return {"status": status, "headers": {"content-type": "application/json"}, "body": body}


def error_response(status, code, message, field=None):
    detail = {"code": code, "message": message}
    if field is not None:
        detail["field"] = field
    return response(status, {"error": detail})
