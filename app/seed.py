from datetime import date, datetime, timedelta, timezone
import random
import uuid

from app.database import Base, SessionLocal, engine
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


random.seed(42)


# Several areas now have multiple questions with different weights so question
# weighting is actually visible: the category score is a weighted mean, and the
# higher-weighted question (e.g. "I can manage stress", "my workload is
# manageable") pulls the area score toward itself.
QUESTIONS = [
    {
        "key": "employee_health",
        "category": "Health",
        "text": "I feel healthy and energized at work.",
        "response_type": "likert_1_5",
        "order": 1,
        "weight": 1.0,
    },
    {
        "key": "health_stress",
        "category": "Health",
        "text": "I can manage work-related stress.",
        "response_type": "likert_1_5",
        "order": 2,
        "weight": 2.0,
    },
    {
        "key": "health_balance",
        "category": "Health",
        "text": "I have a healthy work-life balance.",
        "response_type": "likert_1_5",
        "order": 3,
        "weight": 1.0,
    },
    {
        "key": "workload_balance",
        "category": "Workload",
        "text": "My workload is manageable.",
        "response_type": "likert_1_5",
        "order": 4,
        "weight": 2.0,
    },
    {
        "key": "workload_expectations",
        "category": "Workload",
        "text": "The expectations placed on me are realistic.",
        "response_type": "likert_1_5",
        "order": 5,
        "weight": 1.0,
    },
    {
        "key": "workload_resources",
        "category": "Workload",
        "text": "I have the time and resources to do my job well.",
        "response_type": "likert_1_5",
        "order": 6,
        "weight": 1.0,
    },
    {
        "key": "leadership_support",
        "category": "Leadership",
        "text": "I get the support I need from my manager.",
        "response_type": "likert_1_5",
        "order": 7,
        "weight": 1.0,
    },
    {
        "key": "leadership_clarity",
        "category": "Leadership",
        "text": "My manager sets a clear direction for the team.",
        "response_type": "likert_1_5",
        "order": 8,
        "weight": 2.0,
    },
    {
        "key": "leadership_recognition",
        "category": "Leadership",
        "text": "I receive recognition for doing good work.",
        "response_type": "likert_1_5",
        "order": 9,
        "weight": 1.0,
    },
    {
        "key": "team_collaboration",
        "category": "Collaboration",
        "text": "Collaboration in my team works well.",
        "response_type": "likert_1_5",
        "order": 10,
        "weight": 1.0,
    },
    {
        "key": "collab_trust",
        "category": "Collaboration",
        "text": "There is trust and psychological safety in my team.",
        "response_type": "likert_1_5",
        "order": 11,
        "weight": 2.0,
    },
    {
        "key": "collab_crossteam",
        "category": "Collaboration",
        "text": "Collaboration across teams works well.",
        "response_type": "likert_1_5",
        "order": 12,
        "weight": 1.0,
    },
]

# question_key -> category, used by the deterministic scorer so org-level
# adjustments are applied per category while baselines vary per question.
QUESTION_CATEGORY = {question["key"]: question["category"] for question in QUESTIONS}


# eNPS is NOT part of the annual/half-year survey — it runs only as a separate
# continuous monthly pulse, a single 0-10 question on its own cadence.
ENPS_PULSE_QUESTION = {
    "key": "enps",
    "category": "eNPS",
    "text": "How likely are you to recommend this company as a place to work?",
    "response_type": "enps_0_10",
    "order": 1,
    "weight": 1.0,
}

ENPS_PULSE_COUNT = 12
ENPS_PULSE_BASELINE_START = 5.8
ENPS_PULSE_BASELINE_END = 8.7

# Per-org eNPS adjustment for the pulse scorer (mirrors the survey eNPS spread).
ENPS_ORG_ADJUSTMENT = {
    "engineering": 0.2,
    "platform": 0.5,
    "data": -0.4,
    "ai_lab": -0.8,
    "product_engineering": 0.0,
    "sales": 0.0,
    "enterprise_sales": 0.3,
    "smb_sales": -0.6,
    "customer_operations": 0.0,
    "core_support": -0.1,
    "customer_success": 0.6,
    "cx_research_pod": -0.9,
    "engineering_leadership": 0.4,
    "revenue_leadership": 0.2,
    "customer_operations_leadership": 0.3,
}

