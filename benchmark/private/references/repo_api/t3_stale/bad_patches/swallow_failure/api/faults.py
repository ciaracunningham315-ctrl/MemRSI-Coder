class InputFault(ValueError):
    def __init__(self, path, message):
        super().__init__(message)
        self.path = ".".join(path) if isinstance(path, tuple) else path
        self.message = message
