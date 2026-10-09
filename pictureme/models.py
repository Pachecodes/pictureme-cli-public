"""Pydantic models mirroring the v3 API shapes.

Keep these in sync with:
  - pictureme-go/internal/handlers/model_handlers.go (modelToPublicJSON)
  - pictureme-go/internal/handlers/generate_v3_handlers.go (jobToPublicJSON)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Permissive(BaseModel):
    model_config = ConfigDict(extra="allow")


class ModelMediaSlot(_Permissive):
    field: Optional[str] = None
    max: Optional[int] = None
    required: Optional[bool] = None


class Model(_Permissive):
    model_id: str
    display_name: str
    description: str = ""
    provider: str
    provider_model_id: str
    model_type: str
    capabilities: list[str] = Field(default_factory=list)
    default_cost: int = 0
    cost_rules: dict[str, Any] = Field(default_factory=dict)
    default_params: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    media_inputs: dict[str, ModelMediaSlot] = Field(default_factory=dict)
    poll_profile: str = "medium"


class JobOutput(_Permissive):
    type: str
    url: str


class Job(_Permissive):
    job_id: int
    status: str
    kind: str = ""
    model_id: Optional[str] = None
    prompt: Optional[str] = None
    aspect_ratio: Optional[str] = None
    parent_id: Optional[int] = None
    outputs: list[JobOutput] = Field(default_factory=list)
    error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # Returned only on create — initial cost snapshot.
    cost: Optional[dict[str, Any]] = None
    request_id: Optional[str] = None
