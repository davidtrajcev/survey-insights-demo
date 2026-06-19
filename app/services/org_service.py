from typing import Optional

from sqlalchemy.orm import Session

from app.models import Employee, OrgUnitSnapshot, SurveyCycle


def get_survey_cycle(db: Session, survey_cycle_id: int) -> Optional[SurveyCycle]:
    return (
        db.query(SurveyCycle)
        .filter(SurveyCycle.id == survey_cycle_id)
        .first()
    )


def get_latest_survey_cycle(db: Session) -> Optional[SurveyCycle]:
    # The annual/half-year engagement survey. eNPS pulses are a separate cadence
    # and never count as the latest survey cycle.
    return (
        db.query(SurveyCycle)
        .filter(SurveyCycle.cycle_type.in_(["annual", "half_year"]))
        .order_by(SurveyCycle.starts_on.desc())
        .first()
    )


def get_manager(db: Session, manager_id: int) -> Optional[Employee]:
    return (
        db.query(Employee)
        .filter(
            Employee.id == manager_id,
            Employee.is_manager.is_(True),
        )
        .first()
    )


def get_current_org_snapshot(db: Session):
    """
    Demo helper for current-org access decisions.

    In production this would come from the live people/org source of truth
    such as HRIS or Entra-backed org data. In this demo, the latest survey
    cycle's snapshot represents the current org.
    """

    latest_cycle = get_latest_survey_cycle(db)

    if not latest_cycle:
        return None

    return latest_cycle.org_snapshot


def get_current_org_root_unit(db: Session) -> Optional[OrgUnitSnapshot]:
    """
    Root org unit of the current snapshot — the effective scope for a P&C admin,
    who can view every manager dashboard.
    """

    current_snapshot = get_current_org_snapshot(db)

    if not current_snapshot:
        return None

    return (
        db.query(OrgUnitSnapshot)
        .filter(
            OrgUnitSnapshot.snapshot_id == current_snapshot.id,
            OrgUnitSnapshot.parent_id.is_(None),
        )
        .first()
    )


def get_current_managed_org_unit(
    db: Session,
    manager_id: int,
) -> Optional[OrgUnitSnapshot]:
    """
    Finds what the manager owns in the current org.

    This is for access control, not historical attribution. Historical results
    still use the org snapshot attached to the selected survey cycle.
    """

    current_snapshot = get_current_org_snapshot(db)

    if not current_snapshot:
        return None

    return (
        db.query(OrgUnitSnapshot)
        .filter(
            OrgUnitSnapshot.snapshot_id == current_snapshot.id,
            OrgUnitSnapshot.manager_employee_id == manager_id,
        )
        .first()
    )


def get_org_unit_by_external_key_for_cycle(
    db: Session,
    external_key: str,
    survey_cycle_id: int,
) -> Optional[OrgUnitSnapshot]:
    """
    Resolves the same business org unit in a historical survey snapshot.

    This lets a successor manager view the history of the unit they currently
    own without relying on who managed that unit in the old snapshot.
    """

    cycle = get_survey_cycle(db, survey_cycle_id)

    if not cycle:
        return None

    return (
        db.query(OrgUnitSnapshot)
        .filter(
            OrgUnitSnapshot.snapshot_id == cycle.org_snapshot_id,
            OrgUnitSnapshot.external_key == external_key,
        )
        .first()
    )


def is_org_unit_descendant_or_self(
    db: Session,
    ancestor: OrgUnitSnapshot,
    candidate: OrgUnitSnapshot,
) -> bool:
    if ancestor.snapshot_id != candidate.snapshot_id:
        return False

    descendant_ids = {unit.id for unit in get_descendant_org_units(db, ancestor)}
    return candidate.id in descendant_ids


def get_managed_org_unit_for_cycle(
    db: Session,
    manager_id: int,
    survey_cycle_id: int,
) -> Optional[OrgUnitSnapshot]:
    """
    Finds the org unit managed by this manager in this specific survey cycle.

    This is useful for historical/debug views. For real dashboard access, use
    the current org to decide who may see the dashboard, then map that current
    org unit into the selected survey cycle for historical attribution.
    """

    cycle = get_survey_cycle(db, survey_cycle_id)

    if not cycle:
        return None

    return (
        db.query(OrgUnitSnapshot)
        .filter(
            OrgUnitSnapshot.snapshot_id == cycle.org_snapshot_id,
            OrgUnitSnapshot.manager_employee_id == manager_id,
        )
        .first()
    )


def get_all_org_units_for_snapshot(
    db: Session,
    snapshot_id: int,
) -> list[OrgUnitSnapshot]:
    return (
        db.query(OrgUnitSnapshot)
        .filter(OrgUnitSnapshot.snapshot_id == snapshot_id)
        .order_by(OrgUnitSnapshot.name)
        .all()
    )


def get_descendant_org_units(
    db: Session,
    root_org_unit: OrgUnitSnapshot,
) -> list[OrgUnitSnapshot]:
    """
    Returns the root org unit plus all org units below it.

    Example:
    Engineering
      -> Platform Team
      -> Data Team
      -> AI Lab

    If root is Engineering, return all four.
    """

    all_units = get_all_org_units_for_snapshot(
        db=db,
        snapshot_id=root_org_unit.snapshot_id,
    )

    children_by_parent_id: dict[int | None, list[OrgUnitSnapshot]] = {}

    for unit in all_units:
        children_by_parent_id.setdefault(unit.parent_id, [])
        children_by_parent_id[unit.parent_id].append(unit)

    result = []
    stack = [root_org_unit]

    while stack:
        current = stack.pop()
        result.append(current)

        children = children_by_parent_id.get(current.id, [])
        stack.extend(sorted(children, key=lambda unit: unit.name, reverse=True))

    return result


def get_org_unit_path(org_unit: OrgUnitSnapshot) -> list[str]:
    """
    Returns a readable path for an org unit.

    Example:
    Company > Engineering > Platform Team
    """

    path = []
    current = org_unit

    while current:
        path.append(current.name)
        current = current.parent

    return list(reversed(path))


def get_relative_depth(
    root_org_unit: OrgUnitSnapshot,
    org_unit: OrgUnitSnapshot,
) -> int:
    """
    Used later for rendering an indented org tree in the dashboard.
    """

    root_path = get_org_unit_path(root_org_unit)
    unit_path = get_org_unit_path(org_unit)

    return max(0, len(unit_path) - len(root_path))