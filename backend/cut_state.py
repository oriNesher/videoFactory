"""The cutting screen's live state: boundaries, previews and what is stale.

One cheap, read-only question, asked whenever the form changes: *given the
configuration as it stands in the browser right now*, where would each clip be
cut, which previews still show that, and is the AI recommendation still about
this configuration?

Nothing here decodes audio or renders video. Boundaries come from the analysis
cache (written by the analysis, sample and render jobs); a clip that has not
been analysed with the current detection settings is reported as such, and the
interface offers the job that analyses it. Staleness is decided here, on the
server, by comparing basis keys — never by the browser guessing.
"""

from typing import Any

from . import (
    boundaries,
    capabilities,
    cut_advice,
    cut_samples,
    cutting,
    jobs,
    media,
    plans,
    processes,
    storage,
)


def clean_applied_plan(project: dict, configuration: dict) -> dict:
    """Drop an applied-plan reference the settings no longer match.

    The form says "these settings came from approved proposal N". That is only
    true while they are still that proposal's settings; after a manual edit
    the label would be a lie on every sample and run made from then on.
    """
    reference = configuration.get("applied_plan")
    if not reference:
        return configuration

    cleaned = dict(configuration)
    try:
        revision = plans.read_revision(project, reference["plan_id"], reference["revision"])
    except storage.ProjectError:
        cleaned["applied_plan"] = None
        return cleaned

    action = (revision.get("actions") or [{}])[0]
    parameters = dict(action.get("parameters") or {})
    parameters.pop("output_mode", None)
    mode = capabilities.CUT_MODE_FOR_CAPABILITY.get(action.get("capability_id"))
    active = (
        configuration["boundary_settings"]
        if configuration["mode"] == cutting.CUT_MODE_BOUNDARY
        else configuration["settings"]
    )

    if (
        not cut_advice.is_recommendation(revision)
        or not revision.get("approved")
        or mode != configuration["mode"]
        or parameters != active
    ):
        cleaned["applied_plan"] = None
    return cleaned


def normalise(project: dict, raw: Any, *, allow_empty: bool = True) -> dict:
    """A submitted form, validated, with its applied-plan label checked."""
    return clean_applied_plan(
        project, cutting.normalise_configuration(project, raw, allow_empty=allow_empty)
    )


def describe(project_id: str, raw: Any) -> dict:
    project = storage.read_project(project_id)
    configuration = normalise(project, raw)
    boundary_mode = configuration["mode"] == cutting.CUT_MODE_BOUNDARY
    settings = configuration["boundary_settings"]

    described = {
        source["id"]: source for source in storage.describe_sources(project["sources"])
    }

    clips: list[dict] = []
    digests: dict[str, str | None] = {}
    entries: list[tuple[str, str]] = []
    readable = True

    for index, source_id in enumerate(configuration["source_ids"]):
        source = described[source_id]
        override = configuration["overrides"].get(source_id)
        clip: dict = {
            "source_id": source_id,
            "order": index + 1,
            "filename": source["filename"],
            "available": source["exists"],
            "duration_seconds": source["duration_seconds"],
            "override": override,
            "analysed": False,
            "boundary": None,
            "error": None,
        }

        digest: str | None = None
        if source["exists"]:
            try:
                digest = boundaries.cached_fingerprint(source["path"])["digest"]
            except media.MediaError as error:
                clip["error"] = error.message
        else:
            clip["error"] = "The file is not where it was."

        digests[source_id] = digest
        if digest is None:
            readable = False
        else:
            entries.append((source_id, digest))

        if boundary_mode and digest is not None:
            detection = boundaries.read_analysis(project_id, digest, settings)
            if detection is not None:
                clip["analysed"] = True
                clip["duration_seconds"] = detection["duration_seconds"]
                try:
                    boundaries.check_override_fits(
                        override, detection["duration_seconds"], source["filename"]
                    )
                    clip["boundary"] = boundaries.resolve(
                        detection, settings, override, detection.get("frame_rate")
                    )
                except storage.ProjectError as error:
                    # An invalid manual boundary is reported on its clip; it
                    # must not take the rest of the screen down with it.
                    clip["error"] = error.message
            elif override and source["duration_seconds"]:
                try:
                    boundaries.check_override_fits(
                        override, source["duration_seconds"], source["filename"]
                    )
                except storage.ProjectError as error:
                    clip["error"] = error.message

        clips.append(clip)

    fingerprint = cutting.basis_key(configuration, entries) if readable else None

    recommendation = None
    latest = cut_advice.latest_recommendation(project)
    if latest is not None:
        recommendation = cut_advice.describe(latest, fingerprint, configuration["applied_plan"])

    retained = [clip["boundary"]["retained_seconds"] for clip in clips if clip["boundary"]]
    originals = [clip["duration_seconds"] or 0.0 for clip in clips]

    return {
        "project_id": project_id,
        "mode": configuration["mode"],
        "config_fingerprint": fingerprint,
        # Null while the form holds changes that have not been saved.
        "settings_revision": cutting.saved_revision(project, configuration),
        "applied_plan": configuration["applied_plan"],
        "clips": clips,
        "analysis_needed": boundary_mode
        and any(clip["available"] and not clip["analysed"] for clip in clips),
        "needs_review_count": sum(
            1 for clip in clips if clip["boundary"] and clip["boundary"]["needs_review"]
        ),
        "invalid_count": sum(1 for clip in clips if clip["error"]),
        "original_seconds": round(sum(originals), 3),
        "retained_seconds": (
            round(sum(retained), 3) if retained and len(retained) == len(clips) else None
        ),
        "samples": cut_samples.describe_current(project_id, configuration, digests),
        "recommendation": recommendation,
    }


