import json
import uuid
from datetime import date, datetime, timezone

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, inspect
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.database import Base, engine, get_db

# Important: import models before create_all
from app import models  # noqa: F401

from app.models import (
    Employee,
    OrgMembershipSnapshot,
    OrgSnapshot,
    OrgUnitSnapshot,
    Question,
    ResponseAnswer,
    ResponseSubmission,
    SurveyCycle,
    SurveyParticipant,
)

from app.services.analytics_service import (
    get_company_benchmark,
    get_enps_pulse_trend,
    get_manager_cycle_report,
    get_manager_trends,
    get_org_unit_cycle_report,
    get_org_unit_trends,
    get_question_breakdown,
)
from app.services.privacy_service import (
    apply_privacy_to_enps_pulse,
    apply_privacy_to_manager_report,
    apply_privacy_to_trends,
    apply_threshold_to_question_breakdown,
    build_company_comparison,
)
from app.services.org_service import (
    get_current_managed_org_unit,
    get_current_org_root_unit,
    get_descendant_org_units,
    get_managed_org_unit_for_cycle,
    get_latest_survey_cycle,
    get_org_unit_by_external_key_for_cycle,
    get_org_unit_path,
    get_survey_cycle,
    is_org_unit_descendant_or_self,
)


app = FastAPI(
    title="Survey Insights Demo",
    description="Privacy-safe employee survey analytics demo",
    version="0.1.0",
)

Base.metadata.create_all(bind=engine)

