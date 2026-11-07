from .errors import InputFault, failure
from .validation import require_count


class ServiceAPI:
    def __init__(self):
        self.records = []
        self.next_id = 1

    def _read(self, body):
        if type(body) is not dict:
            raise InputFault("body")
        if "copies" not in body:
            raise InputFault("copies")

        value = require_count(int(body["copies"]), "copies", 9)
        option = body.get("format", 'mono')
        if type(option) is not str or option not in ['mono', 'color']:
            raise InputFault("format")
        return value, option

    def dispatch(self, method, path, body=None):
        if method == "GET" and path == "/health":
            return 200, {"status": "ok"}
        if path != "/print-jobs":
            return 404, {"error": {"code": "not_found"}}
        if method != "POST":
            return 405, {"error": {"code": "method_not_allowed"}}
        self.next_id += 1
        try:
            value, option = self._read(body)
        except InputFault as exc:
            return failure(exc.field)
        except (ValueError, TypeError):
            return failure("copies")
        record = {"id": self.next_id - 1, "copies": value, "format": option}
        self.records.append(record)
        return 201, dict(record)
