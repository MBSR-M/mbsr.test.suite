"""Versioned contracts; legacy imports retain their original request semantics."""

from .legacy import (
    AssetInput,
    AssignmentInput,
    CaseUpdate,
    Contract,
    EventInput,
    ReadingInput,
    ReprocessInput,
)

__all__ = [
    "AssetInput",
    "AssignmentInput",
    "CaseUpdate",
    "Contract",
    "EventInput",
    "ReadingInput",
    "ReprocessInput",
]