# Submitted responses per team — tuned to ~70-80% of each team's size (the case's
# stated response rate), while preserving the suppression narrative: AI Lab, SMB
# Sales and the CX Research Pod stay below 4 (hidden). Managers report in their
# department's Leadership cohort, so every team here counts ICs only. Shared
# across all cycles (cx_research_pod only applies in snapshots that include it).
RESPONSE_TARGETS = {
    # Company root: the CIO abstains, so there's no lone direct response to recover.
    "company": 0,
    # Department leadership cohorts (department + team managers, plus staff).
    "engineering_leadership": 4,            # 4 of 5
    "revenue_leadership": 4,                # 4 of 5
    "customer_operations_leadership": 4,    # 4 of 4-5
    # Engineering teams (ICs only).
    "platform": 4,             # 4 of 6
    "data": 4,                 # 4 of 6
    "ai_lab": 2,               # 2 of 2   hidden (<4)
    "product_engineering": 4,  # 4 of 6
    # Revenue teams (ICs only).
    "enterprise_sales": 4,     # 4 of 5
    "smb_sales": 2,            # 2 of 2   hidden (<4)
    # Customer Operations teams (ICs only).
    "core_support": 14,        # 14 of 19
    "customer_success": 12,    # 12 of 16
    "cx_research_pod": 2,      # 2 of 3   hidden (<4)
}


# Employee codes that are People & Culture admins (pure admin identities — not
# part of any org unit, no surveys of their own).
PC_ADMIN_CODES = {"E100"}


EMPLOYEES = [
    # People & Culture (pure admin, not in any org unit)
    ("E100", "Petra Lindqvist", "petra@example.com", False),

    # CIO — owns the company root, so the whole org rolls up to them.
    ("E200", "Magnus Ahlberg", "magnus@example.com", True),

    # Senior managers
    ("E001", "Olivia Ivkovic", "olivia@example.com", True),
    ("E002", "Julia Lind", "julia@example.com", True),

    # Engineering managers
    ("E003", "Alex Berg", "alex@example.com", True),
    ("E004", "Priya Shah", "priya@example.com", True),
    ("E005", "Noah Jensen", "noah@example.com", True),
    ("E006", "Emma Novak", "emma@example.com", True),

    # Sales managers
    ("E007", "Ivan Petrov", "ivan@example.com", True),
    ("E008", "Lena Svensson", "lena@example.com", True),

    # Platform Team
    ("E009", "David Trajcev", "david@example.com", False),
    ("E010", "Marta Holm", "marta@example.com", False),
    ("E011", "Erik Larsson", "erik@example.com", False),
    ("E012", "Sofia Andersson", "sofia@example.com", False),
    ("E013", "Leo Nilsson", "leo@example.com", False),

    # Data Team
    ("E014", "Ana Petrova", "ana@example.com", False),
    ("E015", "Stefan Miles", "stefan@example.com", False),
    ("E016", "Lina Omar", "lina@example.com", False),
    ("E017", "Omar Haddad", "omar@example.com", False),

    # AI Lab — intentionally small
    ("E018", "Kim Park", "kim@example.com", False),
    ("E019", "Sara Wong", "sara@example.com", False),

    # Product Engineering
    ("E020", "Jonas Meyer", "jonas@example.com", False),
    ("E021", "Maya Chen", "maya@example.com", False),
    ("E022", "Peter Novak", "peter@example.com", False),
    ("E070", "Tara Holm", "tara@example.com", False),
    ("E071", "Felix Bauer", "felix@example.com", False),

    # Enterprise Sales
    ("E023", "Clara Stone", "clara@example.com", False),
    ("E024", "George Smith", "george@example.com", False),
    ("E025", "Nina Brown", "nina@example.com", False),
    ("E026", "Theo Green", "theo@example.com", False),

    # SMB Sales — intentionally small
    ("E027", "Milan Ristov", "milan@example.com", False),
    ("E028", "Eva White", "eva@example.com", False),
]


