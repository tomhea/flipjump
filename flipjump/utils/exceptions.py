"""
the flipjump exception hierarchy.
defines FlipJumpException and its specific subtypes for the different failure points -
parsing, preprocessing, assembling, writing the fjm, runtime, and io errors.
"""


class FlipJumpException(Exception):
    pass


class FlipJumpParsingException(FlipJumpException):
    pass


class FlipJumpPreprocessorException(FlipJumpException):
    pass


class FlipJumpExprException(FlipJumpException):
    pass


class FlipJumpAssemblerException(FlipJumpException):
    pass


class FlipJumpReadFjmException(FlipJumpException):
    pass


class FlipJumpWriteFjmException(FlipJumpException):
    pass


class FlipJumpRuntimeException(FlipJumpException):
    pass


class FlipJumpRuntimeMemoryException(FlipJumpRuntimeException):
    def __init__(self, message: str, memory_address: int):
        super().__init__(message)
        self.memory_address = memory_address


class FlipJumpFlatMemoryException(FlipJumpRuntimeException):
    """the flat memory a run asked for (--flat-memory, given or compiled into the .fjm) could not be allocated"""

    def __init__(self, message: str, window_words: int):
        super().__init__(message)
        self.window_words = window_words


class FlipJumpMissingImportException(FlipJumpException):
    pass


class IODeviceException(IOError, FlipJumpException):
    pass


class BrokenIOUsed(IODeviceException):
    pass


class IOReadOnEOF(IODeviceException):
    pass


class IncompleteOutput(IODeviceException):
    pass