# --- the analysis job ---------------------------------------------------------


def build_analysis_input(project_id: str, raw: Any) -> dict:
    job_input = cutting.build_job_input(project_id, raw)
    if job_input["mode"] != cutting.CUT_MODE_BOUNDARY:
        raise cutting.CuttingError(
            "Boundary detection belongs to boundary-only mode. Full-clip silence "
            "removal is analysed by Auto-Editor when the cut runs."
        )
    job_input["force"] = isinstance(raw, dict) and raw.get("force") is True
    return job_input


def run_analysis(context: jobs.JobContext) -> dict:
    """Find the boundaries of every selected clip. Decodes audio; renders nothing."""
    job_input = context.input
    settings = job_input["boundary_settings"]
    sources = job_input["sources"]
    results: list[dict] = []
    reused = 0
    decoded = 0.0
    seconds = 0.0

    for index, source in enumerate(sources):
        context.raise_if_cancelled()
        context.progress(
            "Finding the boundaries of clip %d of %d: %s"
            % (index + 1, len(sources), source["filename"]),
            percent=100.0 * index / len(sources),
        )

        try:
            detection, was_reused = boundaries.analyse_source(
                context.project_id,
                source,
                settings,
                cancelled=lambda: context.cancelled,
                force=bool(job_input.get("force")),
            )
            resolved = boundaries.resolve(
                detection,
                settings,
                source.get("override"),
                (source.get("video") or {}).get("frame_rate"),
            )
        except processes.ProcessCancelled:
            raise jobs.JobCancelled() from None
        except storage.ProjectError as error:
            raise jobs.JobFailed('"%s": %s' % (source["filename"], error.message)) from error

        if was_reused:
            reused += 1
        else:
            decoded += detection.get("decoded_seconds") or 0.0
            seconds += detection.get("analysis_seconds") or 0.0

        results.append(
            {
                "source_id": source["source_id"],
                "filename": source["filename"],
                "duration_seconds": source["duration_seconds"],
                "status": resolved["status"],
                "start_seconds": resolved["start_seconds"],
                "end_seconds": resolved["end_seconds"],
                "needs_review": resolved["needs_review"],
                "reused": was_reused,
                "full_scan": detection.get("full_scan"),
                "expansions": detection.get("expansions"),
                "decoded_seconds": detection.get("decoded_seconds"),
                "analysis_seconds": detection.get("analysis_seconds"),
            }
        )

    review = sum(1 for result in results if result["needs_review"])
    total = sum(result["duration_seconds"] or 0.0 for result in results)

    return {
        "clips": results,
        "clip_count": len(results),
        "reused_count": reused,
        "needs_review_count": review,
        "source_seconds": round(total, 3),
        # Audio actually decoded, against the audio there was: the measure of
        # what analysing only the ends saved. It says nothing about rendering.
        "decoded_seconds": round(decoded, 3),
        "analysis_seconds": round(seconds, 3),
        "summary": "Found the boundaries of %d clip%s in %.2f s (%d already analysed)%s."
        % (
            len(results),
            "" if len(results) == 1 else "s",
            seconds,
            reused,
            "; %d need a look" % review if review else "",
        ),
    }