# Extra branch for the presentation edge case:
# a small team of 3 respondents rolling up into a large department of ~40.
CUSTOMER_OPS_EMPLOYEES = [
    ("E029", "Marcus Eriksson", "marcus@example.com", True),
    ("E030", "Helena Frost", "helena@example.com", True),
    ("E031", "Ravi Patel", "ravi@example.com", True),
    ("E032", "Elin Bergstrom", "elin@example.com", True),
]

CUSTOMER_OPS_EMPLOYEES.extend(
    (f"E{code:03d}", f"Core Support Employee {idx}", f"core.support.{idx}@example.com", False)
    for idx, code in enumerate(range(33, 52), start=1)
)

CUSTOMER_OPS_EMPLOYEES.extend(
    (f"E{code:03d}", f"Customer Success Employee {idx}", f"customer.success.{idx}@example.com", False)
    for idx, code in enumerate(range(52, 68), start=1)
)

CUSTOMER_OPS_EMPLOYEES.extend(
    (f"E{code:03d}", f"CX Research Employee {idx}", f"cx.research.{idx}@example.com", False)
    for idx, code in enumerate(range(68, 70), start=1)
)

EMPLOYEES.extend(CUSTOMER_OPS_EMPLOYEES)


# Extra staff so the model holds up once team managers move into a department
# Leadership team: leadership padding (to reach ~5 per leadership cohort) and a
# couple of ICs so teams that would drop to exactly 4 keep a ~70-80% rate.
LEADERSHIP_AND_STAFF = [
    ("E300", "Sofia Reuter", "sofia.reuter@example.com", False),    # Revenue Leadership staff
    ("E301", "Mark Lindgren", "mark.lindgren@example.com", False),  # Revenue Leadership staff
    ("E302", "Nadia Holm", "nadia.holm@example.com", False),        # Customer Ops Leadership staff
    ("E303", "Tomas Falk", "tomas.falk@example.com", False),        # Data Team IC
    ("E304", "Greta Vidmar", "greta.vidmar@example.com", False),    # Enterprise Sales IC
    ("E305", "Ada Nyholm", "ada.nyholm@example.com", False),        # CX Research Pod IC
    # A few non-responding ICs so the Engineering teams sit nearer a ~70% rate
    # (6 members, 4 respond) rather than 80%.
    ("E306", "Viktor Sund", "viktor.sund@example.com", False),      # Platform Team IC
    ("E307", "Hanna Roth", "hanna.roth@example.com", False),        # Data Team IC
    ("E308", "Oskar Wahl", "oskar.wahl@example.com", False),        # Product Engineering IC
]

EMPLOYEES.extend(LEADERSHIP_AND_STAFF)



def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def create_employees(db):
    employees_by_code = {}

    for employee_code, full_name, email, is_manager in EMPLOYEES:
        employee = Employee(
            employee_code=employee_code,
            full_name=full_name,
            email=email,
            sso_subject=f"mock-sso-{employee_code.lower()}",
            is_manager=is_manager,
            is_pc_admin=employee_code in PC_ADMIN_CODES,
        )
        db.add(employee)
        employees_by_code[employee_code] = employee

    db.flush()
    return employees_by_code


