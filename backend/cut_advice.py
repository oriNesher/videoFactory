"""AI-assisted cutting settings: one recommendation, reviewed before it applies.

The user describes the pacing they want ("make the joins a little tighter
without clipping the first or last word"). The configured provider is given
the two cutting capabilities with their real parameter ranges, the mode and
settings currently selected, and *measurements* of each clip — durations,
detected boundary times, loudness statistics — and returns a mode, settings,
a short explanation and its limitations.

What this module guarantees around that:

- **No media leaves the machine, and nothing pretends otherwise.** The request
  holds numbers and the user's own text. It carries no audio, video, file name
  or path, and it says in so many words that the model has heard nothing.
- **The backend decides what is valid.** The proposal is checked against the
  same validators the manual form uses; an unknown parameter or an
  out-of-range value fails the job instead of being saved.
- **It is a plan.** A valid proposal is stored as an ordinary plan revision
  (`backend/plans.py`), so it is versioned, editable, and needs the same
  explicit approval as any other AI plan before it has any effect.
- **It goes stale.** The proposal records the fingerprint of the configuration
  it was based on. Once the settings or a manual boundary change, it can no
  longer be applied.
- **It cannot quietly change the mode.** A proposal for a different mode than
  the one selected is flagged, and applying it needs a separate confirmation.
- **One shot.** There is no loop here: one recommendation per request, and a
  revision only when the user asks for one with feedback.
"""

from typing import Any

from . import (
    boundaries,
    capabilities,
    cut_runner,
    cut_samples,
    cutting,
    jobs,
    llm,
    plans,
    processes,
    storage,
)

CONTEXT_KIND = "cut_recommendation"

MAX_LIMITATIONS = 8
MAX_LIMITATION_LENGTH = 400

ACTION_ID = "cut"

# Shown in the interface next to the request box, so what a real provider
# receives is never a surprise.
DATA_SENT = [
    "the request and feedback you typed",
    "the selected cutting mode and its current settings",
    "the two cutting capabilities: parameter meanings, units and ranges",
    "per clip: duration, detected start and end of sound, loudness statistics, warnings",
    "which previews were rendered with the current settings",
]

DATA_NOT_SENT = [
    "video or audio of any kind",
    "file names and paths",
    "the project name",
]


class AdviceError(storage.ProjectError):
    """A recommendation problem the user can act on."""


class AdviceConflict(AdviceError):
    status_code = 409


# --- the job's input ----------------------------------------------------------


