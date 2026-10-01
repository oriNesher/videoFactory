"""The job types this milestone can actually run.

Eight of them:

- `tool_check`      — the milestone-0A processing-tool check, run in the queue.
- `cut_media`       — trim the selected project sources and, on request, join
                      them with FFmpeg. Boundary-only trimming (FFmpeg) or
                      full-clip silence removal (Auto-Editor), by mode.
- `cut_analysis`    — milestone 1B: find each clip's boundaries. Decodes audio;
                      renders nothing.
- `cut_sample`      — milestone 1B: render short opening, ending and join
                      previews at the boundaries the final run would use.
- `cut_recommendation` — milestone 1B: ask the configured provider for cutting
                      settings, stored as a plan awaiting approval.
- `subtitles`       — transcribe a cut run's merged video (merging its clips
                      first if the run has none yet) or single clips with
                      Whisper, and write an SRT beside each.
- `plan_generation` — ask the configured provider for a plan proposal.
- `plan_execution`  — run an approved plan. It can now drive real editing as
                      well as diagnostics, through the same code path the
                      manual button uses.

Every handler cooperates with cancellation by calling `raise_if_cancelled`
between steps, and reports a progress percentage only where there is a real
denominator to divide by.
"""

from typing import Any

from . import (
    capabilities,
    cut_advice,
    cut_runner,
    cut_samples,
    cut_state,
    cutting,
    jobs,
    llm,
    plans,
    resources,
    storage,
    subtitle_runner,
    subtitles,
    tools,
)

TOOL_CHECK_JOB = "tool_check"
CUT_MEDIA_JOB = "cut_media"
SUBTITLES_JOB = "subtitles"
CUT_ANALYSIS_JOB = "cut_analysis"
CUT_SAMPLE_JOB = "cut_sample"
CUT_RECOMMENDATION_JOB = "cut_recommendation"

# The jobs behind the cutting screen's own buttons. It watches these so its
# boundaries, previews and recommendation refresh the moment one finishes.
CUTTING_JOB_TYPES = (CUT_ANALYSIS_JOB, CUT_SAMPLE_JOB, CUT_RECOMMENDATION_JOB)

# Jobs that work on an existing cut run, named by `input.run_id`. The run
# listing reports which of them are active so the interface can show it.
RUN_JOB_TYPES = (SUBTITLES_JOB,)
PLAN_GENERATION_JOB = "plan_generation"
PLAN_EXECUTION_JOB = "plan_execution"


# --- tool check -------------------------------------------------------------


def validate_tool_check_input(project_id: str, raw: Any) -> dict:
    """Same parameter rules as the capability, so both paths agree."""
    capability = capabilities.CAPABILITIES[capabilities.TOOL_CHECK]
    parameters = capabilities.validate_parameters(capability, raw or {}, "Tool check")
    return {"tools": parameters["tools"]}


def _check_tools(context: jobs.JobContext | None, names: list[str]) -> dict:
    """Check each tool in turn. Progress is measurable here: n of N tools."""
    results = []
    total = len(names)

    for index, name in enumerate(names):
        if context is not None:
            context.raise_if_cancelled()
            context.progress(
                "Checking %s (%d of %d)…" % (capabilities.tool_label(name), index + 1, total),
                percent=100.0 * index / total,
            )

        results.append(tools.check_tool(name))

    available = sum(1 for result in results if result["available"] and result["working"])

    return {
        "tools": results,
        "available_count": available,
        "checked_count": total,
        "summary": "%d of %d tools are available and working." % (available, total),
    }


def run_tool_check(context: jobs.JobContext) -> dict:
    names = context.input.get("tools") or list(tools.TOOL_NAMES)
    return _check_tools(context, names)


# --- cutting ----------------------------------------------------------------


def validate_cut_media_input(project_id: str, raw: Any) -> dict:
    """Resolve and freeze the request before it is allowed into the queue.

    Everything expensive to get wrong is checked here, while the user is still
    looking at the form: the ids exist in the project, the files are on disk,
    FFprobe can read them, and each one carries the audio this mode needs.
    """
    return cutting.build_job_input(project_id, raw)


def run_cut_media(context: jobs.JobContext) -> dict:
    return cut_runner.run_cutting(context)


def validate_cut_analysis_input(project_id: str, raw: Any) -> dict:
    return cut_state.build_analysis_input(project_id, raw)


