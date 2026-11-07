from .faults import InputFault, failure
from .validation import read_count


class ServiceAPI:
    def __init__(self):
        self.records = []
        self.next_id = 1

    def _read(self, body):
        if type(body) is not dict:
            raise InputFault("body")
        if "schedule" not in body:
            raise InputFault("schedule.interval")

        if type(body["schedule"]) is not dict or "interval" not in body["schedule"]:
            raise InputFault("schedule.interval")

        value = read_count(body["schedule"]["interval"], "schedule.interval", 60)
        option = body.get("channel", 'email')
        if type(option) is not str or option not in ['email', 'sms']:
            raise InputFault("channel")
        return value, option

    def dispatch(self, method, path, body=None):
        if method == "GET" and path == "/health":
            return 200, {"status": "ok"}
        if path != "/notifications":
            return 404, {"error": {"code": "not_found"}}
        if method != "POST":
            return 405, {"error": {"code": "method_not_allowed"}}
        try:
            value, option = self._read(body)
        except InputFault as exc:
            return failure(exc.field)
        record = {"id": self.next_id, "schedule.interval": value, "channel": option}
        self.records.append(record)
        self.next_id += 1
        return 201, dict(record)
