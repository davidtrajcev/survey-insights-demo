from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class Employee(Base):
    """
    Employee identity table.

    This is used for:
    - survey invitations
    - manager assignments

    Important:
    Employee identity is NOT stored on survey responses.
    """

    __tablename__ = "employees"

    id = Column(Integer, primary_key=True, index=True)
    employee_code = Column(String, unique=True, nullable=False, index=True)
    full_name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False, index=True)
    sso_subject = Column(String, unique=True, nullable=True, index=True)
    is_manager = Column(Boolean, default=False, nullable=False)
    # People & Culture admin: manages survey lifecycle and may view every
    # manager dashboard (still subject to anonymity suppression).
    is_pc_admin = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    managed_org_units = relationship(
        "OrgUnitSnapshot",
        back_populates="manager",
        foreign_keys="OrgUnitSnapshot.manager_employee_id",
    )

    memberships = relationship(
        "OrgMembershipSnapshot",
        back_populates="employee",
    )

    survey_participants = relationship(
        "SurveyParticipant",
        back_populates="employee",
    )


class OrgSnapshot(Base):
    """
    Frozen org structure for a survey cycle.

    Example:
    Annual 2026 uses the org structure as it looked on 2026-06-01.

    This prevents historical survey results from changing when people move teams later.
    """

    __tablename__ = "org_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    snapshot_date = Column(Date, nullable=False)
    description = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    org_units = relationship(
        "OrgUnitSnapshot",
        back_populates="snapshot",
        cascade="all, delete-orphan",
    )

    memberships = relationship(
        "OrgMembershipSnapshot",
        back_populates="snapshot",
        cascade="all, delete-orphan",
    )

    survey_cycles = relationship(
        "SurveyCycle",
        back_populates="org_snapshot",
    )


class OrgUnitSnapshot(Base):
    """
    Org unit inside a specific snapshot.

    This table models the org tree.

    Example:
    Company
      -> Engineering
          -> Platform Team
          -> Data Team

    parent_id creates the tree.
    manager_employee_id connects a manager to the org unit they own.
    """

    __tablename__ = "org_units"

    id = Column(Integer, primary_key=True, index=True)

    snapshot_id = Column(
        Integer,
        ForeignKey("org_snapshots.id"),
        nullable=False,
        index=True,
    )

    external_key = Column(String, nullable=False)
    name = Column(String, nullable=False)

    parent_id = Column(
        Integer,
        ForeignKey("org_units.id"),
        nullable=True,
        index=True,
    )

    manager_employee_id = Column(
        Integer,
        ForeignKey("employees.id"),
        nullable=True,
        index=True,
    )

    snapshot = relationship(
        "OrgSnapshot",
        back_populates="org_units",
    )

    parent = relationship(
        "OrgUnitSnapshot",
        remote_side=[id],
        back_populates="children",
    )

    children = relationship(
        "OrgUnitSnapshot",
        back_populates="parent",
    )

    manager = relationship(
        "Employee",
        back_populates="managed_org_units",
        foreign_keys=[manager_employee_id],
    )

    memberships = relationship(
        "OrgMembershipSnapshot",
        back_populates="org_unit",
    )

    survey_participants = relationship(
        "SurveyParticipant",
        back_populates="org_unit_at_time",
    )

    response_submissions = relationship(
        "ResponseSubmission",
        back_populates="org_unit_at_time",
    )

    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "external_key",
            name="uq_org_unit_snapshot_external_key",
        ),
    )


class OrgMembershipSnapshot(Base):
    """
    Employee membership inside an org snapshot.

    This answers:
    "Where did this employee belong when this survey cycle happened?"

    If someone moves teams later, old memberships stay unchanged.
    """

    __tablename__ = "org_memberships"

    id = Column(Integer, primary_key=True, index=True)

    snapshot_id = Column(
        Integer,
        ForeignKey("org_snapshots.id"),
        nullable=False,
        index=True,
    )

    employee_id = Column(
        Integer,
        ForeignKey("employees.id"),
        nullable=False,
        index=True,
    )

    org_unit_id = Column(
        Integer,
        ForeignKey("org_units.id"),
        nullable=False,
        index=True,
    )

    role_title = Column(String, nullable=True)

    snapshot = relationship(
        "OrgSnapshot",
        back_populates="memberships",
    )

    employee = relationship(
        "Employee",
        back_populates="memberships",
    )

    org_unit = relationship(
        "OrgUnitSnapshot",
        back_populates="memberships",
    )

    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "employee_id",
            name="uq_employee_membership_per_snapshot",
        ),
    )


class SurveyCycle(Base):
    """
    A survey cycle.

    Examples:
    - Annual 2025
    - Half-year 2026
    - June 2026 eNPS Pulse

    Each cycle points to one org snapshot.
    """

    __tablename__ = "survey_cycles"

    id = Column(Integer, primary_key=True, index=True)

    name = Column(String, nullable=False)
    cycle_type = Column(String, nullable=False)
    status = Column(String, default="draft", nullable=False)

    starts_on = Column(Date, nullable=False)
    ends_on = Column(Date, nullable=False)

    org_snapshot_id = Column(
        Integer,
        ForeignKey("org_snapshots.id"),
        nullable=False,
        index=True,
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    org_snapshot = relationship(
        "OrgSnapshot",
        back_populates="survey_cycles",
    )

    questions = relationship(
        "Question",
        back_populates="survey_cycle",
        cascade="all, delete-orphan",
    )

    survey_participants = relationship(
        "SurveyParticipant",
        back_populates="survey_cycle",
    )
    response_submissions = relationship(
        "ResponseSubmission",
        back_populates="survey_cycle",
    )

    __table_args__ = (
        CheckConstraint(
            "cycle_type IN ('annual', 'half_year', 'enps_pulse')",
            name="check_survey_cycle_type",
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'closed')",
            name="check_survey_cycle_status",
        ),
    )


