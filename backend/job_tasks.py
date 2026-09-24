"""The job types this milestone can actually run.

Three of them, and only one touches the outside world:

- `tool_check`      — the milestone-0A processing-tool check, run in the queue.
- `plan_generation` — ask the configured provider for a plan proposal.
- `plan_execution`  — run an approved plan. Today that can only be a diagnostic
                      tool-check plan; there is no video-editing execution yet.

Every handler cooperates with cancellation by calling `raise_if_cancelled`
between steps, and reports a progress percentage only where there is a real
denominator to divide by.
"""

from typing import Any

from . import capabilities, jobs, llm, plans, resources, storage, tools

TOOL_CHECK_JOB = "tool_check"
PLAN_GENERATION_JOB = "plan_generation"
PLAN_EXECUTION_JOB = "plan_execution"


# --- tool check -------------------------------------------------------------


def validate_tool_check_input(project_id: str, raw: Any) -> dict:
    """Same parameter rules as the capability, so both paths agree."""
    capability = capabilities.CAPABILITIES[capabilities.TOOL_CHECK]
    parameters = capabilities.validate_parameters(capability, raw or {}, "בדיקת כלים")
    return {"tools": parameters["tools"]}


def _check_tools(context: jobs.JobContext | None, names: list[str]) -> dict:
    """Check each tool in turn. Progress is measurable here: n of N tools."""
    results = []
    total = len(names)

    for index, name in enumerate(names):
        if context is not None:
            context.raise_if_cancelled()
            context.progress(
                "בודק %s (%d מתוך %d)…" % (capabilities.tool_label(name), index + 1, total),
                percent=100.0 * index / total,
            )

        results.append(tools.check_tool(name))

    available = sum(1 for result in results if result["available"] and result["working"])

    return {
        "tools": results,
        "available_count": available,
        "checked_count": total,
        "summary": "%d מתוך %d כלים זמינים ופועלים." % (available, total),
    }


def run_tool_check(context: jobs.JobContext) -> dict:
    names = context.input.get("tools") or list(tools.TOOL_NAMES)
    return _check_tools(context, names)


# --- plan generation --------------------------------------------------------


def validate_plan_generation_input(project_id: str, raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise plans.PlanError("בקשת יצירת התוכנית אינה תקינה.")

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
    context.progress("מכין את קטלוג היכולות והמשאבים…")

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
        "פונה ל%s…" % ("מנוע ההדגמה" if provider.is_mock else provider.label)
    )

    try:
        proposal = provider.propose(request)
    except llm.ProviderError as error:
        raise jobs.JobFailed(error.message) from error

    context.raise_if_cancelled()
    context.progress("בודק את ההצעה מול קטלוג היכולות…")

    if not isinstance(proposal, dict):
        raise jobs.JobFailed("תשובת הספק אינה במבנה הצפוי.")

    if not proposal.get("supported", False):
        explanation = proposal.get("explanation")
        if not isinstance(explanation, str) or not explanation.strip():
            explanation = (
                "הבקשה אינה נתמכת על ידי היכולות שממומשות כרגע, ולא ניתן הסבר מפורט."
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
            "ההצעה של הספק נדחתה באימות: %s" % error.message
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
        raise plans.PlanError("בקשת ההרצה אינה תקינה.")

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


EXECUTORS = {capabilities.TOOL_CHECK: _execute_tool_check_action}


def run_plan_execution(context: jobs.JobContext) -> dict:
    project = storage.read_project(context.project_id)
    plan_id = context.input["plan_id"]
    revision = context.input["revision"]

    context.progress("בודק שהתוכנית עדיין ניתנת להרצה…")

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
            "מריץ פעולה %d מתוך %d…" % (index + 1, len(actions)),
            percent=100.0 * index / len(actions),
        )

        executor = EXECUTORS.get(action["capability_id"])
        if executor is None:
            raise jobs.JobFailed(
                'אין מימוש הרצה ליכולת "%s".' % action["capability_id"]
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
        "summary": "בוצעו %d פעולות." % len(outcomes),
    }


# --- registration -----------------------------------------------------------

jobs.register_job_type(
    TOOL_CHECK_JOB,
    "בדיקת כלי עיבוד",
    run_tool_check,
    validate_tool_check_input,
)

jobs.register_job_type(
    PLAN_GENERATION_JOB,
    "יצירת תוכנית עריכה",
    run_plan_generation,
    validate_plan_generation_input,
)

jobs.register_job_type(
    PLAN_EXECUTION_JOB,
    "הרצת תוכנית מאושרת",
    run_plan_execution,
    validate_plan_execution_input,
)