def create_org_snapshot(
    db,
    name: str,
    snapshot_date: date,
    ai_lab_parent: str,
    sales_name: str,
    employees_by_code: dict,
    include_cx_research_pod: bool = False,
):
    snapshot = OrgSnapshot(
        name=name,
        snapshot_date=snapshot_date,
        description=f"Org snapshot for {name}",
    )
    db.add(snapshot)
    db.flush()

    units = {}

    def add_unit(
        external_key: str,
        unit_name: str,
        parent_key: str | None,
        manager_code: str | None,
    ):
        unit = OrgUnitSnapshot(
            snapshot=snapshot,
            external_key=external_key,
            name=unit_name,
            parent=units[parent_key] if parent_key else None,
            manager=employees_by_code[manager_code] if manager_code else None,
        )
        db.add(unit)
        db.flush()
        units[external_key] = unit
        return unit

    # The CIO owns the company root; every department rolls up into this dashboard.
    add_unit("company", "Company", None, "E200")

    add_unit("engineering", "Engineering", "company", "E001")
    add_unit("platform", "Platform Team", "engineering", "E003")
    add_unit("data", "Data Team", "engineering", "E004")

    if ai_lab_parent == "data":
        add_unit("ai_lab", "AI Lab", "data", "E005")
    else:
        add_unit("ai_lab", "AI Lab", "engineering", "E005")

    add_unit("product_engineering", "Product Engineering", "engineering", "E006")

    add_unit("sales", sales_name, "company", "E002")
    add_unit("enterprise_sales", "Enterprise Sales", "sales", "E007")
    add_unit("smb_sales", "SMB Sales", "sales", "E008")

    # Large department edge case for the anonymisation discussion.
    # Customer Operations exists historically, but the 3-person CX Research Pod
    # only appears in Annual 2026. This demonstrates a new small team entering
    # a large department and forcing secondary suppression at that point in time.
    add_unit("customer_operations", "Customer Operations", "company", "E029")
    add_unit("core_support", "Core Support", "customer_operations", "E030")
    add_unit("customer_success", "Customer Success", "customer_operations", "E031")

    if include_cx_research_pod:
        add_unit("cx_research_pod", "CX Research Pod", "customer_operations", "E032")

    # Department leadership teams. The department manager and that department's team
    # managers (plus non-team staff) report here, so each team's score reflects its
    # ICs only. No single person manages the leadership group. Created last so that,
    # on a tie, secondary suppression hides an existing team rather than Leadership.
    add_unit("engineering_leadership", "Engineering Leadership", "engineering", None)
    add_unit("revenue_leadership", f"{sales_name} Leadership", "sales", None)
    add_unit(
        "customer_operations_leadership",
        "Customer Operations Leadership",
        "customer_operations",
        None,
    )

    memberships = {
        # Company root holds only the CIO, who abstains from the survey.
        "company": ["E200"],

        # Managers report in their department's Leadership cohort; teams are ICs only.
        "engineering_leadership": ["E001", "E003", "E004", "E005", "E006"],
        "platform": ["E009", "E010", "E011", "E012", "E013", "E306"],
        "data": ["E014", "E015", "E016", "E017", "E303", "E307"],
        "ai_lab": ["E018", "E019"],
        "product_engineering": ["E020", "E021", "E022", "E070", "E071", "E308"],

        "revenue_leadership": ["E002", "E007", "E008", "E300", "E301"],
        "enterprise_sales": ["E023", "E024", "E025", "E026", "E304"],
        "smb_sales": ["E027", "E028"],

        "core_support": [f"E{code:03d}" for code in range(33, 52)],
        "customer_success": [f"E{code:03d}" for code in range(52, 68)],
    }

    # CustOps leadership: Elin (CX Pod manager) only joins once the pod exists.
    custops_leadership = ["E029", "E030", "E031", "E302"]
    if include_cx_research_pod:
        custops_leadership.append("E032")
    memberships["customer_operations_leadership"] = custops_leadership

    if include_cx_research_pod:
        memberships["cx_research_pod"] = ["E068", "E069", "E305"]

    for org_key, employee_codes in memberships.items():
        for employee_code in employee_codes:
            membership = OrgMembershipSnapshot(
                snapshot=snapshot,
                employee=employees_by_code[employee_code],
                org_unit=units[org_key],
                role_title="Manager" if employees_by_code[employee_code].is_manager else "Employee",
            )
            db.add(membership)

    db.flush()
    return snapshot, units


