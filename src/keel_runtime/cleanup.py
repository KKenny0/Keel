"""Job cleanup policies."""

from __future__ import annotations

from dataclasses import dataclass

from keel_runtime.jobs import JobStatus


@dataclass(slots=True)
class CleanupPolicy:
    remove_workspace_on_success: bool = False
    remove_workspace_on_failure: bool = False
    remove_artifacts_on_success: bool = False
    remove_artifacts_on_failure: bool = False

    def workspace_for_status(self, status: JobStatus) -> bool:
        if status == JobStatus.SUCCEEDED:
            return self.remove_workspace_on_success
        if status == JobStatus.FAILED:
            return self.remove_workspace_on_failure
        return False

    def artifacts_for_status(self, status: JobStatus) -> bool:
        if status == JobStatus.SUCCEEDED:
            return self.remove_artifacts_on_success
        if status == JobStatus.FAILED:
            return self.remove_artifacts_on_failure
        return False
