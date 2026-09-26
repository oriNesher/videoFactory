"""Request bodies for the project endpoints.

Fields are typed loosely on purpose: `storage` performs the real validation so
that every rejection comes back as one clear, translated message instead of a
FastAPI validation dump.
"""

from typing import Any

from pydantic import BaseModel


class CreateProjectRequest(BaseModel):
    name: Any = None


class SaveProjectRequest(BaseModel):
    name: Any = None
    source_ids: Any = None


class AddSourceRequest(BaseModel):
    path: Any = None


class SubmitJobRequest(BaseModel):
    type: Any = None
    input: Any = None


class GeneratePlanRequest(BaseModel):
    instruction: Any = None


class SavePlanRevisionRequest(BaseModel):
    summary: Any = None
    actions: Any = None


class SaveCuttingSettingsRequest(BaseModel):
    settings: Any = None
    output_mode: Any = None
    source_ids: Any = None


class StartCuttingRunRequest(BaseModel):
    source_ids: Any = None
    settings: Any = None
    output_mode: Any = None
