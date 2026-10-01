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


class AddSourceDirectoryRequest(BaseModel):
    """`path` omitted means: open the machine's folder dialog and ask."""

    path: Any = None


class SubmitJobRequest(BaseModel):
    type: Any = None
    input: Any = None


class GeneratePlanRequest(BaseModel):
    instruction: Any = None


class SavePlanRevisionRequest(BaseModel):
    summary: Any = None
    actions: Any = None


class CuttingConfiguration(BaseModel):
    """The cutting form as the browser holds it.

    `settings` are the five full-clip values, `boundary_settings` the
    boundary-only ones; `mode` says which set is in force.
    """

    mode: Any = None
    settings: Any = None
    boundary_settings: Any = None
    overrides: Any = None
    sample_seconds: Any = None
    output_mode: Any = None
    source_ids: Any = None
    applied_plan: Any = None


class SaveCuttingSettingsRequest(CuttingConfiguration):
    pass


class StartCuttingRunRequest(CuttingConfiguration):
    pass


class CuttingAnalysisRequest(CuttingConfiguration):
    # Ignore the cache and decode again.
    force: Any = None


class CuttingSamplesRequest(CuttingConfiguration):
    # `[{kind, source_id, next_source_id?}]`; omitted means every preview.
    samples: Any = None


class CuttingRecommendationRequest(CuttingConfiguration):
    request: Any = None
    feedback: Any = None
    revise_plan_id: Any = None


class ApplyRecommendationRequest(CuttingConfiguration):
    confirm_mode_change: Any = None


class SaveSubtitleSettingsRequest(BaseModel):
    settings: Any = None


class StartSubtitlesRequest(BaseModel):
    """The model and the language are fixed by the backend, not requested."""

    run_id: Any = None
    output_ids: Any = None
    settings: Any = None
