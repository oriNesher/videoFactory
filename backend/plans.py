"""Editing plans: a versioned, validated document an AI proposes and a user owns.

A plan is data, never code. It says *which registered capability* to run, *which
project resource ids* it applies to and *which validated parameters* it uses.
There is no action type that can carry a shell command, a Python snippet or any
other executable expression, and the backend — not the model — decides whether a
plan may be saved or run.

Layout, following the same one-JSON-document-per-thing discipline as 0A:

    <workspace>/projects/<project_id>/plans/<plan_id>/rev-0001.json
    <workspace>/projects/<project_id>/plans/<plan_id>/rev-0001.approval.json

Revision files are written with an exclusive create and never rewritten, so
history cannot be lost. Approval is therefore a separate small file: approving
does not touch the revision it approves, and editing a plan writes a *new*
revision which nobody has approved yet.

`approved` and `outdated` are the two states that gate execution. `outdated` is
never stored — it is recomputed by comparing the revision's recorded input
fingerprint with the project's current one, so a plan cannot claim to be current
after the project changed underneath it.
"""

import json
import os
import re
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import capabilities, resources, storage

PLAN_SCHEMA_VERSION = 1

PLANS_DIRECTORY = "plans"

MAX_ACTIONS = 25
MAX_SUMMARY_LENGTH = 1500
MAX_NOTE_LENGTH = 600
MAX_INSTRUCTION_LENGTH = 2000
MAX_ACTION_ID_LENGTH = 64
MAX_REVISIONS = 999

ORIGIN_AI = "ai"
ORIGIN_USER = "user"

STATUS_PROPOSED = "proposed"
STATUS_APPROVED = "approved"

_PLAN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_ACTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]+$")
_REVISION_FILE_PATTERN = re.compile(r"^rev-(\d{4})$")


class PlanError(storage.ProjectError):
    """A plan problem the user can act on."""


class PlanNotFound(PlanError):
    status_code = 404


def _now() -> str:
    # Microsecond precision: job and revision ordering must be unambiguous
    # even when two are created in the same second.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# --- paths ------------------------------------------------------------------


def plans_directory(project_id: str) -> Path:
    return storage.project_directory(project_id) / PLANS_DIRECTORY


def plan_directory(project_id: str, plan_id: str) -> Path:
    return plans_directory(project_id) / plan_id


def revision_name(revision: int) -> str:
    return "rev-%04d" % revision


def revision_file(project_id: str, plan_id: str, revision: int) -> Path:
    return plan_directory(project_id, plan_id) / ("%s.json" % revision_name(revision))


def approval_file(project_id: str, plan_id: str, revision: int) -> Path:
    return plan_directory(project_id, plan_id) / (
        "%s.approval.json" % revision_name(revision)
    )


def validate_plan_id(raw_id: Any) -> str:
    if not isinstance(raw_id, str) or not _PLAN_ID_PATTERN.match(raw_id):
        raise PlanNotFound("מזהה תוכנית לא חוקי.")
    return raw_id