def create_survey_cycle(
    db,
    name: str,
    cycle_type: str,
    starts_on: date,
    ends_on: date,
    snapshot: OrgSnapshot,
):
    cycle = SurveyCycle(
        name=name,
        cycle_type=cycle_type,
        status="closed",
        starts_on=starts_on,
        ends_on=ends_on,
        org_snapshot=snapshot,
    )
    db.add(cycle)
    db.flush()

    questions_by_key = {}

    for question_def in QUESTIONS:
        question = Question(
            survey_cycle=cycle,
            question_key=question_def["key"],
            category=question_def["category"],
            text=question_def["text"],
            response_type=question_def["response_type"],
            display_order=question_def["order"],
            is_required=True,
            weight=question_def["weight"],
        )
        db.add(question)
        questions_by_key[question_def["key"]] = question

    db.flush()
    return cycle, questions_by_key


def score_for_answer(
    cycle_name: str,
    org_key: str,
    question_key: str,
    respondent_index: int,
):
    """
    Deterministic fake scoring.

    We deliberately make:
    - leadership improve over time
    - workload slightly dip in Annual 2026
    - small teams have scores too, but they will later be hidden by privacy rules
    """

    # Per-question baselines. Sibling questions in the same category deliberately
    # differ (e.g. "manage stress" sits below "energized at work") so the
    # weighted mean lands away from the naive average and question weighting is
    # visible on the dashboard.
    cycle_baseline = {
        "Half-year 2025": {
            "employee_health": 3.5,
            "health_stress": 2.9,
            "health_balance": 3.3,
            "workload_balance": 3.7,
            "workload_expectations": 3.5,
            "workload_resources": 3.6,
            "leadership_support": 3.2,
            "leadership_clarity": 3.0,
            "leadership_recognition": 3.4,
            "team_collaboration": 3.5,
            "collab_trust": 3.3,
            "collab_crossteam": 3.6,
        },
        "Annual 2025": {
            "employee_health": 3.8,
            "health_stress": 3.1,
            "health_balance": 3.6,
            "workload_balance": 3.4,
            "workload_expectations": 3.6,
            "workload_resources": 3.5,
            "leadership_support": 3.7,
            "leadership_clarity": 3.5,
            "leadership_recognition": 3.8,
            "team_collaboration": 3.8,
            "collab_trust": 3.6,
            "collab_crossteam": 3.9,
        },
        "Half-year 2026": {
            "employee_health": 4.1,
            "health_stress": 3.2,
            "health_balance": 3.9,
            "workload_balance": 3.0,
            "workload_expectations": 3.5,
            "workload_resources": 3.4,
            "leadership_support": 4.2,
            "leadership_clarity": 4.0,
            "leadership_recognition": 4.3,
            "team_collaboration": 4.1,
            "collab_trust": 3.9,
            "collab_crossteam": 4.2,
        },
    }

    # Category-specific adjustments make the demo dashboards less flat:
    # different departments have different strengths and weaknesses.
    # Org-level adjustments are per category (a department is "weak on workload"),
    # applied to every question in that category.
    org_adjustment = {
        "engineering": {"Health": 0.1, "Workload": -0.1, "Leadership": 0.1, "Collaboration": 0.1, "eNPS": 0.2},
        "platform": {"Health": 0.2, "Workload": -0.4, "Leadership": 0.3, "Collaboration": 0.1, "eNPS": 0.5},
        "data": {"Health": -0.2, "Workload": -0.2, "Leadership": -0.1, "Collaboration": 0.0, "eNPS": -0.4},
        "ai_lab": {"Health": -0.4, "Workload": -0.5, "Leadership": -0.2, "Collaboration": -0.1, "eNPS": -0.8},
        "product_engineering": {"Health": 0.0, "Workload": -0.1, "Leadership": 0.1, "Collaboration": 0.2, "eNPS": 0.0},
        "sales": {"Health": 0.0, "Workload": -0.2, "Leadership": 0.0, "Collaboration": 0.1, "eNPS": 0.0},
        "enterprise_sales": {"Health": 0.1, "Workload": -0.1, "Leadership": 0.0, "Collaboration": 0.2, "eNPS": 0.3},
        "smb_sales": {"Health": -0.2, "Workload": -0.4, "Leadership": -0.2, "Collaboration": -0.1, "eNPS": -0.6},
        "customer_operations": {"Health": 0.0, "Workload": -0.2, "Leadership": 0.0, "Collaboration": 0.0, "eNPS": 0.0},
        "core_support": {"Health": -0.1, "Workload": -0.4, "Leadership": 0.1, "Collaboration": 0.2, "eNPS": -0.1},
        "customer_success": {"Health": 0.2, "Workload": 0.1, "Leadership": 0.2, "Collaboration": 0.3, "eNPS": 0.6},
        "cx_research_pod": {"Health": -0.3, "Workload": -0.6, "Leadership": -0.3, "Collaboration": 0.0, "eNPS": -0.9},
        "engineering_leadership": {"Health": 0.2, "Workload": -0.3, "Leadership": 0.3, "Collaboration": 0.2, "eNPS": 0.4},
        "revenue_leadership": {"Health": 0.1, "Workload": -0.3, "Leadership": 0.2, "Collaboration": 0.2, "eNPS": 0.2},
        "customer_operations_leadership": {"Health": 0.1, "Workload": -0.2, "Leadership": 0.2, "Collaboration": 0.3, "eNPS": 0.3},
    }

    category = QUESTION_CATEGORY[question_key]
    baseline = cycle_baseline[cycle_name][question_key]
    adjustment = org_adjustment.get(org_key, {}).get(category, 0.0)

    # Small deterministic variation between respondents.
    variation = ((respondent_index % 3) - 1) * 0.2
    value = baseline + adjustment + variation

    return round(clamp(value, 1, 5), 1)


