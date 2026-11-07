from copy import deepcopy

class StatusAPI:
    def __init__(self):
        self.metadata = {"service": "dispatch-tools", "labels": ["stable"], "limits": {"workers": 2}}

    def dispatch(self, method, path):
        if path != "/status":
            return 404, {"error": {"code": "not_found"}}
        if method != "GET":
            return 405, {"error": {"code": "method_not_allowed"}}
        return 200, deepcopy(self.metadata)