app.add_middleware(
    SessionMiddleware,
    secret_key="dev-only-mock-sso-secret-change-in-production",
    same_site="lax",
    https_only=False,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")


DEFAULT_QUESTIONS = [
    {
        "key": "employee_health",
        "category": "Health",
        "text": "I feel healthy and energized at work.",
        "response_type": "likert_1_5",
        "order": 1,
    },
    {
        "key": "workload_balance",
        "category": "Workload",
        "text": "My workload is manageable.",
        "response_type": "likert_1_5",
        "order": 2,
    },
    {
        "key": "leadership_support",
        "category": "Leadership",
        "text": "I get the support I need from my manager.",
        "response_type": "likert_1_5",
        "order": 3,
    },
    {
        "key": "team_collaboration",
        "category": "Collaboration",
        "text": "Collaboration in my team works well.",
        "response_type": "likert_1_5",
        "order": 4,
    },
    {
        "key": "enps",
        "category": "eNPS",
        "text": "How likely are you to recommend this company as a place to work?",
        "response_type": "enps_0_10",
        "order": 5,
    },
]


DASHBOARD_CATEGORIES = ["Health", "Workload", "Leadership", "Collaboration", "eNPS"]
LIKERT_CATEGORIES = ["Health", "Workload", "Leadership", "Collaboration"]

# Company priority weights per area. These reflect company goals (set by P&C; in
# production they would be configured per cycle) and only influence which area is
# surfaced as the focus — they never change the displayed scores. A higher weight
# means a gap in this area is treated as more urgent, so it wins the focus pick on
# ties: with Health weighted above Collaboration, two equal-low scores resolve to
# Health.
CATEGORY_PRIORITY_WEIGHTS = {
    "Health": 1.5,
    "Workload": 1.2,
    "Leadership": 1.0,
    "Collaboration": 1.0,
    "eNPS": 1.0,
}

DISCUSSION_PROMPTS = {
    "Health": [
        "What is draining or energising the team right now?",
        "What would make the current pace more sustainable?",
    ],
    "Workload": [
        "Which work should we stop, simplify, or re-prioritise?",
        "Where are bottlenecks or unclear priorities creating pressure?",
    ],
    "Leadership": [
        "What support, clarity, or feedback does the team need from me?",
        "Where do decisions need to be faster or more transparent?",
    ],
    "Collaboration": [
        "Which handoffs or dependencies are slowing the team down?",
        "Where do we need clearer ownership or better rituals?",
    ],
    "eNPS": [
        "What would make this team more likely to recommend working here?",
        "What is one concrete thing that would improve the employee experience?",
    ],
}


def get_visible_score(category_scores: list[dict], category: str) -> float | None:
    for score in category_scores or []:
        if score.get("category") != category or not score.get("visible"):
            continue

        if category == "eNPS":
            return score.get("enps_score")

        return score.get("average_score")

    return None


def classify_trend(category: str, delta: float | None) -> str:
    if delta is None:
        return "Not enough data"

    threshold = 10 if category == "eNPS" else 0.2

    if delta >= threshold:
        return "Improving"

    if delta <= -threshold:
        return "Needs attention"

    return "Stable"


def format_delta(category: str, delta: float | None) -> str:
    if delta is None:
        return "—"

    if category == "eNPS":
        return f"{delta:+.0f}"

    return f"{delta:+.1f}"


def build_manager_usability_summary(
    safe_report: dict,
    safe_trends: list[dict],
    selected_cycle_id: int,
) -> dict:
    """
    Builds a small, manager-friendly summary layer.

    This is intentionally rule-based rather than AI-generated. The aim is to
    help a manager understand the result quickly without exposing extra data or
    adding risky filters.
    """

    overall_scores = safe_report.get("overall_rollup", {}).get("category_scores", [])

    visible_likert_scores = [
        {
            "category": category,
            "value": get_visible_score(overall_scores, category),
        }
        for category in LIKERT_CATEGORIES
    ]
    visible_likert_scores = [item for item in visible_likert_scores if item["value"] is not None]

    top_concern = None
    strongest_area = None

    if visible_likert_scores:
        # Focus pick is priority-weighted: priority = gap-from-top (5 - score)
        # scaled by the area's company weight. The biggest weighted gap wins, so
        # a less-than-worst score in a high-priority area can outrank a slightly
        # lower score in a low-priority one, and ties break toward the heavier
        # area. Displayed scores are untouched.
        for item in visible_likert_scores:
            weight = CATEGORY_PRIORITY_WEIGHTS.get(item["category"], 1.0)
            item["priority_weight"] = weight
            item["priority_score"] = round((5 - item["value"]) * weight, 2)

        top_concern = max(
            visible_likert_scores,
            key=lambda item: (item["priority_score"], item["priority_weight"]),
        )
        strongest_area = max(visible_likert_scores, key=lambda item: item["value"])

    selected_trend_index = next(
        (
            index
            for index, row in enumerate(safe_trends)
            if row.get("survey_cycle_id") == selected_cycle_id
        ),
        None,
    )

    selected_trend = safe_trends[selected_trend_index] if selected_trend_index is not None else None
    previous_visible_trend = None

    if selected_trend_index is not None:
        for row in reversed(safe_trends[:selected_trend_index]):
            if row.get("visible"):
                previous_visible_trend = row
                break

    movements = []

    for category in DASHBOARD_CATEGORIES:
        current_value = None
        previous_value = None

        if selected_trend and selected_trend.get("visible"):
            current_value = get_visible_score(selected_trend.get("category_scores", []), category)

        if previous_visible_trend:
            previous_value = get_visible_score(previous_visible_trend.get("category_scores", []), category)

        delta = None
        if current_value is not None and previous_value is not None:
            # Round to one decimal so the status label and the displayed delta
            # agree at the threshold (raw float subtraction like 3.3 - 3.1 is
            # 0.1999…, which would read "+0.2" but classify as "Stable").
            delta = round(current_value - previous_value, 1)

        movements.append(
            {
                "category": category,
                "current_value": current_value,
                "previous_value": previous_value,
                "delta": delta,
                "delta_label": format_delta(category, delta),
                "status": classify_trend(category, delta),
            }
        )

    comparable_movements = [item for item in movements if item["delta"] is not None]
    biggest_drop = None
    biggest_improvement = None

    if comparable_movements:
        biggest_drop = min(comparable_movements, key=lambda item: item["delta"])
        biggest_improvement = max(comparable_movements, key=lambda item: item["delta"])

    org_units = safe_report.get("org_units", [])
    hidden_units = [unit for unit in org_units if not unit.get("visible")]
    secondary_hidden_units = [unit for unit in hidden_units if unit.get("secondary_suppression")]
    threshold_hidden_units = [unit for unit in hidden_units if not unit.get("secondary_suppression")]

    # Keep the cockpit telling one story: the conversation prompts and the
    # team-comparison chart follow the same priority-weighted focus area as the
    # focus panel, rather than a separately-chosen biggest-drop category.
    prompt_category = top_concern["category"] if top_concern else None

    prompts = DISCUSSION_PROMPTS.get(prompt_category or "Workload", DISCUSSION_PROMPTS["Workload"])

    return {
        "top_concern": top_concern,
        "strongest_area": strongest_area,
        "biggest_drop": biggest_drop,
        "biggest_improvement": biggest_improvement,
        "movements": movements,
        "hidden_unit_count": len(hidden_units),
        "secondary_hidden_unit_count": len(secondary_hidden_units),
        "threshold_hidden_unit_count": len(threshold_hidden_units),
        "suggested_prompt_category": prompt_category or "Workload",
        "suggested_prompts": prompts,
    }


def build_current_score_chart_data(safe_report: dict) -> dict:
    scores = safe_report.get("overall_rollup", {}).get("category_scores", [])

    return {
        "labels": LIKERT_CATEGORIES,
        "datasets": [
            {
                "label": "Current score",
                "data": [get_visible_score(scores, category) for category in LIKERT_CATEGORIES],
            }
        ],
    }


def build_team_comparison_chart_data(safe_report: dict, focus_category: str) -> dict:
    units = safe_report.get("org_units", [])
    focus_category = focus_category if focus_category in DASHBOARD_CATEGORIES else "Workload"

    # Compare immediate child teams only. If the manager has no visible children,
    # fall back to the manager's own visible scope.
    candidate_units = [unit for unit in units if unit.get("depth") == 1 and unit.get("visible")]

    if not candidate_units:
        candidate_units = [unit for unit in units if unit.get("depth") == 0 and unit.get("visible")]

    scored_units = []
    for unit in candidate_units:
        score = get_visible_score(unit.get("category_scores", []), focus_category)
        if score is None:
            continue
        scored_units.append((unit.get("org_unit_name"), score))

    # Sort teams lowest to highest on the focus category.
    scored_units.sort(key=lambda item: item[1])

    labels = [name for name, _ in scored_units]
    values = [score for _, score in scored_units]

    return {
        "focus_category": focus_category,
        "labels": labels,
        "datasets": [
            {
                "label": focus_category,
                "data": values,
            }
        ],
    }


def build_visibility_chart_data(safe_report: dict) -> dict:
    org_units = safe_report.get("org_units", [])

    visible_count = len([unit for unit in org_units if unit.get("visible")])
    secondary_count = len(
        [unit for unit in org_units if not unit.get("visible") and unit.get("secondary_suppression")]
    )
    threshold_count = len(
        [unit for unit in org_units if not unit.get("visible") and not unit.get("secondary_suppression")]
    )

    return {
        "labels": ["Visible", "Hidden <4", "Secondary suppression"],
        "datasets": [
            {
                "label": "Org units",
                "data": [visible_count, threshold_count, secondary_count],
            }
        ],
    }


def build_lowest_questions_chart_data(
    question_breakdown: dict,
    limit: int = 5,
) -> dict:
    """
    The lowest-scoring individual questions across the 1-5 themes.

    Used for teams with no comparable child teams: instead of an empty sibling
    comparison, surface the specific questions to act on first. eNPS is excluded
    (different scale). Only visible (>= threshold) questions are shown.
    """

    items = []
    for category in LIKERT_CATEGORIES:
        for question in question_breakdown.get(category, []):
            if question.get("visible") and question.get("average_score") is not None:
                items.append(
                    {
                        "text": question["text"],
                        "category": category,
                        "score": question["average_score"],
                    }
                )

    items.sort(key=lambda item: item["score"])
    items = items[:limit]

    def short(text: str) -> str:
        text = text.rstrip(".")
        return text if len(text) <= 24 else text[:23].rstrip() + "…"

    return {
        "labels": [short(item["text"]) for item in items],
        "full_labels": [item["text"] for item in items],
        "categories": [item["category"] for item in items],
        "datasets": [{"label": "Score", "data": [item["score"] for item in items]}],
    }


def get_survey_admin_rows(db: Session) -> list[dict]:
    cycles = db.query(SurveyCycle).order_by(SurveyCycle.starts_on.desc()).all()
    rows = []

    for cycle in cycles:
        participant_count = (
            db.query(func.count(SurveyParticipant.id))
            .filter(SurveyParticipant.survey_cycle_id == cycle.id)
            .scalar()
            or 0
        )
        submitted_count = (
            db.query(func.count(SurveyParticipant.id))
            .filter(
                SurveyParticipant.survey_cycle_id == cycle.id,
                SurveyParticipant.has_submitted.is_(True),
            )
            .scalar()
            or 0
        )
        response_count = (
            db.query(func.count(ResponseSubmission.id))
            .filter(ResponseSubmission.survey_cycle_id == cycle.id)
            .scalar()
            or 0
        )

        response_rate = None
        if participant_count:
            response_rate = round((submitted_count / participant_count) * 100)

        rows.append(
            {
                "cycle": cycle,
                "participant_count": participant_count,
                "submitted_count": submitted_count,
                "response_count": response_count,
                "response_rate": response_rate,
                "question_count": len(cycle.questions),
            }
        )

    return rows


def create_default_questions_for_cycle(db: Session, cycle: SurveyCycle) -> None:
    for question_def in DEFAULT_QUESTIONS:
        db.add(
            Question(
                survey_cycle=cycle,
                question_key=question_def["key"],
                category=question_def["category"],
                text=question_def["text"],
                response_type=question_def["response_type"],
                display_order=question_def["order"],
                is_required=True,
            )
        )


def create_participants_for_cycle(db: Session, cycle: SurveyCycle) -> int:
    memberships = (
        db.query(OrgMembershipSnapshot)
        .filter(OrgMembershipSnapshot.snapshot_id == cycle.org_snapshot_id)
        .all()
    )

    created = 0

    for membership in memberships:
        existing = (
            db.query(SurveyParticipant)
            .filter(
                SurveyParticipant.survey_cycle_id == cycle.id,
                SurveyParticipant.employee_id == membership.employee_id,
            )
            .first()
        )

        if existing:
            continue

        db.add(
            SurveyParticipant(
                survey_cycle=cycle,
                employee=membership.employee,
                org_unit_at_time=membership.org_unit,
                has_submitted=False,
            )
        )
        created += 1

    return created


def get_current_employee(
    request: Request,
    db: Session,
) -> Employee | None:
    """
    Demo helper for mocked SSO.

    In production, the employee identity would come from the Entra ID / OIDC
    claims. Here we store the selected demo employee id in the signed session.
    """

    employee_id = request.session.get("employee_id")

    if not employee_id:
        return None

    return (
        db.query(Employee)
        .filter(Employee.id == int(employee_id))
        .first()
    )


def require_current_employee(
    request: Request,
    db: Session,
) -> Employee:
    current_employee = get_current_employee(request=request, db=db)

    if not current_employee:
        raise HTTPException(status_code=401, detail="Not signed in")

    return current_employee


def get_participant_for_current_employee(
    db: Session,
    participant_id: int,
    current_employee: Employee,
) -> SurveyParticipant:
    """
    Returns a participant record only if it belongs to the signed-in employee.

    In production, current_employee would come from Entra ID / OIDC claims.
    This check prevents a user from opening someone else's survey eligibility record.
    """

    participant = (
        db.query(SurveyParticipant)
        .filter(
            SurveyParticipant.id == participant_id,
            SurveyParticipant.employee_id == current_employee.id,
        )
        .first()
    )

    if not participant:
        raise HTTPException(status_code=404, detail="Survey participant record not found")

    return participant


def assert_pc_admin(current_employee: Employee) -> None:
    if not getattr(current_employee, "is_pc_admin", False):
        raise HTTPException(
            status_code=403,
            detail="Only People & Culture admins can access this area",
        )


def get_current_manager_org_unit_or_403(
    db: Session,
    current_employee: Employee,
) -> OrgUnitSnapshot:
    if not current_employee.is_manager:
        raise HTTPException(status_code=403, detail="Only managers can view manager dashboards")

    current_org_unit = get_current_managed_org_unit(
        db=db,
        manager_id=current_employee.id,
    )

    if not current_org_unit:
        raise HTTPException(
            status_code=403,
            detail="Signed-in manager has no current org scope",
        )

    return current_org_unit


def get_target_manager_current_org_unit_or_404(
    db: Session,
    manager_id: int,
) -> tuple[Employee, OrgUnitSnapshot]:
    manager = (
        db.query(Employee)
        .filter(Employee.id == manager_id, Employee.is_manager.is_(True))
        .first()
    )

    if not manager:
        raise HTTPException(status_code=404, detail="Manager not found")

    current_org_unit = get_current_managed_org_unit(db=db, manager_id=manager_id)

    if not current_org_unit:
        raise HTTPException(
            status_code=404,
            detail="Manager has no current org scope",
        )

    return manager, current_org_unit


def assert_can_view_manager_dashboard(
    db: Session,
    current_employee: Employee,
    target_manager_id: int,
) -> tuple[Employee, OrgUnitSnapshot, OrgUnitSnapshot]:
    """
    Enforces dashboard access from the current org.

    Historical survey snapshots decide what a result is about. The current org
    decides who may see it. A manager can view their own current scope and any
    manager scope below them in the current org tree.
    """

    target_manager, target_org_unit = get_target_manager_current_org_unit_or_404(
        db=db,
        manager_id=target_manager_id,
    )

    # P&C admins can view every manager dashboard. Their effective scope is the
    # whole company, so the cross-dashboard suppression mask is computed
    # company-wide — anonymity still holds, they just aren't org-scope limited.
    if getattr(current_employee, "is_pc_admin", False):
        company_root = get_current_org_root_unit(db)
        if not company_root:
            raise HTTPException(status_code=403, detail="No current org is available")
        return target_manager, target_org_unit, company_root

    viewer_org_unit = get_current_manager_org_unit_or_403(
        db=db,
        current_employee=current_employee,
    )

    if not is_org_unit_descendant_or_self(
        db=db,
        ancestor=viewer_org_unit,
        candidate=target_org_unit,
    ):
        raise HTTPException(
            status_code=403,
            detail="You can only view manager dashboards inside your current org scope",
        )

    return target_manager, target_org_unit, viewer_org_unit


def build_cross_dashboard_privacy_block(
    db: Session,
    current_employee: Employee,
    viewer_org_unit: OrgUnitSnapshot,
    target_org_unit: OrgUnitSnapshot,
    selected_cycle: SurveyCycle,
) -> dict | None:
    """
    Prevents navigation around secondary suppression.

    Example:
    Olivia can view Engineering. In that Engineering view, Data Team may be
    secondarily suppressed to protect AI Lab. If Olivia then opens Priya's Data
    Team dashboard directly for the same cycle, the subtraction attack becomes
    live again.

    To close that gap, the viewer's broad current-org scope creates a privacy
    mask for the selected historical cycle. If the requested dashboard root, or
    any branch leading to it, is hidden in that broad view, the standalone
    dashboard is blocked for that viewer and cycle.
    """

    # A manager may always open their own root scope. The suppression mask only
    # blocks navigation into descendant dashboards that were hidden in the
    # manager's broader view.
    if viewer_org_unit.external_key == target_org_unit.external_key:
        return None

    historical_viewer_root = get_org_unit_by_external_key_for_cycle(
        db=db,
        external_key=viewer_org_unit.external_key,
        survey_cycle_id=selected_cycle.id,
    )
    historical_target_root = get_org_unit_by_external_key_for_cycle(
        db=db,
        external_key=target_org_unit.external_key,
        survey_cycle_id=selected_cycle.id,
    )

    if not historical_viewer_root or not historical_target_root:
        return None

    raw_viewer_report = get_org_unit_cycle_report(
        db=db,
        root_org_unit=historical_viewer_root,
        survey_cycle_id=selected_cycle.id,
        manager_id=current_employee.id,
    )

    if not raw_viewer_report:
        return None

    safe_viewer_report = apply_privacy_to_manager_report(raw_viewer_report)
    safe_units_by_id = {
        unit.get("org_unit_id"): unit
        for unit in safe_viewer_report.get("org_units", [])
        if unit.get("org_unit_id") is not None
    }

    current_unit = safe_units_by_id.get(historical_target_root.id)

    while current_unit and current_unit.get("org_unit_id") != historical_viewer_root.id:
        if not current_unit.get("visible"):
            return {
                "blocked": True,
                "blocked_org_unit_name": current_unit.get("org_unit_name"),
                "requested_org_unit_name": target_org_unit.name,
                "viewer_scope_name": viewer_org_unit.name,
                "survey_cycle_name": selected_cycle.name,
                "reason": current_unit.get("reason") or "Hidden by privacy suppression",
                "privacy_note": current_unit.get("privacy_note"),
                "message": (
                    f"{target_org_unit.name} is hidden for your {viewer_org_unit.name} "
                    f"view in {selected_cycle.name}. Opening it as a standalone "
                    "dashboard would bypass the suppression mask and could reveal "
                    "a smaller team."
                ),
            }

        parent_id = current_unit.get("parent_id")
        current_unit = safe_units_by_id.get(parent_id)

    return None


def parse_numeric_answer(
    raw_value: str | None,
    question: Question,
) -> float:
    if raw_value is None or raw_value == "":
        raise HTTPException(status_code=400, detail=f"Missing answer for: {question.text}")

    try:
        numeric_value = float(raw_value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid answer value") from exc

    if question.response_type == "likert_1_5" and numeric_value not in {1, 2, 3, 4, 5}:
        raise HTTPException(status_code=400, detail="Likert answers must be between 1 and 5")

    if question.response_type == "enps_0_10" and numeric_value not in set(range(0, 11)):
        raise HTTPException(status_code=400, detail="eNPS answers must be between 0 and 10")

    return numeric_value


def build_manager_dashboard_context(
    db: Session,
    manager_id: int,
    current_employee: Employee,
    survey_cycle_id: int | None = None,
) -> dict:
    managers = (
        db.query(Employee)
        .filter(Employee.is_manager.is_(True))
        .order_by(Employee.full_name)
        .all()
    )

    manager, current_target_org_unit, viewer_org_unit = assert_can_view_manager_dashboard(
        db=db,
        current_employee=current_employee,
        target_manager_id=manager_id,
    )

    # The cycle selector is for the annual/half-year survey; eNPS pulses are a
    # separate cadence and are not selectable here.
    cycles = (
        db.query(SurveyCycle)
        .filter(SurveyCycle.cycle_type.in_(["annual", "half_year"]))
        .order_by(SurveyCycle.starts_on)
        .all()
    )

    if not cycles:
        raise HTTPException(status_code=404, detail="No survey cycles found")

    selected_cycle = (
        get_survey_cycle(db, survey_cycle_id)
        if survey_cycle_id
        else get_latest_survey_cycle(db)
    )

    if not selected_cycle:
        raise HTTPException(status_code=404, detail="Survey cycle not found")

    privacy_block = build_cross_dashboard_privacy_block(
        db=db,
        current_employee=current_employee,
        viewer_org_unit=viewer_org_unit,
        target_org_unit=current_target_org_unit,
        selected_cycle=selected_cycle,
    )

    if privacy_block:
        return {
            "_template_name": "manager_blocked.html",
            "title": "Dashboard Hidden",
            "manager": manager,
            "cycles": cycles,
            "selected_cycle": selected_cycle,
            "privacy_block": privacy_block,
            "access_model_note": (
                "Access is resolved from the current org, but privacy suppression "
                "is enforced across every dashboard the viewer can navigate to."
            ),
            "viewer_org_unit_path": " > ".join(get_org_unit_path(viewer_org_unit)),
            "current_target_org_unit_path": " > ".join(get_org_unit_path(current_target_org_unit)),
        }

    historical_root_org_unit = get_org_unit_by_external_key_for_cycle(
        db=db,
        external_key=current_target_org_unit.external_key,
        survey_cycle_id=selected_cycle.id,
    )

    if not historical_root_org_unit:
        raise HTTPException(
            status_code=404,
            detail="The current org unit does not exist in the selected historical snapshot",
        )

    raw_report = get_org_unit_cycle_report(
        db=db,
        root_org_unit=historical_root_org_unit,
        survey_cycle_id=selected_cycle.id,
        manager_id=manager_id,
    )

    if not raw_report:
        raise HTTPException(
            status_code=404,
            detail="No report found for this manager and survey cycle",
        )

    safe_report = apply_privacy_to_manager_report(raw_report)
    safe_trends = apply_privacy_to_trends(
        get_org_unit_trends(
            db=db,
            current_org_unit_external_key=current_target_org_unit.external_key,
        )
    )


    manager_summary = build_manager_usability_summary(
        safe_report=safe_report,
        safe_trends=safe_trends,
        selected_cycle_id=selected_cycle.id,
    )

    # Per-question scores behind each category, suppressed independently.
    scope_org_unit_ids = [
        unit.id for unit in get_descendant_org_units(db, historical_root_org_unit)
    ]
    question_breakdown = apply_threshold_to_question_breakdown(
        get_question_breakdown(
            db=db,
            survey_cycle_id=selected_cycle.id,
            org_unit_ids=scope_org_unit_ids,
        )
    )

    # Company-wide comparison baseline. Skipped when the manager's scope is the
    # company root itself (comparing to yourself is meaningless).
    company_comparison = None
    if historical_root_org_unit.parent_id is not None:
        raw_company = get_company_benchmark(db=db, survey_cycle_id=selected_cycle.id)
        if raw_company:
            company_comparison = build_company_comparison(
                scope_category_scores=safe_report.get("overall_rollup", {}).get(
                    "category_scores", []
                ),
                raw_company_category_scores=raw_company["rolled_up_category_scores"],
                company_name=raw_company["company_org_unit_name"],
            )

    focus_category = manager_summary.get("suggested_prompt_category") or "Workload"

    current_score_chart_data = build_current_score_chart_data(safe_report)
    team_comparison_chart_data = build_team_comparison_chart_data(
        safe_report=safe_report,
        focus_category=focus_category,
    )
    visibility_chart_data = build_visibility_chart_data(safe_report)

    # Sibling comparison only makes sense with 2+ comparable child teams. For a
    # leaf team, swap that panel for the lowest-scoring questions to act on.
    has_comparable_children = len(team_comparison_chart_data.get("labels", [])) >= 2
    lowest_questions_chart_data = build_lowest_questions_chart_data(question_breakdown)

    def trend_series(category: str) -> list:
        values = []
        for row in safe_trends:
            if not row.get("visible"):
                values.append(None)
                continue

            score = next(
                (
                    item
                    for item in row.get("category_scores", [])
                    if item.get("category") == category and item.get("visible")
                ),
                None,
            )

            if not score:
                values.append(None)
            elif category == "eNPS":
                values.append(score.get("enps_score"))
            else:
                values.append(score.get("average_score"))

        return values

    trend_labels = [row["survey_cycle_name"] for row in safe_trends]

    # The four 1-5 themes share one chart. eNPS is a -100..100 net score, so it
    # gets its own chart rather than a second axis sharing the Likert frame.
    chart_data = {
        "labels": trend_labels,
        "datasets": [
            {
                "label": category,
                "data": trend_series(category),
                "tension": 0.25,
                "spanGaps": False,
            }
            for category in LIKERT_CATEGORIES
        ],
    }

    # eNPS is a continuous monthly pulse, so its trend shows every pulse (its own
    # cadence), not the annual/half-year survey points.
    enps_pulse = apply_privacy_to_enps_pulse(
        get_enps_pulse_trend(
            db=db,
            current_org_unit_external_key=current_target_org_unit.external_key,
        )
    )

    enps_chart_data = {
        "labels": [
            pulse["survey_cycle_name"].replace("eNPS Pulse ", "") for pulse in enps_pulse
        ],
        "datasets": [
            {
                "label": "eNPS",
                "data": [
                    pulse.get("enps_score") if pulse.get("visible") else None
                    for pulse in enps_pulse
                ],
                "tension": 0.25,
                "spanGaps": False,
            }
        ],
    }

    # The main-page eNPS card shows the pulse closest in time to the selected
    # survey cycle.
    def closest_pulse(pulse_rows):
        if not pulse_rows:
            return None
        return min(
            pulse_rows,
            key=lambda pulse: abs((pulse["starts_on"] - selected_cycle.starts_on).days),
        )

    enps_pulse_card = closest_pulse(enps_pulse)

    # Company benchmark from the pulse closest to the same date.
    enps_pulse_comparison = None
    if historical_root_org_unit.parent_id is not None and enps_pulse_card and enps_pulse_card.get("visible"):
        company_pulse = closest_pulse(
            apply_privacy_to_enps_pulse(
                get_enps_pulse_trend(db=db, current_org_unit_external_key="company")
            )
        )
        if company_pulse and company_pulse.get("visible"):
            company_enps = company_pulse["enps_score"]
            delta = enps_pulse_card["enps_score"] - company_enps
            enps_pulse_comparison = {
                "company_name": "Company",
                "benchmark": company_enps,
                "delta": delta,
                "delta_label": f"+{delta}" if delta > 0 else str(delta),
                "direction": "above" if delta > 0 else ("below" if delta < 0 else "inline"),
            }

    return {
        "title": "Manager Dashboard",
        "manager": manager,
        "managers": managers,
        "cycles": cycles,
        "selected_cycle": selected_cycle,
        "report": safe_report,
        "trends": safe_trends,
        "manager_summary": manager_summary,
        "company_comparison": company_comparison,
        "question_breakdown": question_breakdown,
        "enps_pulse_card": enps_pulse_card,
        "enps_pulse_comparison": enps_pulse_comparison,
        "chart_data_json": json.dumps(chart_data, default=str),
        "enps_chart_data_json": json.dumps(enps_chart_data, default=str),
        "current_score_chart_data_json": json.dumps(current_score_chart_data, default=str),
        "team_comparison_chart_data_json": json.dumps(team_comparison_chart_data, default=str),
        "visibility_chart_data_json": json.dumps(visibility_chart_data, default=str),
        "has_comparable_children": has_comparable_children,
        "lowest_questions_chart_data": lowest_questions_chart_data,
        "lowest_questions_chart_data_json": json.dumps(lowest_questions_chart_data, default=str),
        "access_model_note": (
            "Access is resolved from the current org scope. Historical survey "
            "snapshots are used only to decide what the result is about."
        ),
        "viewer_org_unit_path": " > ".join(get_org_unit_path(viewer_org_unit)),
        "current_target_org_unit_path": " > ".join(get_org_unit_path(current_target_org_unit)),
    }


@app.get("/")
def home(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context={
            "title": "Survey Insights Demo",
            "current_user": get_current_employee(request=request, db=db),
        },
    )


@app.get("/admin")
def admin_overview(request: Request, db: Session = Depends(get_db)):
    current_employee = require_current_employee(request=request, db=db)
    assert_pc_admin(current_employee)
    rows = get_survey_admin_rows(db)
    latest_snapshot = (
        db.query(OrgSnapshot)
        .order_by(OrgSnapshot.snapshot_date.desc())
        .first()
    )

    return templates.TemplateResponse(
        request=request,
        name="admin_overview.html",
        context={
            "title": "P&C Admin",
            "current_user": current_employee,
            "survey_rows": rows,
            "latest_snapshot": latest_snapshot,
        },
    )


@app.get("/admin/surveys/new")
def new_survey_page(request: Request, db: Session = Depends(get_db)):
    current_employee = require_current_employee(request=request, db=db)
    assert_pc_admin(current_employee)
    snapshots = db.query(OrgSnapshot).order_by(OrgSnapshot.snapshot_date.desc()).all()

    return templates.TemplateResponse(
        request=request,
        name="admin_new_survey.html",
        context={
            "title": "Create Survey Cycle",
            "current_user": current_employee,
            "snapshots": snapshots,
            "default_questions": DEFAULT_QUESTIONS,
        },
    )


@app.post("/admin/surveys/new")
def create_survey_cycle(
    request: Request,
    name: str = Form(...),
    cycle_type: str = Form(...),
    starts_on: str = Form(...),
    ends_on: str = Form(...),
    org_snapshot_id: int = Form(...),
    db: Session = Depends(get_db),
):
    assert_pc_admin(require_current_employee(request=request, db=db))

    snapshot = db.query(OrgSnapshot).filter(OrgSnapshot.id == org_snapshot_id).first()

    if not snapshot:
        raise HTTPException(status_code=404, detail="Org snapshot not found")

    if cycle_type not in {"annual", "half_year", "enps_pulse"}:
        raise HTTPException(status_code=400, detail="Invalid cycle type")

    try:
        start_date = date.fromisoformat(starts_on)
        end_date = date.fromisoformat(ends_on)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date format") from exc

    if end_date < start_date:
        raise HTTPException(status_code=400, detail="End date must be after start date")

    cycle = SurveyCycle(
        name=name.strip(),
        cycle_type=cycle_type,
        status="draft",
        starts_on=start_date,
        ends_on=end_date,
        org_snapshot=snapshot,
    )
    db.add(cycle)
    db.flush()

    create_default_questions_for_cycle(db, cycle)
    db.commit()

    return RedirectResponse(url=f"/admin/surveys/{cycle.id}", status_code=303)


@app.get("/admin/surveys/{survey_cycle_id}")
def admin_survey_detail(
    request: Request,
    survey_cycle_id: int,
    db: Session = Depends(get_db),
):
    current_employee = require_current_employee(request=request, db=db)
    assert_pc_admin(current_employee)
    cycle = get_survey_cycle(db, survey_cycle_id)

    if not cycle:
        raise HTTPException(status_code=404, detail="Survey cycle not found")

    row = next(
        (item for item in get_survey_admin_rows(db) if item["cycle"].id == cycle.id),
        None,
    )

    return templates.TemplateResponse(
        request=request,
        name="admin_survey_detail.html",
        context={
            "title": cycle.name,
            "current_user": current_employee,
            "cycle": cycle,
            "row": row,
            "questions": sorted(cycle.questions, key=lambda question: question.display_order),
        },
    )


@app.post("/admin/surveys/{survey_cycle_id}/publish")
def publish_survey_cycle(
    request: Request,
    survey_cycle_id: int,
    db: Session = Depends(get_db),
):
    assert_pc_admin(require_current_employee(request=request, db=db))

    cycle = get_survey_cycle(db, survey_cycle_id)

    if not cycle:
        raise HTTPException(status_code=404, detail="Survey cycle not found")

    if cycle.status != "draft":
        raise HTTPException(status_code=400, detail="Only draft surveys can be published")

    create_participants_for_cycle(db, cycle)
    cycle.status = "active"
    db.commit()

    return RedirectResponse(url=f"/admin/surveys/{cycle.id}", status_code=303)


@app.post("/admin/surveys/{survey_cycle_id}/close")
def close_survey_cycle(
    request: Request,
    survey_cycle_id: int,
    db: Session = Depends(get_db),
):
    assert_pc_admin(require_current_employee(request=request, db=db))

    cycle = get_survey_cycle(db, survey_cycle_id)

    if not cycle:
        raise HTTPException(status_code=404, detail="Survey cycle not found")

    if cycle.status not in {"active", "draft"}:
        raise HTTPException(status_code=400, detail="Survey is already closed")

    cycle.status = "closed"
    db.commit()

    return RedirectResponse(url=f"/admin/surveys/{cycle.id}", status_code=303)


@app.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    featured_names = [
        "Marcus Eriksson",
        "Olivia Ivkovic",
        "David Trajcev",
        "Petra Lindqvist",
    ]

    employees = db.query(Employee).order_by(Employee.full_name).all()
    employee_by_name = {employee.full_name: employee for employee in employees}
    featured_users = [
        employee_by_name[name]
        for name in featured_names
        if name in employee_by_name
    ]

    managers = [employee for employee in employees if employee.is_manager]
    non_managers = [employee for employee in employees if not employee.is_manager]

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "title": "Mock SSO Login",
            "current_user": get_current_employee(request=request, db=db),
            "featured_users": featured_users,
            "managers": managers,
            "employees": non_managers,
        },
    )


