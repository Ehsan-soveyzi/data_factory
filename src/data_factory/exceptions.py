"""Errors raised by the data factory and its configuration helpers."""


class DataFactoryError(Exception):
    """Base error for the data factory."""


class ConfigurationError(DataFactoryError):
    """The project settings are missing or invalid."""


class DownloadError(DataFactoryError):
    """The HTTP request failed after its retry budget."""


class DataError(DataFactoryError):
    """A source response or its records are invalid."""


class CheckpointError(DataFactoryError):
    """Local acquisition state is inconsistent or belongs to another scope."""


class SchemaDriftError(DataError):
    """A source batch no longer satisfies the structural contract."""


class QuarantineThresholdError(DataError):
    """Too many records failed validation; diagnostics were saved."""

    def __init__(self, message, *, report_path):
        super().__init__(message)
        self.report_path = str(report_path)
