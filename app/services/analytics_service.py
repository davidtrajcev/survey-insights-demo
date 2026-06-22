from collections import defaultdict
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Question, ResponseAnswer, ResponseSubmission, OrgUnitSnapshot, SurveyParticipant
from app.services.org_service import (
    get_descendant_org_units,
    get_org_unit_by_external_key_for_cycle,
    get_org_unit_path,
    get_relative_depth,
    get_survey_cycle,
)


CATEGORY_ORDER = [
    "Health",
    "Workload",
    "Leadership",
    "Collaboration",
    "eNPS",
]


def _category_sort_key(category: str) -> int:
    try:
        return CATEGORY_ORDER.index(category)
    except ValueError:
        return len(CATEGORY_ORDER)


def get_submission_count(
    db: Session,
    survey_cycle_id: int,
    org_unit_ids: list[int],
) -> int:
    if not org_unit_ids:
        return 0

    return (
        db.query(func.count(func.distinct(ResponseSubmission.id)))
        .filter(
            ResponseSubmission.survey_cycle_id == survey_cycle_id,
            ResponseSubmission.org_unit_id_at_time.in_(org_unit_ids),
        )
        .scalar()
        or 0
    )


def get_eligible_count(
    db: Session,
    survey_cycle_id: int,
    org_unit_ids: list[int],
) -> int:
    """Number of eligible participants in a scope — the denominator for response rate."""

    if not org_unit_ids:
        return 0

    return (
        db.query(func.count(SurveyParticipant.id))
        .filter(
            SurveyParticipant.survey_cycle_id == survey_cycle_id,
            SurveyParticipant.org_unit_id_at_time.in_(org_unit_ids),
        )
        .scalar()
        or 0
    )


def get_category_scores(
    db: Session,
    survey_cycle_id: int,
    org_unit_ids: list[int],
) -> list[dict[str, Any]]:
    """
    Aggregates category scores for a set of org units.

    For Likert questions:
    - average_score is the average 1-5 score.

    For eNPS:
    - average_score is also shown for demo readability.
    - enps_score is calculated as % promoters - % detractors.
    """

    if not org_unit_ids:
        return []

    rows = (
        db.query(
            Question.category,
            Question.weight,
            ResponseSubmission.id.label("submission_id"),
            ResponseAnswer.numeric_value,
        )
        .join(ResponseAnswer, ResponseAnswer.question_id == Question.id)
        .join(ResponseSubmission, ResponseSubmission.id == ResponseAnswer.submission_id)
        .filter(
            ResponseSubmission.survey_cycle_id == survey_cycle_id,
            ResponseSubmission.org_unit_id_at_time.in_(org_unit_ids),
            ResponseAnswer.numeric_value.isnot(None),
        )
        .all()
    )

    # (value, weight) pairs per category so the category score is a weighted
    # mean of its questions. respondent_count stays a count of distinct
    # submissions, so question weights never affect the <4 anonymity threshold.
    weighted_values_by_category: dict[str, list[tuple[float, float]]] = defaultdict(list)
    submissions_by_category: dict[str, set[int]] = defaultdict(set)

    for category, weight, submission_id, numeric_value in rows:
        weighted_values_by_category[category].append((float(numeric_value), float(weight)))
        submissions_by_category[category].add(int(submission_id))

    results = []

    for category, weighted_values in weighted_values_by_category.items():
        respondent_count = len(submissions_by_category[category])
        total_weight = sum(weight for _, weight in weighted_values) or 1.0
        average_score = round(
            sum(value * weight for value, weight in weighted_values) / total_weight, 1
        )

        result = {
            "category": category,
            "respondent_count": respondent_count,
            "average_score": average_score,
        }

        if category == "eNPS":
            # eNPS is promoter/detractor share, an unweighted count of responses.
            values = [value for value, _ in weighted_values]
            promoters = len([value for value in values if value >= 9])
            detractors = len([value for value in values if value <= 6])

            enps_score = round(
                ((promoters / len(values)) - (detractors / len(values))) * 100
            )

            result["enps_score"] = enps_score
            result["promoters"] = promoters
            result["detractors"] = detractors

        results.append(result)

    return sorted(
        results,
        key=lambda item: _category_sort_key(item["category"]),
    )