@app.post("/login")
def login_submit(
    request: Request,
    employee_id: int = Form(...),
    db: Session = Depends(get_db),
):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()

    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    request.session["employee_id"] = employee.id

    # Land each role on its home: P&C admins go to the admin area, everyone else
    # to their personal area.
    landing_url = "/admin" if employee.is_pc_admin else "/me"

    return RedirectResponse(url=landing_url, status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)


@app.get("/me")
def my_area(request: Request, db: Session = Depends(get_db)):
    current_employee = require_current_employee(request=request, db=db)

    if current_employee.is_manager:
        return RedirectResponse(url=f"/manager/{current_employee.id}", status_code=303)

    return RedirectResponse(url="/employee", status_code=303)


@app.get("/employee")
def employee_home(request: Request, db: Session = Depends(get_db)):
    current_employee = require_current_employee(request=request, db=db)

    participants = (
        db.query(SurveyParticipant)
        .filter(SurveyParticipant.employee_id == current_employee.id)
        .join(SurveyCycle, SurveyParticipant.survey_cycle_id == SurveyCycle.id)
        .order_by(SurveyCycle.starts_on.desc())
        .all()
    )

    participant_rows = []

    for participant in participants:
        participant_rows.append(
            {
                "participant_id": participant.id,
                "survey_cycle": participant.survey_cycle,
                "org_unit_path": " > ".join(get_org_unit_path(participant.org_unit_at_time)),
                "has_submitted": participant.has_submitted,
                "submitted_at": participant.submitted_at,
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="employee_home.html",
        context={
            "title": "My Surveys",
            "current_user": current_employee,
            "participant_rows": participant_rows,
        },
    )


@app.get("/survey/{participant_id}")
def survey_form(
    request: Request,
    participant_id: int,
    db: Session = Depends(get_db),
):
    current_employee = require_current_employee(request=request, db=db)
    participant = get_participant_for_current_employee(
        db=db,
        participant_id=participant_id,
        current_employee=current_employee,
    )

    questions = (
        db.query(Question)
        .filter(Question.survey_cycle_id == participant.survey_cycle_id)
        .order_by(Question.display_order)
        .all()
    )

    return templates.TemplateResponse(
        request=request,
        name="survey_form.html",
        context={
            "title": "Employee Survey",
            "current_user": current_employee,
            "participant": participant,
            "questions": questions,
            "org_unit_path": " > ".join(get_org_unit_path(participant.org_unit_at_time)),
        },
    )


@app.post("/survey/{participant_id}")
async def submit_survey(
    request: Request,
    participant_id: int,
    db: Session = Depends(get_db),
):
    current_employee = require_current_employee(request=request, db=db)
    participant = get_participant_for_current_employee(
        db=db,
        participant_id=participant_id,
        current_employee=current_employee,
    )

    if participant.has_submitted:
        return templates.TemplateResponse(
            request=request,
            name="survey_submitted.html",
            context={
                "title": "Survey Already Submitted",
                "current_user": current_employee,
                "participant": participant,
                "already_submitted": True,
            },
            status_code=409,
        )

    questions = (
        db.query(Question)
        .filter(Question.survey_cycle_id == participant.survey_cycle_id)
        .order_by(Question.display_order)
        .all()
    )

    form_data = await request.form()

    submission = ResponseSubmission(
        anonymous_response_id=str(uuid.uuid4()),
        survey_cycle=participant.survey_cycle,
        org_unit_at_time=participant.org_unit_at_time,
        submitted_bucket=participant.survey_cycle.starts_on.strftime("%Y-%m"),
    )
    db.add(submission)
    db.flush()

    for question in questions:
        field_name = f"question_{question.id}"

        if question.response_type == "text":
            # Free text is not stored in this MVP because comments can identify
            # their author even when numeric scores are aggregated.
            continue

        numeric_value = parse_numeric_answer(
            raw_value=form_data.get(field_name),
            question=question,
        )
        answer = ResponseAnswer(
            submission=submission,
            question=question,
            numeric_value=numeric_value,
        )

        db.add(answer)

    participant.has_submitted = True
    participant.submitted_at = datetime.now(timezone.utc)

    db.commit()

    return templates.TemplateResponse(
        request=request,
        name="survey_submitted.html",
        context={
            "title": "Survey Submitted",
            "current_user": current_employee,
            "participant": participant,
            "already_submitted": False,
        },
    )


@app.get("/managers")
def manager_list(request: Request, db: Session = Depends(get_db)):
    current_employee = require_current_employee(request=request, db=db)

    all_managers = (
        db.query(Employee)
        .filter(Employee.is_manager.is_(True))
        .order_by(Employee.full_name)
        .all()
    )

    if getattr(current_employee, "is_pc_admin", False):
        # P&C admins can open every manager dashboard.
        managers = all_managers
    else:
        viewer_org_unit = get_current_manager_org_unit_or_403(
            db=db,
            current_employee=current_employee,
        )
        managers = []
        for manager in all_managers:
            current_org_unit = get_current_managed_org_unit(db=db, manager_id=manager.id)
            if current_org_unit and is_org_unit_descendant_or_self(
                db=db,
                ancestor=viewer_org_unit,
                candidate=current_org_unit,
            ):
                managers.append(manager)

    cycles = db.query(SurveyCycle).order_by(SurveyCycle.starts_on).all()

    return templates.TemplateResponse(
        request=request,
        name="manager_list.html",
        context={
            "title": "Accessible Manager Dashboards",
            "current_user": current_employee,
            "managers": managers,
            "cycles": cycles,
        },
    )


@app.get("/manager/{manager_id}")
def manager_dashboard(
    request: Request,
    manager_id: int,
    cycle_id: int | None = None,
    db: Session = Depends(get_db),
):
    current_employee = require_current_employee(request=request, db=db)

    context = build_manager_dashboard_context(
        db=db,
        manager_id=manager_id,
        current_employee=current_employee,
        survey_cycle_id=cycle_id,
    )

    context["current_user"] = current_employee

    template_name = context.pop("_template_name", "manager_dashboard.html")

    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context=context,
    )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "app": "survey-insights-demo",
    }


