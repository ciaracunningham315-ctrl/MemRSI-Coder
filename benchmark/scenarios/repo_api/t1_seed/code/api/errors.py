class InputError(ValueError):
    def __init__(self, field, message):
        super().__init__(message)
        self.field = "body"
        self.message = message
