"""Stable, safe error payloads for additive canonical APIs."""

from enum import StrEnum
from uuid import UUID

from pydantic import Field, JsonValue

from .common import CanonicalContract


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INVALID_REQUEST = "INVALID_REQUEST"
    UNSUPPORTED_SCHEMA_VERSION = "UNSUPPORTED_SCHEMA_VERSION"
    NOT_FOUND = "NOT_FOUND"
    FEEDER_NOT_FOUND = "FEEDER_NOT_FOUND"
    TRANSFORMER_NOT_FOUND = "TRANSFORMER_NOT_FOUND"
    METER_NOT_FOUND = "METER_NOT_FOUND"
    INVESTIGATION_NOT_FOUND = "INVESTIGATION_NOT_FOUND"
    DUPLICATE_RECORD = "DUPLICATE_RECORD"
    READING_INVALID = "READING_INVALID"
    READING_OUT_OF_ORDER = "READING_OUT_OF_ORDER"
    REGISTER_RESET = "REGISTER_RESET"
    REGISTER_ROLLOVER = "REGISTER_ROLLOVER"
    DATABASE_ERROR = "DATABASE_ERROR"
    KAFKA_ERROR = "KAFKA_ERROR"
    RABBITMQ_ERROR = "RABBITMQ_ERROR"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    AUTHORIZATION_FAILED = "AUTHORIZATION_FAILED"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorDetailContract(CanonicalContract):
    code: ErrorCode
    message: str = Field(min_length=1, max_length=500)
    details: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="Safe validation/context information; never credentials, stack traces or SQL.",
    )
    request_id: UUID


class ErrorResponse(CanonicalContract):
    error: ErrorDetailContract


APIErrorContract = ErrorResponse