@app.get("/debug/tables")
def debug_tables():
    inspector = inspect(engine)
    return {
        "tables": inspector.get_table_names()
    }


@app.get("/debug/summary")
def debug_summary(db: Session = Depends(get_db)):
    return {
        "employees": db.query(func.count(Employee.id)).scalar(),
        "org_snapshots": db.query(func.count(OrgSnapshot.id)).scalar(),
        "org_units": db.query(func.count(OrgUnitSnapshot.id)).scalar(),
        "org_memberships": db.query(func.count(OrgMembershipSnapshot.id)).scalar(),
        "survey_cycles": db.query(func.count(SurveyCycle.id)).scalar(),
        "questions": db.query(func.count(Question.id)).scalar(),
        "survey_participants": db.query(func.count(SurveyParticipant.id)).scalar(),
        "response_submissions": db.query(func.count(ResponseSubmission.id)).scalar(),
        "response_answers": db.query(func.count(ResponseAnswer.id)).scalar(),
    }


@app.get("/debug/cycles")
def debug_cycles(db: Session = Depends(get_db)):
    cycles = db.query(SurveyCycle).order_by(SurveyCycle.starts_on).all()

    return [
        {
            "id": cycle.id,
            "name": cycle.name,
            "type": cycle.cycle_type,
            "status": cycle.status,
            "starts_on": cycle.starts_on,
            "ends_on": cycle.ends_on,
            "org_snapshot": cycle.org_snapshot.name,
        }
        for cycle in cycles
    ]


