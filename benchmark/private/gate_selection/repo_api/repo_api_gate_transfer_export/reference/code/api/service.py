from .errors import InputFault, failure
from .validation import require_count


class ServiceAPI:
    def __init__(self):
        self.records = []
        self.next_id = 1

    def _read(self, body):
        if type(body) is not dict:
            raise InputFault("body")
        if "days" not in body:
            raise InputFault("days")

        value = require_count(body["days"], "days", 90)
        option = body.get("compressed", False)
        if type(option) is not bool:
            raise InputFault("compressed")
        return value, option

    def dispatch(self, method, path, body=None):
        if method == "GET" and path == "/health":
            return 200, {"status": "ok"}
        if path != "/exports":
            return 404, {"error": {"code": "not_found"}}
        if method != "POST":
            return 405, {"error": {"code": "method_not_allowed"}}
        try:
            value, option = self._read(body)
        except InputFault as exc:
            return failure(exc.field)
        record = {"id": self.next_id, "days": value, "compressed": option}
        self.records.append(record)
        self.next_id += 1
        return 201, dict(record)