def get_question_breakdown(
    db: Session,
    survey_cycle_id: int,
    org_unit_ids: list[int],
) -> list[dict[str, Any]]:
    """
    Per-question average for a set of org units (the scores behind each category).

    Each question carries its own respondent count so suppression can be applied
    independently: with optional questions, one question could fall below the
    threshold even when its category clears it.
    """

    if not org_unit_ids:
        return []

    rows = (
        db.query(
            Question.question_key,
            Question.category,
            Question.text,
            Question.weight,
            Question.display_order,
            ResponseSubmission.id.label("submission_id"),
            ResponseAnswer.numeric_value,
        )
        .join(ResponseAnswer, ResponseAnswer.question_id == Question.id)
        .join(ResponseSubmission, ResponseSubmission.id == ResponseAnswer.submission_id)
        .filter(
            ResponseSubmission.survey_cycle_id == survey_cycle_id,
            ResponseSubmission.org_unit_id_at_time.in_(org_unit_ids),
            ResponseAnswer.numeric_value.isnot(None),
        )
        .all()
    )

    by_question: dict[str, dict[str, Any]] = {}

    for question_key, category, text, weight, display_order, submission_id, numeric_value in rows:
        entry = by_question.setdefault(
            question_key,
            {
                "question_key": question_key,
                "category": category,
                "text": text,
                "weight": float(weight),
                "display_order": display_order,
                "values": [],
                "submissions": set(),
            },
        )
        entry["values"].append(float(numeric_value))
        entry["submissions"].add(int(submission_id))

    breakdown = []

    for entry in by_question.values():
        breakdown.append(
            {
                "question_key": entry["question_key"],
                "category": entry["category"],
                "text": entry["text"],
                "weight": entry["weight"],
                "display_order": entry["display_order"],
                "average_score": round(sum(entry["values"]) / len(entry["values"]), 1),
                "respondent_count": len(entry["submissions"]),
            }
        )

    breakdown.sort(key=lambda item: item["display_order"])

    return breakdown


def get_unit_rollup(
    db: Session,
    survey_cycle_id: int,
    org_unit: OrgUnitSnapshot,
) -> dict[str, Any]:
    """
    Aggregates responses for one org unit including all child org units below it.

    Example:
    Engineering rollup includes:
    - Engineering direct responses
    - Platform Team
    - Data Team
    - AI Lab
    - Product Engineering
    """

    descendant_units = get_descendant_org_units(db, org_unit)
    descendant_unit_ids = [unit.id for unit in descendant_units]

    direct_unit_ids = [org_unit.id]

    return {
        "org_unit_id": org_unit.id,
        "org_unit_external_key": org_unit.external_key,
        "org_unit_name": org_unit.name,
        "org_unit_path": " > ".join(get_org_unit_path(org_unit)),
        "direct_respondent_count": get_submission_count(
            db=db,
            survey_cycle_id=survey_cycle_id,
            org_unit_ids=direct_unit_ids,
        ),
        "rolled_up_respondent_count": get_submission_count(
            db=db,
            survey_cycle_id=survey_cycle_id,
            org_unit_ids=descendant_unit_ids,
        ),
        "direct_category_scores": get_category_scores(
            db=db,
            survey_cycle_id=survey_cycle_id,
            org_unit_ids=direct_unit_ids,
        ),
        "rolled_up_category_scores": get_category_scores(
            db=db,
            survey_cycle_id=survey_cycle_id,
            org_unit_ids=descendant_unit_ids,
        ),
        "descendant_org_unit_ids": descendant_unit_ids,
    }


def get_org_unit_cycle_report(
    db: Session,
    root_org_unit: OrgUnitSnapshot,
    survey_cycle_id: int,
    manager_id: int | None = None,
) -> dict[str, Any] | None:
    """
    Main raw analytics function for a resolved org unit.

    This raw layer calculates counts and scores only. Privacy suppression is
    applied later in privacy_service before anything is rendered to managers.
    """

    cycle = get_survey_cycle(db, survey_cycle_id)

    if not cycle:
        return None

    if root_org_unit.snapshot_id != cycle.org_snapshot_id:
        return None

    scoped_units = get_descendant_org_units(db, root_org_unit)

    org_unit_summaries = []

    for unit in scoped_units:
        summary = get_unit_rollup(
            db=db,
            survey_cycle_id=survey_cycle_id,
            org_unit=unit,
        )

        summary["depth"] = get_relative_depth(root_org_unit, unit)
        summary["parent_id"] = unit.parent_id
        summary["manager_employee_id"] = unit.manager_employee_id
        # Opaque id for building /manager/<public_id> links without leaking the
        # sequential employee id.
        summary["manager_public_id"] = unit.manager.public_id if unit.manager else None

        org_unit_summaries.append(summary)

    root_rollup = get_unit_rollup(
        db=db,
        survey_cycle_id=survey_cycle_id,
        org_unit=root_org_unit,
    )

    return {
        "survey_cycle": {
            "id": cycle.id,
            "name": cycle.name,
            "type": cycle.cycle_type,
            "starts_on": cycle.starts_on,
            "ends_on": cycle.ends_on,
            "org_snapshot": cycle.org_snapshot.name,
        },
        "manager_scope": {
            "manager_id": manager_id,
            "root_org_unit_id": root_org_unit.id,
            "root_org_unit_external_key": root_org_unit.external_key,
            "root_org_unit_name": root_org_unit.name,
            "root_org_unit_path": " > ".join(get_org_unit_path(root_org_unit)),
            "scoped_org_unit_count": len(scoped_units),
        },
        "overall_rollup": root_rollup,
        "org_units": org_unit_summaries,
    }