def enps_pulse_value(
    month_index: int,
    total_months: int,
    org_key: str,
    respondent_index: int,
):
    """
    Deterministic eNPS pulse answer (0-10), ramping up month over month so the
    net score trends from negative to positive across the year.
    """

    span = max(total_months - 1, 1)
    baseline = ENPS_PULSE_BASELINE_START + (
        ENPS_PULSE_BASELINE_END - ENPS_PULSE_BASELINE_START
    ) * (month_index / span)

    adjustment = ENPS_ORG_ADJUSTMENT.get(org_key, 0.0)
    variation = ((respondent_index % 5) - 2) * 0.5

    return round(clamp(baseline + adjustment + variation, 0, 10), 0)


def create_enps_pulse_cycle(
    db,
    name: str,
    starts_on: date,
    ends_on: date,
    snapshot: OrgSnapshot,
    status: str = "closed",
):
    cycle = SurveyCycle(
        name=name,
        cycle_type="enps_pulse",
        status=status,
        starts_on=starts_on,
        ends_on=ends_on,
        org_snapshot=snapshot,
    )
    db.add(cycle)
    db.flush()

    question = Question(
        survey_cycle=cycle,
        question_key=ENPS_PULSE_QUESTION["key"],
        category=ENPS_PULSE_QUESTION["category"],
        text=ENPS_PULSE_QUESTION["text"],
        response_type=ENPS_PULSE_QUESTION["response_type"],
        display_order=ENPS_PULSE_QUESTION["order"],
        is_required=True,
        weight=ENPS_PULSE_QUESTION["weight"],
    )
    db.add(question)
    db.flush()

    return cycle, {ENPS_PULSE_QUESTION["key"]: question}