@app.get("/debug/managers")
def debug_managers(db: Session = Depends(get_db)):
    managers = (
        db.query(Employee)
        .filter(Employee.is_manager.is_(True))
        .order_by(Employee.full_name)
        .all()
    )

    return [
        {
            "id": manager.id,
            "name": manager.full_name,
            "email": manager.email,
        }
        for manager in managers
    ]

@app.get("/debug/manager/{manager_id}/cycle/{survey_cycle_id}/scope")
def debug_manager_scope(
    manager_id: int,
    survey_cycle_id: int,
    db: Session = Depends(get_db),
):
    cycle = get_survey_cycle(db, survey_cycle_id)

    if not cycle:
        raise HTTPException(status_code=404, detail="Survey cycle not found")

    root_org_unit = get_managed_org_unit_for_cycle(
        db=db,
        manager_id=manager_id,
        survey_cycle_id=survey_cycle_id,
    )

    if not root_org_unit:
        raise HTTPException(
            status_code=404,
            detail="Manager does not manage an org unit in this survey cycle",
        )

    scoped_units = get_descendant_org_units(db, root_org_unit)

    return {
        "survey_cycle": {
            "id": cycle.id,
            "name": cycle.name,
            "org_snapshot": cycle.org_snapshot.name,
        },
        "manager_id": manager_id,
        "root_org_unit": {
            "id": root_org_unit.id,
            "name": root_org_unit.name,
            "path": " > ".join(get_org_unit_path(root_org_unit)),
        },
        "scoped_org_units": [
            {
                "id": unit.id,
                "name": unit.name,
                "path": " > ".join(get_org_unit_path(unit)),
                "parent_id": unit.parent_id,
                "manager_id": unit.manager_employee_id,
            }
            for unit in scoped_units
        ],
    }