def get_company_benchmark(
    db: Session,
    survey_cycle_id: int,
) -> dict[str, Any] | None:
    """
    Company-wide rollup for a cycle, used as a comparison baseline.

    The baseline is the snapshot root (the org unit with no parent), aggregated
    over every descendant. This is a large aggregate and a *score* (not a count),
    so comparing a manager's visible score against it does not enable the
    differencing attack that protects small teams. Suppression is still applied
    later in privacy_service before any value is shown.
    """

    cycle = get_survey_cycle(db, survey_cycle_id)

    if not cycle:
        return None

    root_org_unit = (
        db.query(OrgUnitSnapshot)
        .filter(
            OrgUnitSnapshot.snapshot_id == cycle.org_snapshot_id,
            OrgUnitSnapshot.parent_id.is_(None),
        )
        .first()
    )

    if not root_org_unit:
        return None

    rollup = get_unit_rollup(
        db=db,
        survey_cycle_id=survey_cycle_id,
        org_unit=root_org_unit,
    )

    return {
        "company_org_unit_id": root_org_unit.id,
        "company_org_unit_name": root_org_unit.name,
        "rolled_up_respondent_count": rollup["rolled_up_respondent_count"],
        "rolled_up_category_scores": rollup["rolled_up_category_scores"],
    }


def get_enps_pulse_trend(
    db: Session,
    current_org_unit_external_key: str,
) -> list[dict[str, Any]]:
    """
    eNPS net score for one org unit across every monthly pulse.

    eNPS runs as a continuous monthly pulse, separate from the annual/half-year
    survey. The current org unit is mapped into each pulse's snapshot by
    external_key, the same way survey trends are.
    """

    from app.models import SurveyCycle

    # Draft pulses aren't published, so they never appear in the eNPS trend.
    pulses = (
        db.query(SurveyCycle)
        .filter(
            SurveyCycle.cycle_type == "enps_pulse",
            SurveyCycle.status.in_(["active", "closed"]),
        )
        .order_by(SurveyCycle.starts_on)
        .all()
    )

    rows = []

    for pulse in pulses:
        root_org_unit = get_org_unit_by_external_key_for_cycle(
            db=db,
            external_key=current_org_unit_external_key,
            survey_cycle_id=pulse.id,
        )

        if not root_org_unit:
            continue

        descendant_unit_ids = [unit.id for unit in get_descendant_org_units(db, root_org_unit)]

        scores = get_category_scores(
            db=db,
            survey_cycle_id=pulse.id,
            org_unit_ids=descendant_unit_ids,
        )
        enps = next((score for score in scores if score["category"] == "eNPS"), None)

        rows.append(
            {
                "survey_cycle_id": pulse.id,
                "survey_cycle_name": pulse.name,
                "starts_on": pulse.starts_on,
                "root_org_unit_name": root_org_unit.name,
                "respondent_count": get_submission_count(
                    db=db,
                    survey_cycle_id=pulse.id,
                    org_unit_ids=descendant_unit_ids,
                ),
                "enps_score": enps["enps_score"] if enps else None,
            }
        )

    return rows


def get_org_unit_trends(
    db: Session,
    current_org_unit_external_key: str,
) -> list[dict[str, Any]]:
    """
    Calculates trend data for the same business org unit across cycles.

    Access is decided from the current org elsewhere. This function maps that
    current org unit into each historical survey snapshot by external_key so
    org attribution stays historical while access follows today's ownership.
    """

    from app.models import SurveyCycle

    # Draft cycles aren't published, so they never appear in a manager's trend.
    cycles = (
        db.query(SurveyCycle)
        .filter(
            SurveyCycle.cycle_type.in_(["annual", "half_year"]),
            SurveyCycle.status.in_(["active", "closed"]),
        )
        .order_by(SurveyCycle.starts_on)
        .all()
    )

    trend_rows = []

    for cycle in cycles:
        root_org_unit = get_org_unit_by_external_key_for_cycle(
            db=db,
            external_key=current_org_unit_external_key,
            survey_cycle_id=cycle.id,
        )

        if not root_org_unit:
            continue

        descendant_units = get_descendant_org_units(db, root_org_unit)
        descendant_unit_ids = [unit.id for unit in descendant_units]

        category_scores = get_category_scores(
            db=db,
            survey_cycle_id=cycle.id,
            org_unit_ids=descendant_unit_ids,
        )

        trend_rows.append(
            {
                "survey_cycle_id": cycle.id,
                "survey_cycle_name": cycle.name,
                "starts_on": cycle.starts_on,
                "root_org_unit_name": root_org_unit.name,
                "respondent_count": get_submission_count(
                    db=db,
                    survey_cycle_id=cycle.id,
                    org_unit_ids=descendant_unit_ids,
                ),
                "category_scores": category_scores,
            }
        )

    return trend_rows