def validate_revision_number(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1 or raw > MAX_REVISIONS:
        raise PlanNotFound("מספר גרסה לא חוקי.")
    return raw


# --- validation -------------------------------------------------------------


def validate_instruction(raw: Any) -> str:
    if not isinstance(raw, str):
        raise PlanError("ההנחיה חייבת להיות טקסט.")

    text = unicodedata.normalize("NFC", raw).strip()
    if not text:
        raise PlanError("יש לכתוב מה רוצים שהתוכנית תעשה.")
    if len(text) > MAX_INSTRUCTION_LENGTH:
        raise PlanError("ההנחיה ארוכה מדי (עד %d תווים)." % MAX_INSTRUCTION_LENGTH)

    return text


def validate_summary(raw: Any) -> str:
    if not isinstance(raw, str):
        raise PlanError("תקציר התוכנית חייב להיות טקסט.")

    text = unicodedata.normalize("NFC", raw).strip()
    if not text:
        raise PlanError("לתוכנית חייב להיות תקציר קריא.")
    if len(text) > MAX_SUMMARY_LENGTH:
        raise PlanError("תקציר התוכנית ארוך מדי (עד %d תווים)." % MAX_SUMMARY_LENGTH)

    return text


def _validate_note(raw: Any, action_label: str) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise PlanError("%s: ההסבר לפעולה חייב להיות טקסט." % action_label)

    text = raw.strip()
    if len(text) > MAX_NOTE_LENGTH:
        raise PlanError(
            "%s: ההסבר לפעולה ארוך מדי (עד %d תווים)." % (action_label, MAX_NOTE_LENGTH)
        )
    return text


def _validate_resource_ids(
    capability: dict, raw: Any, catalog: dict, action_label: str
) -> list[str]:
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise PlanError("%s: רשימת המשאבים אינה תקינה." % action_label)

    known = {resource["id"]: resource for resource in catalog["resources"]}
    max_resources = capability.get("max_resources", 0)
    accepted_types = capability.get("resource_types", [])

    if len(raw) > max_resources:
        if max_resources == 0:
            raise PlanError(
                "%s: היכולת הזו אינה פועלת על חומרי גלם, ולכן אי אפשר לשייך לה קבצים."
                % action_label
            )
        raise PlanError(
            "%s: היכולת הזו מקבלת עד %d משאבים." % (action_label, max_resources)
        )

    validated: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or entry not in known:
            raise PlanError(
                '%s: המשאב "%s" אינו קיים בפרויקט הזה.' % (action_label, entry)
            )
        if entry in validated:
            raise PlanError("%s: אותו משאב מופיע פעמיים." % action_label)

        resource = known[entry]
        if accepted_types and resource["media_type"] not in accepted_types:
            raise PlanError(
                "%s: סוג המשאב אינו נתמך על ידי היכולת הזו." % action_label
            )
        if not resource["available"]:
            raise PlanError(
                '%s: הקובץ "%s" אינו נמצא כרגע במיקומו, ולכן אי אפשר להשתמש בו.'
                % (action_label, resource["filename"])
            )

        validated.append(entry)

    return validated


def validate_actions(project: dict, raw_actions: Any) -> list[dict]:
    """Validate structure *and* meaning of every action against the catalogs."""
    if not isinstance(raw_actions, list):
        raise PlanError("רשימת הפעולות אינה תקינה.")
    if not raw_actions:
        raise PlanError("תוכנית חייבת לכלול לפחות פעולה אחת.")
    if len(raw_actions) > MAX_ACTIONS:
        raise PlanError("תוכנית יכולה לכלול עד %d פעולות." % MAX_ACTIONS)

    catalog = resources.build_catalog(project)
    validated: list[dict] = []
    seen_ids: set[str] = set()

    for index, raw in enumerate(raw_actions):
        action_label = "פעולה %d" % (index + 1)

        if not isinstance(raw, dict):
            raise PlanError("%s: מבנה הפעולה אינו תקין." % action_label)

        action_id = raw.get("id")
        if not isinstance(action_id, str) or not action_id.strip():
            raise PlanError("%s: לפעולה חייב להיות מזהה." % action_label)

        action_id = action_id.strip()
        if len(action_id) > MAX_ACTION_ID_LENGTH or not _ACTION_ID_PATTERN.match(action_id):
            raise PlanError(
                "%s: מזהה הפעולה מכיל תווים לא חוקיים." % action_label
            )
        if action_id in seen_ids:
            raise PlanError("%s: מזהה הפעולה כבר בשימוש (%s)." % (action_label, action_id))
        seen_ids.add(action_id)

        capability = capabilities.get_capability(raw.get("capability_id"))
        parameters = capabilities.validate_parameters(
            capability, raw.get("parameters"), action_label
        )
        resource_ids = _validate_resource_ids(
            capability, raw.get("resource_ids"), catalog, action_label
        )

        validated.append(
            {
                "id": action_id,
                "capability_id": capability["id"],
                "capability_version": capability["version"],
                "resource_ids": resource_ids,
                "parameters": parameters,
                "note": _validate_note(raw.get("note"), action_label),
            }
        )

    return validated


# --- persistence ------------------------------------------------------------


def _revision_numbers(project_id: str, plan_id: str) -> list[int]:
    directory = plan_directory(project_id, plan_id)
    if not directory.is_dir():
        return []

    numbers: list[int] = []
    for path in directory.iterdir():
        if path.suffix != ".json" or path.name.endswith(".approval.json"):
            continue
        match = _REVISION_FILE_PATTERN.match(path.stem)
        if match:
            numbers.append(int(match.group(1)))

    return sorted(numbers)


def _write_revision_exclusively(path: Path, document: dict) -> None:
    """Create the revision file, failing if it already exists.

    Revisions are immutable, so this must be both *exclusive* (a new revision
    can never replace an earlier one — that is what `FileExistsError` protects)
    and *atomic* (a reader polling the plan must never see a half-written file).
    The content is written to a temp file first and then hard-linked into place;
    `os.link` fails if the target exists, which gives both properties at once.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=2)
    temporary = path.parent / (".%s.tmp" % uuid.uuid4().hex[:12])

    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        for attempt in range(storage.WRITE_ATTEMPTS):
            try:
                os.link(temporary, path)
                break
            except FileExistsError:
                raise
            except OSError:
                # Either a transient Windows sharing violation, or a filesystem
                # without hard links. Retry, then fall back to a checked
                # replace, which still keeps atomicity for readers.
                if attempt < storage.WRITE_ATTEMPTS - 1:
                    time.sleep(storage.WRITE_RETRY_SECONDS)
                    continue
                if path.exists():
                    raise FileExistsError(path) from None
                storage.replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_revision_file(project_id: str, plan_id: str, revision: int) -> dict:
    path = revision_file(project_id, plan_id, revision)

    try:
        data = storage.read_json(path)
    except FileNotFoundError as error:
        raise PlanNotFound("גרסת התוכנית לא נמצאה.") from error
    except (OSError, ValueError) as error:
        raise PlanError(
            "לא ניתן לקרוא את קובץ התוכנית: %s" % error, status_code=422
        ) from error

    if not isinstance(data, dict):
        raise PlanError("קובץ התוכנית פגום.", status_code=422)

    schema_version = data.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise PlanError("קובץ התוכנית פגום: חסרה גרסת סכמה.", status_code=422)
    if schema_version > PLAN_SCHEMA_VERSION:
        raise PlanError(
            "קובץ התוכנית נוצר בגרסה חדשה יותר (%d) ואינו נתמך (%d)."
            % (schema_version, PLAN_SCHEMA_VERSION),
            status_code=422,
        )

    data["plan_id"] = plan_id
    data["project_id"] = project_id
    data["revision"] = revision
    return data


def _read_approval(project_id: str, plan_id: str, revision: int) -> dict | None:
    path = approval_file(project_id, plan_id, revision)
    if not path.exists():
        return None

    try:
        data = storage.read_json(path)
    except (OSError, ValueError):
        return None

    return data if isinstance(data, dict) else None


def create_revision(
    project: dict,
    summary: Any,
    actions: Any,
    origin: str,
    provider: dict,
    instruction: Any,
    plan_id: str | None = None,
) -> dict:
    """Validate a proposal or an edit and persist it as the next revision."""
    project_id = project["id"]
    validated_summary = validate_summary(summary)
    validated_actions = validate_actions(project, actions)
    validated_instruction = validate_instruction(instruction)

    if plan_id is None:
        plan_id = uuid.uuid4().hex
        created_at = _now()
        existing = []
    else:
        plan_id = validate_plan_id(plan_id)
        existing = _revision_numbers(project_id, plan_id)
        if not existing:
            raise PlanNotFound("התוכנית לא נמצאה.")
        created_at = _read_revision_file(project_id, plan_id, existing[0]).get(
            "created_at"
        ) or _now()

    if len(existing) >= MAX_REVISIONS:
        raise PlanError("התוכנית הגיעה למספר הגרסאות המרבי.")

    document = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_id": plan_id,
        "project_id": project_id,
        "revision": (existing[-1] + 1) if existing else 1,
        "created_at": created_at,
        "revised_at": _now(),
        "origin": origin,
        "instruction": validated_instruction,
        "provider": provider,
        "capability_catalog_version": capabilities.CATALOG_VERSION,
        "resource_catalog_version": resources.RESOURCE_CATALOG_VERSION,
        "input_fingerprint": resources.fingerprint(project),
        "input_snapshot": resources.input_snapshot(project),
        "summary": validated_summary,
        "actions": validated_actions,
    }

    # Retry on the (single-user, very unlikely) race of two revisions at once.
    for attempt in range(5):
        path = revision_file(project_id, plan_id, document["revision"])
        try:
            _write_revision_exclusively(path, document)
            break
        except FileExistsError:
            document["revision"] += 1
            if attempt == 4:
                raise PlanError("שמירת גרסת התוכנית נכשלה.", status_code=500) from None
        except OSError as error:
            raise PlanError(
                "שמירת גרסת התוכנית נכשלה: %s" % error, status_code=500
            ) from error

    return describe_revision(project, document)


def approve_revision(project: dict, plan_id: str, revision: int) -> dict:
    """Record the user's explicit approval of one revision."""
    project_id = project["id"]
    plan_id = validate_plan_id(plan_id)
    revision = validate_revision_number(revision)

    document = _read_revision_file(project_id, plan_id, revision)
    described = describe_revision(project, document)

    if described["outdated"]:
        raise PlanError(
            "התוכנית אינה מעודכנת ביחס לפרויקט, ולכן אי אפשר לאשר אותה. "
            "יש ליצור תוכנית חדשה או גרסה חדשה."
        )
    if not described["is_latest"]:
        raise PlanError("אפשר לאשר רק את הגרסה האחרונה של התוכנית.")
    if described["approved"]:
        return described

    # Re-validate against the current catalogs before approving.
    validate_actions(project, document["actions"])

    try:
        storage.write_json_atomic(
            approval_file(project_id, plan_id, revision),
            {
                "schema_version": PLAN_SCHEMA_VERSION,
                "plan_id": plan_id,
                "project_id": project_id,
                "revision": revision,
                "approved_at": _now(),
                "input_fingerprint": document["input_fingerprint"],
            },
        )
    except OSError as error:
        raise PlanError("שמירת האישור נכשלה: %s" % error, status_code=500) from error

    return read_revision(project, plan_id, revision)


# --- reading and presentation -----------------------------------------------


def describe_revision(project: dict, document: dict) -> dict:
    """Add the derived state: approval, staleness, execution readiness."""
    project_id = project["id"]
    plan_id = document["plan_id"]
    revision = document["revision"]

    revisions = _revision_numbers(project_id, plan_id)
    latest = revisions[-1] if revisions else revision

    approval = _read_approval(project_id, plan_id, revision)
    current_fingerprint = resources.fingerprint(project)
    outdated = document.get("input_fingerprint") != current_fingerprint

    described = dict(document)
    described.update(
        {
            "approved": approval is not None,
            "approved_at": approval.get("approved_at") if approval else None,
            "status": STATUS_APPROVED if approval else STATUS_PROPOSED,
            "outdated": outdated,
            "outdated_reason": (
                "חומרי הגלם או ההגדרות של הפרויקט השתנו מאז שהתוכנית נוצרה."
                if outdated
                else None
            ),
            "is_latest": revision == latest,
            "latest_revision": latest,
            "revisions": revisions or [revision],
            "current_input_fingerprint": current_fingerprint,
        }
    )

    described["executable"], described["blocked_reason"] = _execution_state(described)
    return described


def _execution_state(described: dict) -> tuple[bool, str | None]:
    if described["outdated"]:
        return False, described["outdated_reason"]
    if not described["approved"]:
        return False, "התוכנית טרם אושרה. יש לאשר אותה לפני הרצה."
    if not described["is_latest"]:
        return False, "קיימת גרסה חדשה יותר של התוכנית."

    for action in described["actions"]:
        capability = capabilities.CAPABILITIES.get(action["capability_id"])
        if capability is None or not capability.get("executable"):
            return False, (
                'התוכנית כוללת יכולת שאינה זמינה להרצה: "%s".'
                % action["capability_id"]
            )

    return True, None


def read_revision(project: dict, plan_id: str, revision: Any) -> dict:
    plan_id = validate_plan_id(plan_id)
    revision = validate_revision_number(revision)
    document = _read_revision_file(project["id"], plan_id, revision)
    return describe_revision(project, document)


def read_latest(project: dict, plan_id: str) -> dict:
    plan_id = validate_plan_id(plan_id)
    revisions = _revision_numbers(project["id"], plan_id)
    if not revisions:
        raise PlanNotFound("התוכנית לא נמצאה.")
    return read_revision(project, plan_id, revisions[-1])


def list_plans(project: dict) -> list[dict]:
    """One entry per plan, showing its latest revision. Newest first."""
    directory = plans_directory(project["id"])
    if not directory.is_dir():
        return []

    entries: list[dict] = []
    for plan_path in directory.iterdir():
        if not plan_path.is_dir() or not _PLAN_ID_PATTERN.match(plan_path.name):
            continue
        try:
            latest = read_latest(project, plan_path.name)
        except PlanError:
            continue

        entries.append(
            {
                "plan_id": latest["plan_id"],
                "revision": latest["revision"],
                "revisions": latest["revisions"],
                "created_at": latest["created_at"],
                "revised_at": latest["revised_at"],
                "summary": latest["summary"],
                "instruction": latest["instruction"],
                "origin": latest["origin"],
                "provider": latest["provider"],
                "status": latest["status"],
                "approved": latest["approved"],
                "outdated": latest["outdated"],
                "executable": latest["executable"],
                "action_count": len(latest["actions"]),
            }
        )

    entries.sort(key=lambda entry: entry["revised_at"], reverse=True)
    return entries


def assert_executable(project: dict, plan_id: str, revision: Any) -> dict:
    """The gate every execution goes through, re-checked at execution time."""
    described = read_revision(project, plan_id, revision)

    if not described["executable"]:
        raise PlanError(described["blocked_reason"] or "אי אפשר להריץ את התוכנית.")

    # Parameters and resources are validated again here: the catalog may have
    # changed, or a source file may have disappeared since approval.
    validate_actions(project, described["actions"])
    return described