def create_participants_and_responses(
    db,
    cycle: SurveyCycle,
    questions_by_key: dict,
    snapshot: OrgSnapshot,
    response_targets_by_org: dict,
    score_fn=None,
):
    """
    Creates one participant record for every employee in the snapshot,
    but only creates anonymous responses for selected employees.

    This models an SSO-based internal survey:

    - Employee signs in with SSO
    - App checks SurveyParticipant for eligibility
    - App checks has_submitted to prevent duplicate submission
    - App stores the response anonymously without employee_id
    """

    memberships = (
        db.query(OrgMembershipSnapshot)
        .filter(OrgMembershipSnapshot.snapshot_id == snapshot.id)
        .all()
    )

    memberships_by_org = {}

    for membership in memberships:
        memberships_by_org.setdefault(membership.org_unit.external_key, [])
        memberships_by_org[membership.org_unit.external_key].append(membership)

    for org_key, org_memberships in memberships_by_org.items():
        org_memberships = sorted(
            org_memberships,
            key=lambda membership: membership.employee.employee_code,
        )

        target_response_count = response_targets_by_org.get(org_key, 0)

        for index, membership in enumerate(org_memberships):
            has_submitted = index < target_response_count

            participant = SurveyParticipant(
                survey_cycle=cycle,
                employee=membership.employee,
                org_unit_at_time=membership.org_unit,
                has_submitted=has_submitted,
                submitted_at=datetime.now(timezone.utc) if has_submitted else None,
            )
            db.add(participant)

            if has_submitted:
                submission = ResponseSubmission(
                    anonymous_response_id=str(uuid.uuid4()),
                    survey_cycle=cycle,
                    org_unit_at_time=membership.org_unit,
                    submitted_bucket=cycle.starts_on.strftime("%Y-%m"),
                )
                db.add(submission)
                db.flush()

                for question_key, question in questions_by_key.items():
                    if score_fn is not None:
                        value = score_fn(org_key, question_key, index)
                    else:
                        value = score_for_answer(
                            cycle_name=cycle.name,
                            org_key=org_key,
                            question_key=question_key,
                            respondent_index=index,
                        )

                    answer = ResponseAnswer(
                        submission=submission,
                        question=question,
                        numeric_value=value,
                    )
                    db.add(answer)

    db.flush()

