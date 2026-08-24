"""Transport-neutral message envelopes for the generic ContextForge bridge."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BRIDGE_PROTOCOL_VERSION: Literal["1.0"] = "1.0"
BridgeOperation = Literal[
    "prepare_discovery_candidates",
    "expand_discovery",
    "read_verified_context",
    "package_verified_context",
]

_MESSAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class BridgeProtocolModel(BaseModel):
    """Closed immutable base for transport-neutral protocol messages."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class BridgeRequest(BridgeProtocolModel):
    """One versioned application request; transports supply framing separately."""

    protocol_version: Literal["1.0"] = BRIDGE_PROTOCOL_VERSION
    kind: Literal["request"] = "request"
    request_id: str
    operation: BridgeOperation
    repository_root: str = Field(min_length=1, max_length=32_768)
    payload: dict[str, Any] = Field(default_factory=dict)
    cancellation_id: str | None = None

    @field_validator("request_id", "cancellation_id")
    @classmethod
    def validate_id(cls, value: str | None) -> str | None:
        if value is not None and not _MESSAGE_ID.fullmatch(value):
            raise ValueError("protocol IDs must be bounded portable identifiers")
        return value

    @field_validator("repository_root")
    @classmethod
    def validate_repository_root(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("repository_root must be bounded non-empty text")
        return value


class BridgeCancellation(BridgeProtocolModel):
    """Cooperative cancellation message independent of request transport."""

    protocol_version: Literal["1.0"] = BRIDGE_PROTOCOL_VERSION
    kind: Literal["cancel"] = "cancel"
    cancellation_id: str

    @field_validator("cancellation_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _MESSAGE_ID.fullmatch(value):
            raise ValueError("protocol IDs must be bounded portable identifiers")
        return value


class BridgeError(BridgeProtocolModel):
    """Safe structured failure returned without a partial successful result."""

    code: str
    message: str = Field(min_length=1, max_length=2_000)
    retryable: bool = False

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        if not _MESSAGE_ID.fullmatch(value):
            raise ValueError("error code must be a bounded portable identifier")
        return value


class BridgeResponse(BridgeProtocolModel):
    """All-or-nothing response envelope for a matching bridge request."""

    protocol_version: Literal["1.0"] = BRIDGE_PROTOCOL_VERSION
    kind: Literal["response"] = "response"
    request_id: str
    ok: bool
    result: dict[str, Any] | None = None
    error: BridgeError | None = None

    @field_validator("request_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _MESSAGE_ID.fullmatch(value):
            raise ValueError("protocol IDs must be bounded portable identifiers")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> BridgeResponse:
        success_shape = self.result is not None and self.error is None
        failure_shape = self.result is None and self.error is not None
        if (self.ok and not success_shape) or (not self.ok and not failure_shape):
            raise ValueError("response must contain exactly one matching outcome")
        return self


__all__ = [
    "BRIDGE_PROTOCOL_VERSION",
    "BridgeCancellation",
    "BridgeError",
    "BridgeOperation",
    "BridgeProtocolModel",
    "BridgeRequest",
    "BridgeResponse",
]