def _optional_text(raw: Any, label: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise AdviceError("The %s must be text." % label)
    text = raw.strip()
    if not text:
        return None
    if len(text) > plans.MAX_INSTRUCTION_LENGTH:
        raise AdviceError(
            "The %s is too long (up to %d characters)." % (label, plans.MAX_INSTRUCTION_LENGTH)
        )
    return text


def is_recommendation(revision: dict) -> bool:
    context = revision.get("context")
    return isinstance(context, dict) and context.get("kind") == CONTEXT_KIND


def build_job_input(project_id: str, raw: Any) -> dict:
    """Freeze the request, the configuration it is about and the provider."""
    if not isinstance(raw, dict):
        raise AdviceError("The recommendation request is invalid.")

    job_input = cutting.build_job_input(project_id, raw)
    project = storage.read_project(project_id)
    configuration = cutting.normalise_configuration(project, raw)

    revise_plan_id = raw.get("revise_plan_id")
    if revise_plan_id is not None:
        latest = plans.read_latest(project, plans.validate_plan_id(revise_plan_id))
        if not is_recommendation(latest):
            raise AdviceError("That plan is not a cutting recommendation.")

    feedback = _optional_text(raw.get("feedback"), "feedback")
    if revise_plan_id is not None and feedback is None:
        raise AdviceError("Say what to change: a revision needs your feedback.")

    status = llm.provider_status()

    job_input.update(
        {
            "request": plans.validate_instruction(raw.get("request")),
            "feedback": feedback,
            "revise_plan_id": revise_plan_id,
            # The settings of the selected mode, which is what a proposal for
            # the same mode starts from.
            "current_settings": (
                configuration["boundary_settings"]
                if configuration["mode"] == cutting.CUT_MODE_BOUNDARY
                else configuration["settings"]
            ),
            # Recorded for display and a faithful retry; the provider itself is
            # resolved from the environment when the job runs.
            "provider_id": status["provider"],
            "model": status["model"],
        }
    )
    return job_input


# --- what the provider is told ------------------------------------------------


def _clip_measurements(
    context: jobs.JobContext, job_input: dict
) -> list[dict]:
    """Numbers about each clip. No names, no paths, no samples of the media."""
    mode = cutting.job_mode(job_input)
    clips: list[dict] = []

    for index, source in enumerate(job_input["sources"]):
        context.raise_if_cancelled()
        entry: dict = {
            "clip": index + 1,
            "resource_id": source["source_id"],
            "duration_seconds": round(source["duration_seconds"] or 0.0, 3),
            "has_audio": bool(source.get("audio")),
        }

        if mode == cutting.CUT_MODE_BOUNDARY:
            try:
                boundary = cut_runner.resolve_boundary(
                    context.project_id,
                    source,
                    job_input["boundary_settings"],
                    cancelled=lambda: context.cancelled,
                )
            except processes.ProcessCancelled:
                raise jobs.JobCancelled() from None
            except storage.ProjectError as error:
                entry["measurement_error"] = error.message
                clips.append(entry)
                continue

            stats = boundary.get("stats") or {}
            entry.update(
                {
                    "detection_status": boundary["status"],
                    "first_sound_at_seconds": boundary["activity_start_seconds"],
                    "last_sound_ends_at_seconds": boundary["activity_end_seconds"],
                    "retained_start_seconds": boundary["start_seconds"],
                    "retained_end_seconds": boundary["end_seconds"],
                    "removed_before_seconds": boundary["removed_leading_seconds"],
                    "removed_after_seconds": boundary["removed_trailing_seconds"],
                    "start_set_by": boundary["start_origin"],
                    "end_set_by": boundary["end_origin"],
                    "peak_level": stats.get("peak"),
                    "background_level": stats.get("background_level"),
                    "activity_level": stats.get("activity_level"),
                    "warnings": [warning["code"] for warning in boundary["warnings"]],
                }
            )
        else:
            level = source.get("audio_level") or {}
            entry.update(
                {
                    "peak_level": level.get("peak_ratio"),
                    "peak_db": level.get("max_db"),
                    "mean_db": level.get("mean_db"),
                }
            )

        clips.append(entry)

    return clips


def _preview_summary(project_id: str, job_input: dict) -> list[dict]:
    """Which previews exist for exactly the configuration being discussed."""
    order = {source["source_id"]: index + 1 for index, source in enumerate(job_input["sources"])}
    by_id = {source["source_id"]: source for source in job_input["sources"]}
    previews: list[dict] = []
    seen: set[str] = set()

    for sample in cut_samples.list_samples(project_id):
        slot = cut_samples.slot(sample)
        source_ids = sample.get("source_ids") or []
        if slot in seen or any(source_id not in by_id for source_id in source_ids):
            continue
        seen.add(slot)
        key = cut_samples.sample_basis_key(
            job_input, [by_id[source_id] for source_id in source_ids]
        )
        if key != sample.get("basis_key"):
            continue
        previews.append(
            {
                "kind": sample.get("kind"),
                "clips": [order[source_id] for source_id in source_ids],
                "length_seconds": (sample.get("edited") or {}).get("duration_seconds"),
            }
        )

    return previews


def build_request(job_input: dict, clips: list[dict], previews: list[dict]) -> dict:
    """Exactly what is sent to a provider — and the mock's only input too."""
    mode = cutting.job_mode(job_input)
    return {
        "request": job_input["request"],
        "feedback": job_input.get("feedback"),
        "evidence": (
            "Measurements only. No audio or video was provided to you, and no "
            "file names: you have not heard or seen these clips."
        ),
        "workflow": (
            "Each clip is one successful take of a short section of a script; the "
            "clips are joined in order. Retakes and mistakes are handled by "
            "re-recording, not by cutting."
        ),
        "current": {"mode": mode, "settings": job_input["current_settings"]},
        "modes": {
            mode_id: {
                "capability_id": capabilities.CAPABILITY_FOR_CUT_MODE[mode_id],
                "description": definition["description"],
            }
            for mode_id, definition in cutting.CUT_MODES.items()
        },
        "capabilities": [
            capabilities.CAPABILITIES[capability_id]
            for capability_id in capabilities.CAPABILITY_FOR_CUT_MODE.values()
        ],
        "detection_notes": {
            "levels": "0-1 share of full scale, peak per 10 ms",
            "bridge_seconds": boundaries.BRIDGE_SECONDS,
            "meaning": (
                "first_sound_at / last_sound_ends_at are where loudness first and "
                "last stayed above the threshold; they are not word boundaries."
            ),
        },
        "clips": clips,
        "previews_rendered_with_current_settings": previews,
        "response_schema": llm.CUT_RESPONSE_SCHEMA,
    }


# --- validating the answer ----------------------------------------------------


def validate_proposal(raw: Any, current_mode: str, current_settings: dict) -> dict:
    """Check a provider's answer. The model proposes; this decides."""
    if not isinstance(raw, dict):
        raise AdviceError("The provider's response is not in the expected shape.")

    mode = raw.get("mode")
    if mode not in cutting.CUT_MODES:
        raise AdviceError(
            "The provider proposed an unknown cutting mode. Supported modes: %s."
            % ", ".join(cutting.CUT_MODES)
        )

    proposed = raw.get("settings")
    if proposed is None:
        proposed = {}
    if not isinstance(proposed, dict):
        raise AdviceError("The provider's proposed settings are not in the expected shape.")

    # A proposal for the selected mode changes what it names and keeps the
    # rest; one for the other mode starts from that mode's own defaults.
    base = dict(current_settings) if mode == current_mode else {}
    merged = {**base, **proposed}

    if mode == cutting.CUT_MODE_BOUNDARY:
        settings = boundaries.validate_settings(merged)
    else:
        settings = cutting.validate_settings(merged)

    explanation = raw.get("explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        raise AdviceError("The provider gave no explanation for its proposal.")
    explanation = explanation.strip()[: plans.MAX_SUMMARY_LENGTH]

    limitations: list[str] = []
    raw_limitations = raw.get("limitations")
    if isinstance(raw_limitations, list):
        for entry in raw_limitations[:MAX_LIMITATIONS]:
            if isinstance(entry, str) and entry.strip():
                limitations.append(entry.strip()[:MAX_LIMITATION_LENGTH])

    return {
        "mode": mode,
        "mode_changed": mode != current_mode,
        "settings": settings,
        "explanation": explanation,
        "limitations": limitations,
    }


# --- the job ------------------------------------------------------------------


def run_recommendation(context: jobs.JobContext) -> dict:
    job_input = context.input
    project = storage.read_project(context.project_id)
    current_mode = cutting.job_mode(job_input)

    context.progress("Measuring the clips…")
    clips = _clip_measurements(context, job_input)
    previews = _preview_summary(context.project_id, job_input)
    request = build_request(job_input, clips, previews)

    try:
        provider = llm.get_provider()
    except llm.ProviderError as error:
        raise jobs.JobFailed(error.message) from error

    context.raise_if_cancelled()
    context.progress(
        "Asking %s for cutting settings…"
        % ("the demo engine" if provider.is_mock else provider.label)
    )

    try:
        answer = provider.recommend_cut(request)
    except llm.ProviderError as error:
        raise jobs.JobFailed(error.message) from error

    context.raise_if_cancelled()
    context.progress("Checking the proposal against the supported settings…")

    if isinstance(answer, dict) and answer.get("supported") is False:
        explanation = answer.get("explanation")
        if not isinstance(explanation, str) or not explanation.strip():
            explanation = "The request cannot be met by the cutting settings."
        # A refusal is a successful job, as for any other plan: a real answer,
        # and no proposal invented to fill the gap.
        return {
            "supported": False,
            "explanation": explanation.strip(),
            "provider": provider.describe(),
        }

    try:
        proposal = validate_proposal(answer, current_mode, job_input["current_settings"])
    except storage.ProjectError as error:
        raise jobs.JobFailed(
            "The provider's proposal was rejected in validation: %s" % error.message
        ) from error

    summary = proposal["explanation"]
    if proposal["mode_changed"]:
        summary = (
            "This proposal changes the cutting mode from %s to %s. %s"
            % (
                cutting.CUT_MODES[current_mode]["label"],
                cutting.CUT_MODES[proposal["mode"]]["label"],
                summary,
            )
        )[: plans.MAX_SUMMARY_LENGTH]

    try:
        revision = plans.create_revision(
            project,
            summary=summary,
            actions=[
                {
                    "id": ACTION_ID,
                    "capability_id": capabilities.CAPABILITY_FOR_CUT_MODE[proposal["mode"]],
                    "resource_ids": job_input["source_ids"],
                    "parameters": {
                        **proposal["settings"],
                        "output_mode": job_input["output_mode"],
                    },
                    "note": " ".join(proposal["limitations"])[: plans.MAX_NOTE_LENGTH],
                }
            ],
            origin=plans.ORIGIN_AI,
            provider=provider.describe(),
            instruction=job_input["request"],
            plan_id=job_input.get("revise_plan_id"),
            context={
                "kind": CONTEXT_KIND,
                "mode": proposal["mode"],
                "requested_in_mode": current_mode,
                "mode_changed": proposal["mode_changed"],
                # What the measurements were taken under. A proposal is only
                # applicable while the configuration still matches this.
                "based_on_fingerprint": job_input["config_fingerprint"],
                "based_on_settings_revision": job_input.get("settings_revision"),
                "based_on_settings": job_input["current_settings"],
                "feedback": job_input.get("feedback"),
                "limitations": proposal["limitations"],
                "evidence": "measurements only; no media was sent",
                "clip_count": len(clips),
                "preview_count": len(previews),
            },
        )
    except storage.ProjectError as error:
        raise jobs.JobFailed(
            "The provider's proposal was rejected in validation: %s" % error.message
        ) from error

    return {
        "supported": True,
        "plan_id": revision["plan_id"],
        "revision": revision["revision"],
        "mode": proposal["mode"],
        "mode_changed": proposal["mode_changed"],
        "settings": proposal["settings"],
        "limitations": proposal["limitations"],
        "summary": revision["summary"],
        "provider": provider.describe(),
    }


# --- reading and applying -----------------------------------------------------


def describe(revision: dict, current_fingerprint: str | None, applied: dict | None) -> dict:
    """A recommendation as the cutting screen shows it, with its staleness."""
    context = revision.get("context") or {}
    action = (revision.get("actions") or [{}])[0]
    parameters = dict(action.get("parameters") or {})
    parameters.pop("output_mode", None)

    is_applied = bool(
        applied
        and applied.get("plan_id") == revision["plan_id"]
        and applied.get("revision") == revision["revision"]
    )

    reason: str | None = None
    if revision.get("outdated"):
        reason = revision.get("outdated_reason")
    elif not is_applied and current_fingerprint != context.get("based_on_fingerprint"):
        reason = (
            "The settings, a manual boundary or the selected clips changed since "
            "this recommendation was made. Ask for a new one."
        )

    return {
        "plan_id": revision["plan_id"],
        "revision": revision["revision"],
        "latest_revision": revision.get("latest_revision"),
        "is_latest": revision.get("is_latest"),
        "revised_at": revision.get("revised_at"),
        "origin": revision.get("origin"),
        "provider": revision.get("provider"),
        "request": revision.get("instruction"),
        "feedback": context.get("feedback"),
        "mode": context.get("mode"),
        "requested_in_mode": context.get("requested_in_mode"),
        "mode_changed": bool(context.get("mode_changed")),
        "capability_id": action.get("capability_id"),
        "settings": parameters,
        "explanation": revision.get("summary"),
        "limitations": context.get("limitations") or [],
        "based_on_settings": context.get("based_on_settings"),
        "based_on_settings_revision": context.get("based_on_settings_revision"),
        "approved": revision.get("approved"),
        "applied": is_applied,
        "stale": reason is not None,
        "stale_reason": reason,
    }


def latest_recommendation(project: dict) -> dict | None:
    """The most recently revised cutting recommendation of a project, if any."""
    for entry in plans.list_plans(project):
        try:
            revision = plans.read_latest(project, entry["plan_id"])
        except storage.ProjectError:
            continue
        if is_recommendation(revision):
            return revision
    return None


def apply(project_id: str, plan_id: Any, revision_number: Any, raw: Any) -> dict:
    """Approve a recommendation and put its settings into the cutting form.

    `raw` is the form as it stands now. The recommendation is refused if that
    is no longer the configuration it was made for, and a proposal that
    changes the cutting mode needs `confirm_mode_change`.
    """
    if not isinstance(raw, dict):
        raise AdviceError("The request is invalid.")

    project = storage.read_project(project_id)
    revision = plans.read_revision(project, plan_id, revision_number)

    if not is_recommendation(revision):
        raise AdviceError("That plan is not a cutting recommendation.")
    if not revision["is_latest"]:
        raise AdviceConflict("A newer revision of this recommendation exists.")
    if revision["outdated"]:
        raise AdviceConflict(revision["outdated_reason"] or "The recommendation is out of date.")

    configuration = cutting.normalise_configuration(project, raw)
    by_id = {source["id"]: source for source in project["sources"]}
    entries = []
    for source_id in configuration["source_ids"]:
        try:
            digest = boundaries.cached_fingerprint(by_id[source_id]["path"])["digest"]
        except Exception as error:  # noqa: BLE001 - any unreadable file is the same refusal
            raise AdviceConflict(
                "A selected file can no longer be read, so the recommendation "
                "cannot be checked against it."
            ) from error
        entries.append((source_id, digest))

    context = revision["context"]
    if cutting.basis_key(configuration, entries) != context.get("based_on_fingerprint"):
        raise AdviceConflict(
            "The settings, a manual boundary or the selected clips changed since "
            "this recommendation was made, so it no longer describes this "
            "configuration. Ask for a new recommendation."
        )

    action = revision["actions"][0]
    mode = capabilities.CUT_MODE_FOR_CAPABILITY.get(action["capability_id"])
    if mode is None:
        raise AdviceError("The recommendation names a capability that does not cut.")

    if mode != configuration["mode"] and raw.get("confirm_mode_change") is not True:
        raise AdviceConflict(
            "This recommendation switches the cutting mode to %s. Confirm the "
            "mode change to apply it." % cutting.CUT_MODES[mode]["label"]
        )

    # The explicit human step, through the same gate as every other AI plan.
    plans.approve_revision(project, revision["plan_id"], revision["revision"])

    parameters = dict(action["parameters"])
    output_mode = parameters.pop("output_mode", configuration["output_mode"])
    updated = dict(configuration)
    updated["mode"] = mode
    updated["output_mode"] = output_mode
    updated["boundary_settings" if mode == cutting.CUT_MODE_BOUNDARY else "settings"] = parameters
    updated["applied_plan"] = {
        "plan_id": revision["plan_id"],
        "revision": revision["revision"],
    }

    return cutting.save_settings(project_id, updated)
