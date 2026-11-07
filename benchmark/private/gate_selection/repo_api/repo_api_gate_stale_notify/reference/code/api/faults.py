class InputFault(ValueError):
    def __init__(self, field):
        self.field = field


def failure(field):
    return 400, {"error": {"code": "invalid_request", "field": field}}
