"""Errors raised by the data acquisition stage."""


class DataFactoryError(Exception):
    """Base error for the data factory."""


class ConfigurationError(DataFactoryError):
    """The acquisition settings are invalid."""


class DownloadError(DataFactoryError):
    """The HTTP request failed after its retry budget."""


class DataError(DataFactoryError):
    """The server returned an invalid batch."""


class CheckpointError(DataFactoryError):
    """Local acquisition state is inconsistent or belongs to another scope."""
