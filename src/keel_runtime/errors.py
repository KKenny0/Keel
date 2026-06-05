"""Runtime-specific exceptions."""


class KeelError(Exception):
    """Base exception for Keel runtime failures."""


class JobNotFoundError(KeelError):
    """Raised when a job id does not exist in the configured store."""


class InvalidJobStateError(KeelError):
    """Raised when an operation is not valid for the current job state."""


class RuntimeExecutionError(KeelError):
    """Raised when the underlying agent process fails."""


class StorageSyncError(KeelError):
    """Raised when a job cannot be synced to or restored from object storage."""