class Question(Base):
    """
    Survey question.

    question_key is important for trends.

    Example:
    The text may change slightly over time, but the key can stay stable:

    leadership_support
    workload_balance
    team_collaboration
    employee_health
    enps
    """

    __tablename__ = "questions"

    id = Column(Integer, primary_key=True, index=True)

    survey_cycle_id = Column(
        Integer,
        ForeignKey("survey_cycles.id"),
        nullable=False,
        index=True,
    )

    question_key = Column(String, nullable=False)
    category = Column(String, nullable=False)
    text = Column(Text, nullable=False)

    response_type = Column(String, default="likert_1_5", nullable=False)
    display_order = Column(Integer, default=0, nullable=False)
    is_required = Column(Boolean, default=True, nullable=False)

    # Relative importance of this question within its category. The category
    # score is a weighted mean, so a higher weight makes this question count for
    # more. Stored per cycle (Question is per cycle), so weights can change over
    # time without rewriting historical cycles. Does not affect respondent
    # counts, so the <4 anonymity threshold is unchanged.
    weight = Column(Float, default=1.0, nullable=False)

    survey_cycle = relationship(
        "SurveyCycle",
        back_populates="questions",
    )

    answers = relationship(
        "ResponseAnswer",
        back_populates="question",
    )

    __table_args__ = (
        UniqueConstraint(
            "survey_cycle_id",
            "question_key",
            name="uq_question_key_per_cycle",
        ),
        CheckConstraint(
            "response_type IN ('likert_1_5', 'enps_0_10', 'text')",
            name="check_question_response_type",
        ),
    )


class SurveyParticipant(Base):
    """
    Operational eligibility table for SSO-based survey access.

    In production:
    - employee signs in with company SSO
    - app maps SSO identity to Employee
    - app checks this table to see whether the employee is eligible
    - app checks has_submitted to prevent duplicate responses

    Important:
    This table contains identity.

    The anonymous response tables do NOT store:
    - employee_id
    - email
    - name
    - sso_subject
    - participant_id
    """

    __tablename__ = "survey_participants"

    id = Column(Integer, primary_key=True, index=True)

    survey_cycle_id = Column(
        Integer,
        ForeignKey("survey_cycles.id"),
        nullable=False,
        index=True,
    )

    employee_id = Column(
        Integer,
        ForeignKey("employees.id"),
        nullable=False,
        index=True,
    )

    org_unit_id_at_time = Column(
        Integer,
        ForeignKey("org_units.id"),
        nullable=False,
        index=True,
    )

    has_submitted = Column(Boolean, default=False, nullable=False)
    submitted_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    survey_cycle = relationship(
        "SurveyCycle",
        back_populates="survey_participants",
    )

    employee = relationship(
        "Employee",
        back_populates="survey_participants",
    )

    org_unit_at_time = relationship(
        "OrgUnitSnapshot",
        back_populates="survey_participants",
    )

    __table_args__ = (
        UniqueConstraint(
            "survey_cycle_id",
            "employee_id",
            name="uq_one_participant_per_employee_per_cycle",
        ),
    )

class ResponseSubmission(Base):
    """
    Anonymous survey submission.

    Important:
    No employee_id.
    No email.
    No name.
    No participant_id.
    No exact submission timestamp.

    It only stores:
    - which survey cycle this belongs to
    - which org unit the respondent belonged to at that time
    - anonymous response id
    - an optional coarse submitted bucket for operational reporting

    The coarse bucket avoids creating a timestamp bridge between the identity
    table and the anonymous response table.
    """

    __tablename__ = "response_submissions"

    id = Column(Integer, primary_key=True, index=True)

    anonymous_response_id = Column(
        String,
        unique=True,
        nullable=False,
        index=True,
    )

    survey_cycle_id = Column(
        Integer,
        ForeignKey("survey_cycles.id"),
        nullable=False,
        index=True,
    )

    org_unit_id_at_time = Column(
        Integer,
        ForeignKey("org_units.id"),
        nullable=False,
        index=True,
    )

    submitted_bucket = Column(String, nullable=True)

    survey_cycle = relationship(
        "SurveyCycle",
        back_populates="response_submissions",
    )

    org_unit_at_time = relationship(
        "OrgUnitSnapshot",
        back_populates="response_submissions",
    )

    answers = relationship(
        "ResponseAnswer",
        back_populates="submission",
        cascade="all, delete-orphan",
    )


class ResponseAnswer(Base):
    """
    Individual answer belonging to an anonymous submission.

    Numeric values are used for dashboard analytics.

    Free-text values are intentionally not used in manager reporting. In the
    current MVP submission flow, free text is not stored because comments can
    identify their author even when numeric scores are aggregated.
    """

    __tablename__ = "response_answers"

    id = Column(Integer, primary_key=True, index=True)

    submission_id = Column(
        Integer,
        ForeignKey("response_submissions.id"),
        nullable=False,
        index=True,
    )

    question_id = Column(
        Integer,
        ForeignKey("questions.id"),
        nullable=False,
        index=True,
    )

    numeric_value = Column(Float, nullable=True)
    text_value = Column(Text, nullable=True)

    submission = relationship(
        "ResponseSubmission",
        back_populates="answers",
    )

    question = relationship(
        "Question",
        back_populates="answers",
    )

    __table_args__ = (
        UniqueConstraint(
            "submission_id",
            "question_id",
            name="uq_one_answer_per_question_per_submission",
        ),
    )