from copy import deepcopy
from typing import Any


MIN_RESPONDENTS = 4
SECONDARY_SUPPRESSION_REASON = "Hidden to protect a smaller team"


def is_visible_count(respondent_count: int | None) -> bool:
    """
    A result can only be shown when it has at least MIN_RESPONDENTS.

    The threshold is based on submitted responses, not team headcount.
    """

    return respondent_count is not None and respondent_count >= MIN_RESPONDENTS


def hidden_result(reason: str = "Hidden due to anonymity threshold") -> dict[str, Any]:
    return {
        "visible": False,
        "reason": reason,
    }


def apply_threshold_to_category_scores(
    category_scores: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Suppress individual category scores when the category has fewer than
    MIN_RESPONDENTS.

    In the current seed data, every respondent answers every question, so the
    category respondent count usually equals the unit respondent count. Keeping
    this check makes the model safer for optional questions later.
    """

    safe_scores = []

    for score in category_scores:
        respondent_count = score.get("respondent_count", 0)
        category = score.get("category")

        if not is_visible_count(respondent_count):
            safe_scores.append(
                {
                    "category": category,
                    "visible": False,
                    "respondent_count_label": f"<{MIN_RESPONDENTS}",
                    "reason": "Hidden because fewer than 4 respondents answered this category",
                }
            )
            continue

        safe_score = {
            "category": category,
            "visible": True,
            "respondent_count": respondent_count,
            "average_score": score.get("average_score"),
        }

        if category == "eNPS":
            safe_score["enps_score"] = score.get("enps_score")
            safe_score["promoters"] = score.get("promoters")
            safe_score["detractors"] = score.get("detractors")

        safe_scores.append(safe_score)

    return safe_scores


def apply_threshold_to_question_breakdown(
    raw_breakdown: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """
    Suppresses per-question scores below the threshold and groups them by category.

    Each question is checked independently. A question's score is shown only when
    that question itself has at least MIN_RESPONDENTS answers, so the breakdown
    can never expose a sub-threshold aggregate even if a category clears the
    threshold on the union of its questions.
    """

    by_category: dict[str, list[dict[str, Any]]] = {}

    for question in raw_breakdown:
        category = question.get("category")
        respondent_count = question.get("respondent_count", 0)
        weight = float(question.get("weight", 1.0))
        weight_label = f"×{int(weight)}" if weight.is_integer() else f"×{weight:g}"

        safe_question = {
            "text": question.get("text"),
            "category": category,
            "weight": weight,
            "weight_label": weight_label,
            "visible": is_visible_count(respondent_count),
        }

        if safe_question["visible"]:
            safe_question["average_score"] = question.get("average_score")
        else:
            safe_question["respondent_count_label"] = f"<{MIN_RESPONDENTS}"
            safe_question["reason"] = (
                f"Hidden because fewer than {MIN_RESPONDENTS} answered this question"
            )

        by_category.setdefault(category, []).append(safe_question)

    return by_category


def apply_threshold_to_rollup(
    rollup: dict[str, Any],
    count_field: str = "rolled_up_respondent_count",
    scores_field: str = "rolled_up_category_scores",
) -> dict[str, Any]:
    """
    Converts a raw rollup into a privacy-safe rollup.

    If the rollup has fewer than MIN_RESPONDENTS, scores are removed and the
    exact respondent count is not returned.
    """

    respondent_count = rollup.get(count_field, 0)

    safe_rollup = {
        "org_unit_id": rollup.get("org_unit_id"),
        "org_unit_external_key": rollup.get("org_unit_external_key"),
        "org_unit_name": rollup.get("org_unit_name"),
        "org_unit_path": rollup.get("org_unit_path"),
        "visible": is_visible_count(respondent_count),
        # Keep internally for privacy calculations only. The template should use
        # respondent_count/respondent_count_label for display.
        "_raw_respondent_count": respondent_count,
    }

    if not safe_rollup["visible"]:
        safe_rollup.update(
            {
                "respondent_count_label": f"<{MIN_RESPONDENTS}",
                "category_scores": [],
                "reason": "Hidden because fewer than 4 people responded",
            }
        )
        return safe_rollup

    safe_rollup.update(
        {
            "respondent_count": respondent_count,
            "category_scores": apply_threshold_to_category_scores(
                rollup.get(scores_field, [])
            ),
        }
    )

    return safe_rollup


def _children_by_parent_id(
    safe_org_units: list[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    children_by_parent_id: dict[int, list[dict[str, Any]]] = {}

    for unit in safe_org_units:
        parent_id = unit.get("parent_id")
        if parent_id is None:
            continue

        children_by_parent_id.setdefault(parent_id, [])
        children_by_parent_id[parent_id].append(unit)

    return children_by_parent_id


def _suppress_unit_for_secondary_privacy(
    unit: dict[str, Any],
    protected_child_name: str,
    parent_name: str,
) -> None:
    """
    Hide a visible sibling so the hidden child is no longer uniquely inferable.

    We keep the raw count internally so the suppression algorithm can continue
    to pick the smallest visible sibling on later passes, but the UI no longer
    receives scores or exact visible counts for that sibling.
    """

    unit["visible"] = False
    unit.pop("respondent_count", None)
    unit["respondent_count_label"] = "Suppressed"
    unit["category_scores"] = []
    unit["reason"] = SECONDARY_SUPPRESSION_REASON
    unit["secondary_suppression"] = True
    unit["protected_child_org_unit_name"] = protected_child_name
    unit["privacy_note"] = (
        f"Also hidden because {protected_child_name} would otherwise be inferable "
        f"from the {parent_name} total and visible sibling rows."
    )


def apply_secondary_suppression_to_org_units(
    safe_org_units: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Closes the simplest differencing leak.

    If a visible parent has exactly one hidden immediate child, a manager can
    subtract the visible children from the parent total to recover the hidden
    child. To prevent that, hide the smallest visible sibling too.

    This runs to a fixpoint because suppressing one sibling can create a new
    lone-hidden-child situation higher up the tree.
    """

    risks = []

    while True:
        units_by_id = {
            unit["org_unit_id"]: unit
            for unit in safe_org_units
            if unit.get("org_unit_id") is not None
        }
        children_by_parent_id = _children_by_parent_id(safe_org_units)

        suppression_applied = False

        for parent_id, children in children_by_parent_id.items():
            parent = units_by_id.get(parent_id)

            if not parent or not parent.get("visible"):
                continue

            hidden_children = [child for child in children if not child.get("visible")]
            visible_children = [child for child in children if child.get("visible")]

            if len(hidden_children) != 1 or not visible_children:
                continue

            hidden_child = hidden_children[0]
            sibling_to_hide = min(
                visible_children,
                key=lambda child: child.get("_raw_respondent_count", 10**9),
            )

            _suppress_unit_for_secondary_privacy(
                unit=sibling_to_hide,
                protected_child_name=hidden_child.get("org_unit_name", "the hidden team"),
                parent_name=parent.get("org_unit_name", "the parent org"),
            )

            risks.append(
                {
                    "parent_org_unit_id": parent_id,
                    "parent_org_unit_name": parent.get("org_unit_name"),
                    "hidden_child_org_unit_id": hidden_child.get("org_unit_id"),
                    "hidden_child_org_unit_name": hidden_child.get("org_unit_name"),
                    "secondary_hidden_org_unit_id": sibling_to_hide.get("org_unit_id"),
                    "secondary_hidden_org_unit_name": sibling_to_hide.get("org_unit_name"),
                    "risk": "A single hidden child would be inferable from the visible parent total and sibling rows.",
                    "mitigation_applied": (
                        f"Also hid {sibling_to_hide.get('org_unit_name')} to protect "
                        f"{hidden_child.get('org_unit_name')}."
                    ),
                }
            )

            parent.setdefault("privacy_warnings", [])
            parent["privacy_warnings"].append(
                "Secondary suppression applied to prevent subtraction inference."
            )

            suppression_applied = True
            break

        if not suppression_applied:
            return risks


def apply_privacy_to_manager_report(
    raw_report: dict[str, Any],
) -> dict[str, Any]:
    """
    Takes the raw manager report from analytics_service and returns the version
    that is safe to show in a manager dashboard.
    """

    report = deepcopy(raw_report)

    safe_org_units = []

    for unit in report.get("org_units", []):
        safe_unit = apply_threshold_to_rollup(
            rollup=unit,
            count_field="rolled_up_respondent_count",
            scores_field="rolled_up_category_scores",
        )
        safe_unit["depth"] = unit.get("depth", 0)
        safe_unit["parent_id"] = unit.get("parent_id")
        safe_unit["manager_employee_id"] = unit.get("manager_employee_id")
        safe_org_units.append(safe_unit)

    inference_risks = apply_secondary_suppression_to_org_units(safe_org_units)

    safe_report = {
        "survey_cycle": report.get("survey_cycle"),
        "manager_scope": report.get("manager_scope"),
        "privacy_policy": {
            "minimum_respondents": MIN_RESPONDENTS,
            "threshold_basis": "submitted responses per visible result, not team headcount",
            "identity_model": "SSO is used for eligibility and duplicate prevention; anonymous response rows do not store employee identity or exact submission timestamps.",
            "navigation_model": "If a team is hidden as secondary suppression in a manager's broader scope, that same manager cannot open the hidden team's standalone dashboard for that cycle.",
            "mvp_note": "This demo applies primary threshold suppression plus recursive secondary suppression for the lone-hidden-child differencing attack. Production should also add exclusion-size and segment-overlap protections.",
        },
        "overall_rollup": apply_threshold_to_rollup(
            rollup=report.get("overall_rollup", {}),
            count_field="rolled_up_respondent_count",
            scores_field="rolled_up_category_scores",
        ),
        "org_units": safe_org_units,
        "inference_risks": inference_risks,
    }

    return safe_report


def apply_privacy_to_trends(
    raw_trends: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Applies the threshold to trend rows.

    If a manager's scoped org has fewer than MIN_RESPONDENTS in a cycle, that
    cycle's scores are hidden from the trend line.
    """

    safe_trends = []

    for row in raw_trends:
        respondent_count = row.get("respondent_count", 0)
        visible = is_visible_count(respondent_count)

        safe_row = {
            "survey_cycle_id": row.get("survey_cycle_id"),
            "survey_cycle_name": row.get("survey_cycle_name"),
            "starts_on": row.get("starts_on"),
            "root_org_unit_name": row.get("root_org_unit_name"),
            "visible": visible,
        }

        if not visible:
            safe_row.update(
                {
                    "respondent_count_label": f"<{MIN_RESPONDENTS}",
                    "category_scores": [],
                    "reason": "Hidden because fewer than 4 people responded in this cycle",
                }
            )
        else:
            safe_row.update(
                {
                    "respondent_count": respondent_count,
                    "category_scores": apply_threshold_to_category_scores(
                        row.get("category_scores", [])
                    ),
                }
            )

        safe_trends.append(safe_row)

    return safe_trends


def apply_privacy_to_enps_pulse(
    raw_pulse_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Applies the threshold to each monthly eNPS pulse independently.

    A pulse has one question, so its respondent count is the submission count;
    pulses below MIN_RESPONDENTS for the scope are hidden, same as any aggregate.
    """

    safe_rows = []

    for row in raw_pulse_rows:
        respondent_count = row.get("respondent_count", 0)
        visible = is_visible_count(respondent_count)

        safe_row = {
            "survey_cycle_id": row.get("survey_cycle_id"),
            "survey_cycle_name": row.get("survey_cycle_name"),
            "starts_on": row.get("starts_on"),
            "root_org_unit_name": row.get("root_org_unit_name"),
            "visible": visible,
        }

        if visible:
            safe_row["enps_score"] = row.get("enps_score")
            safe_row["respondent_count"] = respondent_count
        else:
            safe_row["respondent_count_label"] = f"<{MIN_RESPONDENTS}"
            safe_row["reason"] = (
                f"Hidden because fewer than {MIN_RESPONDENTS} people responded in this pulse"
            )

        safe_rows.append(safe_row)

    return safe_rows


def build_company_comparison(
    scope_category_scores: list[dict[str, Any]],
    raw_company_category_scores: list[dict[str, Any]],
    company_name: str = "Company",
) -> dict[str, Any]:
    """
    Compares a manager's visible scores against the company-wide baseline.

    Both sides are passed through the threshold first: a score is only compared
    when the manager's scope AND the company baseline are independently visible.
    We compare averages/eNPS (not respondent counts), and only against the large
    company aggregate — never against a specific small sibling — so the
    comparison cannot be used to recover a suppressed team.
    """

    safe_company = apply_threshold_to_category_scores(raw_company_category_scores)

    company_by_category = {
        score["category"]: score for score in safe_company if score.get("visible")
    }
    scope_by_category = {
        score["category"]: score
        for score in (scope_category_scores or [])
        if score.get("visible")
    }

    categories: dict[str, Any] = {}

    for category, scope_score in scope_by_category.items():
        company_score = company_by_category.get(category)

        if not company_score:
            continue

        is_enps = category == "eNPS"
        value_field = "enps_score" if is_enps else "average_score"

        scope_value = scope_score.get(value_field)
        company_value = company_score.get(value_field)

        if scope_value is None or company_value is None:
            continue

        raw_delta = scope_value - company_value
        delta = int(round(raw_delta)) if is_enps else round(raw_delta, 1)

        if delta > 0:
            direction = "above"
        elif delta < 0:
            direction = "below"
        else:
            direction = "inline"

        categories[category] = {
            "benchmark": company_value,
            "delta": delta,
            "delta_label": f"+{delta}" if delta > 0 else str(delta),
            "direction": direction,
            "is_enps": is_enps,
        }

    return {
        "company_name": company_name,
        "respondent_count": None,
        "categories": categories,
    }