@app.get("/debug/manager/{manager_id}/cycle/{survey_cycle_id}/raw-report")
def debug_manager_raw_report(
    manager_id: int,
    survey_cycle_id: int,
    db: Session = Depends(get_db),
):
    report = get_manager_cycle_report(
        db=db,
        manager_id=manager_id,
        survey_cycle_id=survey_cycle_id,
    )

    if not report:
        raise HTTPException(
            status_code=404,
            detail="No report found for this manager and survey cycle",
        )

    return report


@app.get("/debug/manager/{manager_id}/trends")
def debug_manager_trends(
    manager_id: int,
    db: Session = Depends(get_db),
):
    trends = get_manager_trends(
        db=db,
        manager_id=manager_id,
    )

    if not trends:
        raise HTTPException(
            status_code=404,
            detail="No trends found for this manager",
        )

    return trends

@app.get("/debug/manager/{manager_id}/cycle/{survey_cycle_id}/safe-report")
def debug_manager_safe_report(
    manager_id: int,
    survey_cycle_id: int,
    db: Session = Depends(get_db),
):
    raw_report = get_manager_cycle_report(
        db=db,
        manager_id=manager_id,
        survey_cycle_id=survey_cycle_id,
    )

    if not raw_report:
        raise HTTPException(
            status_code=404,
            detail="No report found for this manager and survey cycle",
        )

    return apply_privacy_to_manager_report(raw_report)


@app.get("/debug/manager/{manager_id}/safe-trends")
def debug_manager_safe_trends(
    manager_id: int,
    db: Session = Depends(get_db),
):
    raw_trends = get_manager_trends(
        db=db,
        manager_id=manager_id,
    )

    if not raw_trends:
        raise HTTPException(
            status_code=404,
            detail="No trends found for this manager",
        )

    return apply_privacy_to_trends(raw_trends)
