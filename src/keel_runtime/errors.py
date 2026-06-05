"""Runtime-specific exceptions."""


class KeelError(Exception):
    """Base exception for Keel runtime failures."""


class JobNotFoundError(KeelError):
    """Raised when a job id does not exist in the configured store."""


class InvalidJobStateError(KeelError):
    """Raised when an operation is not valid for the current job state."""


class RuntimeExecutionError(KeelError):
    """Raised when the underlying agent process fails."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int | None = None,
        timed_out: bool = False,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.timed_out = timed_out


class RuntimeTimeoutError(RuntimeExecutionError):
    """Raised when the underlying agent process exceeds its timeout."""

    def __init__(self, message: str) -> None:
        super().__init__(message, timed_out=True)


class StorageSyncError(KeelError):
    """Raised when a job cannot be synced to or restored from object storage."""
