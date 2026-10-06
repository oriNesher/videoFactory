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


class CaptionImportRequest(BaseModel):
    """`path` omitted means: open the machine's file dialog and ask."""

    path: Any = None


class CaptionSelectionRequest(BaseModel):
    input_key: Any = None


class CaptionTranscriptionRequest(BaseModel):
    input_key: Any = None
    settings: Any = None
    # Make a new transcript even when a matching one exists.
    force: Any = None


class ModelDownloadRequest(BaseModel):
    model: Any = None


class CaptionValidateRequest(BaseModel):
    captions: Any = None


class CaptionOperationRequest(BaseModel):
    captions: Any = None
    operation: Any = None
    caption_id: Any = None
    word_index: Any = None
    at_seconds: Any = None


class CaptionRevisionRequest(BaseModel):
    base_revision: Any = None
    captions: Any = None


class CaptionRegroupRequest(BaseModel):
    base_revision: Any = None
    grouping: Any = None
    confirm_discard_edits: Any = None


class CaptionStyleRequest(BaseModel):
    style: Any = None


class CaptionExportRequest(BaseModel):
    transcript_id: Any = None
    revision: Any = None
    format: Any = None
    # The video to draw on, when it is not the transcript's own: a zoomed
    # version of it (stage 2B). The backend checks the lineage.
    input_key: Any = None


class ZoomSettingsRequest(BaseModel):
    preference: Any = None
    selected_input: Any = None


class ZoomStartRequest(BaseModel):
    input_key: Any = None
    preference: Any = None
    # Ask the provider again even though a plan exists for this transcript.
    force_replan: Any = None
    # Render (or re-render) a stored plan without asking the provider.
    plan_id: Any = None


class ZoomApproveRequest(BaseModel):
    # What the reviewer actually saw: the output's fingerprint and the caption
    # revision / style revision drawn over it (or null for no captions).
    output_digest: Any = None
    captions: Any = None


class StartSubtitlesRequest(BaseModel):
    """The model and the language are fixed by the backend, not requested."""

    run_id: Any = None
    output_ids: Any = None
    settings: Any = None