def validate_cut_sample_input(project_id: str, raw: Any) -> dict:
    return cut_samples.build_job_input(project_id, raw)


def validate_cut_recommendation_input(project_id: str, raw: Any) -> dict:
    return cut_advice.build_job_input(project_id, raw)



# --- subtitles --------------------------------------------------------------


def validate_subtitles_input(project_id: str, raw: Any) -> dict:
    """Resolve the chosen videos inside their run and freeze the settings."""
    job_input = subtitles.build_job_input(project_id, raw)
    # One subtitle job per run at a time: two could both start merging the
    # same clips into the same file.
    for record in jobs.list_jobs(project_id):
        if (
            record["type"] == SUBTITLES_JOB
            and record["status"] in jobs.ACTIVE_STATUSES
            and record["input"].get("run_id") == job_input["run_id"]
        ):
            raise subtitles.SubtitleError(
                "Subtitles are already being created for this run.", status_code=409
            )
    return job_input


def run_subtitles(context: jobs.JobContext) -> dict:
    return subtitle_runner.run_subtitles(context)


# --- plan generation --------------------------------------------------------


def validate_plan_generation_input(project_id: str, raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise plans.PlanError("The plan request is invalid.")

    instruction = plans.validate_instruction(raw.get("instruction"))
    status = llm.provider_status()

    return {
        # Recorded for display and for a faithful retry. The provider itself is
        # resolved from the environment when the job runs, so a retry never uses
        # a stale key or a provider the user has since turned off.
        "instruction": instruction,
        "provider_id": status["provider"],
        "model": status["model"],
    }


def run_plan_generation(context: jobs.JobContext) -> dict:
    project = storage.read_project(context.project_id)

    context.raise_if_cancelled()
    context.progress("Preparing the capability and resource catalogs…")

    capability_catalog = capabilities.catalog()
    resource_catalog = resources.build_catalog(project)
    instruction = context.input["instruction"]
    request = llm.build_request(instruction, capability_catalog, resource_catalog)

    try:
        provider = llm.get_provider()
    except llm.ProviderError as error:
        raise jobs.JobFailed(error.message) from error

    context.raise_if_cancelled()
    context.progress(
        "Asking %s…" % ("the demo engine" if provider.is_mock else provider.label)
    )

    try:
        proposal = provider.propose(request)
    except llm.ProviderError as error:
        raise jobs.JobFailed(error.message) from error

    context.raise_if_cancelled()
    context.progress("Checking the proposal against the capability catalog…")

    if not isinstance(proposal, dict):
        raise jobs.JobFailed("The provider's response is not in the expected shape.")

    if not proposal.get("supported", False):
        explanation = proposal.get("explanation")
        if not isinstance(explanation, str) or not explanation.strip():
            explanation = (
                "The request is not supported by the capabilities implemented so far, and no detailed explanation was given."
            )
        # A refusal is a successful job: the user gets a real answer, and no
        # plan is invented to fill the gap.
        return {
            "supported": False,
            "explanation": explanation.strip(),
            "provider": provider.describe(),
        }

    try:
        revision = plans.create_revision(
            project,
            summary=proposal.get("summary"),
            actions=proposal.get("actions"),
            origin=plans.ORIGIN_AI,
            provider=provider.describe(),
            instruction=instruction,
        )
    except storage.ProjectError as error:
        raise jobs.JobFailed(
            "The provider's proposal was rejected in validation: %s" % error.message
        ) from error

    return {
        "supported": True,
        "plan_id": revision["plan_id"],
        "revision": revision["revision"],
        "action_count": len(revision["actions"]),
        "summary": revision["summary"],
        "provider": provider.describe(),
    }


# --- plan execution ---------------------------------------------------------


def validate_plan_execution_input(project_id: str, raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise plans.PlanError("The run request is invalid.")

    project = storage.read_project(project_id)
    plan_id = plans.validate_plan_id(raw.get("plan_id"))
    revision = plans.validate_revision_number(raw.get("revision"))

    # Reject an unapproved, outdated or unsupported plan immediately, rather
    # than queueing a job that is bound to fail.
    plans.assert_executable(project, plan_id, revision)

    return {"plan_id": plan_id, "revision": revision}


def _execute_tool_check_action(context: jobs.JobContext, action: dict) -> dict:
    names = action["parameters"].get("tools") or list(tools.TOOL_NAMES)
    return _check_tools(context, names)


def _execute_cut_action(context: jobs.JobContext, action: dict) -> dict:
    """Run a plan's cutting action through the same pipeline as the button.

    The action names resource ids and parameters; the snapshot is built here,
    at execution time, by the same validator the manual path uses. A plan never
    carries a path, a command or an output location.

    The capability decides the mode — `edit.cut_silence` is full-clip cutting,
    `edit.trim_boundaries` is boundary-only — so no parameter can turn one
    into the other. A boundary-only plan applies the manual start/end
    overrides saved in the cutting screen: they are the user's own corrections
    to specific clips, and a plan that silently dropped them would undo them.
    """
    parameters = dict(action["parameters"])
    output_mode = parameters.pop("output_mode", cutting.DEFAULT_OUTPUT_MODE)
    mode = capabilities.CUT_MODE_FOR_CAPABILITY[action["capability_id"]]

    request: dict = {
        "mode": mode,
        "source_ids": action["resource_ids"],
        "output_mode": output_mode,
    }

    try:
        if mode == cutting.CUT_MODE_BOUNDARY:
            saved = cutting.read_settings(storage.read_project(context.project_id))
            request["boundary_settings"] = parameters
            request["overrides"] = saved["overrides"]
        else:
            request["settings"] = parameters

        job_input = cutting.build_job_input(context.project_id, request)
    except storage.ProjectError as error:
        raise jobs.JobFailed(error.message) from error

    job_input["plan"] = {
        "plan_id": context.input.get("plan_id"),
        "revision": context.input.get("revision"),
    }
    return cut_runner.run_cutting(context, job_input)


EXECUTORS = {
    capabilities.TOOL_CHECK: _execute_tool_check_action,
    capabilities.CUT_SILENCE: _execute_cut_action,
    capabilities.TRIM_BOUNDARIES: _execute_cut_action,
}


def run_plan_execution(context: jobs.JobContext) -> dict:
    project = storage.read_project(context.project_id)
    plan_id = context.input["plan_id"]
    revision = context.input["revision"]

    context.progress("Checking that the plan is still runnable…")

    # Re-checked here, not only at submission: the project or the plan may have
    # changed while the job waited in the queue.
    try:
        described = plans.assert_executable(project, plan_id, revision)
    except storage.ProjectError as error:
        raise jobs.JobFailed(error.message) from error

    actions = described["actions"]
    outcomes = []

    for index, action in enumerate(actions):
        context.raise_if_cancelled()
        context.progress(
            "Running action %d of %d…" % (index + 1, len(actions)),
            percent=100.0 * index / len(actions),
        )

        executor = EXECUTORS.get(action["capability_id"])
        if executor is None:
            raise jobs.JobFailed(
                'Capability "%s" has no run implementation.' % action["capability_id"]
            )

        outcomes.append(
            {
                "action_id": action["id"],
                "capability_id": action["capability_id"],
                "status": "succeeded",
                "output": executor(context, action),
            }
        )

    return {
        "plan_id": plan_id,
        "revision": revision,
        "action_results": outcomes,
        "summary": "%d actions completed." % len(outcomes),
    }


# --- registration -----------------------------------------------------------

jobs.register_job_type(
    TOOL_CHECK_JOB,
    "Processing tool check",
    run_tool_check,
    validate_tool_check_input,
)

jobs.register_job_type(
    CUT_MEDIA_JOB,
    "Silence cutting",
    run_cut_media,
    validate_cut_media_input,
)

jobs.register_job_type(
    CUT_ANALYSIS_JOB,
    "Boundary detection",
    cut_state.run_analysis,
    validate_cut_analysis_input,
)

jobs.register_job_type(
    CUT_SAMPLE_JOB,
    "Cut previews",
    cut_samples.run_samples,
    validate_cut_sample_input,
)

jobs.register_job_type(
    CUT_RECOMMENDATION_JOB,
    "Cutting recommendation (AI)",
    cut_advice.run_recommendation,
    validate_cut_recommendation_input,
)

jobs.register_job_type(
    SUBTITLES_JOB,
    "Subtitles (Whisper)",
    run_subtitles,
    validate_subtitles_input,
)

jobs.register_job_type(
    PLAN_GENERATION_JOB,
    "Generate editing plan",
    run_plan_generation,
    validate_plan_generation_input,
)

jobs.register_job_type(
    PLAN_EXECUTION_JOB,
    "Run approved plan",
    run_plan_execution,
    validate_plan_execution_input,
)
