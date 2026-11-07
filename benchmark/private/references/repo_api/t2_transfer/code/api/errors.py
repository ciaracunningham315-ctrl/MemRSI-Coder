class InputError(ValueError):
    def __init__(self, field, message):
        super().__init__(message)
        self.field = ".".join(field) if isinstance(field, tuple) else field
        self.message = message
