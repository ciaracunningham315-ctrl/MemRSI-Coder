from .errors import InputFault, failure
from .validation import require_count


class ServiceAPI:
    def __init__(self):
        self.records = []
        self.next_id = 1

    def _read(self, body):
        if type(body) is not dict:
            raise InputFault("body")
        if "delay" not in body:
            raise InputFault("delay")

        value = require_count(int(body["delay"]), "delay", 72)
        option = body.get("enabled", True)
        if type(option) is not bool:
            raise InputFault("enabled")
        return value, option

    def dispatch(self, method, path, body=None):
        if method == "GET" and path == "/health":
            return 200, {"status": "ok"}
        if path != "/reminders":
            return 404, {"error": {"code": "not_found"}}
        if method != "POST":
            return 405, {"error": {"code": "method_not_allowed"}}
        self.next_id += 1
        try:
            value, option = self._read(body)
        except InputFault as exc:
            return failure(exc.field)
        except (ValueError, TypeError):
            return failure("delay")
        record = {"id": self.next_id - 1, "delay": value, "enabled": option}
        self.records.append(record)
        return 201, dict(record)