def seed():
    print("Resetting database...")

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()

    try:
        print("Creating employees...")
        employees_by_code = create_employees(db)

        print("Creating org snapshots...")

        snapshot_h1_2025, _ = create_org_snapshot(
            db=db,
            name="Org Snapshot Half-year 2025",
            snapshot_date=date(2025, 6, 1),
            ai_lab_parent="data",
            sales_name="Sales",
            employees_by_code=employees_by_code,
            include_cx_research_pod=False,
        )

        snapshot_annual_2025, _ = create_org_snapshot(
            db=db,
            name="Org Snapshot Annual 2025",
            snapshot_date=date(2025, 12, 1),
            ai_lab_parent="data",
            sales_name="Revenue",
            employees_by_code=employees_by_code,
            include_cx_research_pod=False,
        )

        snapshot_h1_2026, _ = create_org_snapshot(
            db=db,
            name="Org Snapshot Half-year 2026",
            snapshot_date=date(2026, 6, 1),
            ai_lab_parent="data",
            sales_name="Revenue",
            employees_by_code=employees_by_code,
            include_cx_research_pod=True,
        )

        print("Creating survey cycles and questions...")

        h1_2025, h1_2025_questions = create_survey_cycle(
            db=db,
            name="Half-year 2025",
            cycle_type="half_year",
            starts_on=date(2025, 6, 3),
            ends_on=date(2025, 6, 17),
            snapshot=snapshot_h1_2025,
        )

        annual_2025, annual_2025_questions = create_survey_cycle(
            db=db,
            name="Annual 2025",
            cycle_type="annual",
            starts_on=date(2025, 12, 1),
            ends_on=date(2025, 12, 15),
            snapshot=snapshot_annual_2025,
        )

        h1_2026, h1_2026_questions = create_survey_cycle(
            db=db,
            name="Half-year 2026",
            cycle_type="half_year",
            starts_on=date(2026, 6, 3),
            ends_on=date(2026, 6, 17),
            snapshot=snapshot_h1_2026,
        )

        print("Creating SSO participants and anonymous responses...")

        create_participants_and_responses(
            db=db,
            cycle=h1_2025,
            questions_by_key=h1_2025_questions,
            snapshot=snapshot_h1_2025,
            response_targets_by_org=RESPONSE_TARGETS,
        )

        create_participants_and_responses(
            db=db,
            cycle=annual_2025,
            questions_by_key=annual_2025_questions,
            snapshot=snapshot_annual_2025,
            response_targets_by_org=RESPONSE_TARGETS,
        )

        create_participants_and_responses(
            db=db,
            cycle=h1_2026,
            questions_by_key=h1_2026_questions,
            snapshot=snapshot_h1_2026,
            response_targets_by_org=RESPONSE_TARGETS,
        )

        print("Creating monthly eNPS pulses...")

        # Each monthly pulse uses the org snapshot effective at that time.
        pulse_snapshots = [
            (date(2025, 6, 1), snapshot_h1_2025),
            (date(2025, 12, 1), snapshot_annual_2025),
            (date(2026, 6, 1), snapshot_h1_2026),
        ]

        def effective_snapshot(on_date):
            chosen = pulse_snapshots[0][1]
            for snap_date, snap in pulse_snapshots:
                if snap_date <= on_date:
                    chosen = snap
            return chosen

        pulse_year, pulse_month = 2025, 7

        for month_index in range(ENPS_PULSE_COUNT):
            pulse_start = date(pulse_year, pulse_month, 1)
            pulse_end = date(pulse_year, pulse_month, 7)
            pulse_snapshot = effective_snapshot(pulse_start)

            # The most recent pulse is left open so the survey-taking flow can be
            # demonstrated live; all earlier pulses are closed archives.
            is_latest_pulse = month_index == ENPS_PULSE_COUNT - 1

            # Submission is date-gated, so the active pulse's window must include
            # "today" no matter when the demo is seeded. Keep its start at the month
            # boundary (so the monthly trend position is unchanged) and extend the
            # end past today.
            if is_latest_pulse:
                today = date.today()
                pulse_start = min(pulse_start, today)
                pulse_end = today + timedelta(days=7)

            pulse_cycle, pulse_questions = create_enps_pulse_cycle(
                db=db,
                name=f"eNPS Pulse {pulse_year}-{pulse_month:02d}",
                starts_on=pulse_start,
                ends_on=pulse_end,
                snapshot=pulse_snapshot,
                status="active" if is_latest_pulse else "closed",
            )

            create_participants_and_responses(
                db=db,
                cycle=pulse_cycle,
                questions_by_key=pulse_questions,
                snapshot=pulse_snapshot,
                response_targets_by_org=RESPONSE_TARGETS,
                score_fn=lambda org, qk, idx, mi=month_index: enps_pulse_value(
                    month_index=mi,
                    total_months=ENPS_PULSE_COUNT,
                    org_key=org,
                    respondent_index=idx,
                ),
            )

            pulse_month += 1
            if pulse_month > 12:
                pulse_month = 1
                pulse_year += 1

        db.commit()

        print("Seed complete.")
        print()
        print("Demo managers:")
        print("- Magnus Ahlberg (CIO) manages the whole company (all departments roll up)")
        print("- Olivia Ivkovic manages Engineering")
        print("- Alex Berg manages Platform Team")
        print("- Priya Shah manages Data Team")
        print("- Noah Jensen manages AI Lab")
        print("- Julia Lind manages Sales / Revenue")
        print("- Marcus Eriksson manages Customer Operations")
        print()
        print()
        print("Org model:")
        print("- Each department has a Leadership team (department + team managers + staff).")
        print("  Managers report there, so each team's score reflects its ICs only.")
        print("- The CIO (Magnus) abstains, so no lone response is recoverable at the company root.")
        print()
        print("Important seeded edge cases:")
        print("- AI Lab (under Data Team) and SMB Sales fall below 4 respondents and are hidden")
        print("- CX Research Pod is a new 3-person team that appears only in the latest snapshot (Half-year 2026)")
        print("- Customer Operations: the 3-person CX Research Pod is hidden, and the smallest visible sibling")
        print("  (the Leadership team) is secondarily suppressed, while the department rollup stays visible")
        print("- eNPS is a separate monthly pulse (the latest month is left active for live submission)")
        print("- Category scores vary by function so dashboards show clearer trends")

    finally:
        db.close()


if __name__ == "__main__":
    seed()