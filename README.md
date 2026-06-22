# Survey Insights Demo

A small FastAPI demo for a privacy-safe internal employee survey platform — a
focused take on replacing a tool like Eletive with something you own and control.

It deliberately concentrates on the hard parts: **anonymity**, **org-aware
reporting**, and **disclosure control** — not survey-builder breadth.

## What it demonstrates

- **SSO-style access** with role-based navigation (employee / manager / People & Culture admin).
- **Participant eligibility** and duplicate-submission prevention.
- **Anonymous response storage** kept separate from identity.
- **Org-aware manager reporting** — a manager sees their unit and everyone below it.
- **Historical org snapshots** so org changes don't rewrite past results.
- **`<4` respondent suppression**, plus **secondary suppression** to close the differencing leak.
- **Question and area weighting** — weighted category scores and a priority-weighted focus.
- **Company benchmarking** — every theme compared to the company-wide average.
- **Trends over time**, including a separate **continuous monthly eNPS pulse**.

## Survey model

- **Two survey cycles a year:** an **Annual** survey (December) and a **Half-year**
  survey (June). Seeded cycles: `Half-year 2025` → `Annual 2025` → `Half-year 2026`
  (the newest is the dashboard default).
- Each survey covers four themes — **Health, Workload, Leadership, Collaboration** —
  with **three questions per theme**. Each question carries a **weight**, so a
  category score is a weighted mean of its questions.
- **eNPS is a separate, continuous monthly pulse** (its own cadence, `-100..100`
  net score), not part of the annual/half-year survey. The dashboard shows the
  pulse closest in time to the selected survey cycle, and a full monthly trend.

## Privacy model

Operational identity is separated from analytics data:

- `survey_participants` stores the SSO-authenticated employee, eligibility, org
  snapshot unit, and submitted / not-submitted status.
- `response_submissions` and `response_answers` store anonymous answers — no
  employee id, email, name, SSO subject, or participant id, and only a coarse
  submission month (no precise timestamp).
- Manager dashboards read **privacy-safe aggregates only**, via a privacy layer
  that sits between raw analytics and the templates.

Suppression is layered:

- **Primary:** any unit/theme with fewer than `MIN_RESPONDENTS` (4) is hidden.
- **Secondary:** what a manager could recover as *(parent total − visible
  children)* — the combined cohort of every hidden child **plus the parent's own
  direct respondents** — must be 0 or at least `MIN_RESPONDENTS`. If it's smaller,
  the smallest visible sibling is hidden too (run to a fixpoint), so no
  sub-threshold group can be isolated by subtraction.
- **Per-question:** each question in a breakdown is suppressed independently.
- **Cross-dashboard:** a viewer's broad scope masks navigation into descendant
  dashboards that were suppressed in their wider view, so drilling in can't
  reopen the differencing attack.
- **eNPS pulses:** each monthly pulse is masked independently against the viewer's
  broad scope, so the pulse history can't be used to read a score that's
  suppressed in the current view.

## Org model

- Each survey cycle pins an **org snapshot**; reorgs create a new snapshot and
  never rewrite old ones.
- A stable **`external_key`** identifies the same business unit across snapshots,
  so a renamed/moved team keeps one continuous trend line.
- **Attribution is frozen** (responses are tagged with the org unit at submission
  time) while **access is resolved against the current org**.

## Roles

| Role | Sees |
|------|------|
| Employee | Overview, My area (their own surveys) |
| Manager | Overview, My area, Manager dashboards (their scope) |
| P&C admin | Overview, Manager dashboards (**all**), P&C admin (survey lifecycle) |

Navigation is role-aware, and the `/admin` routes are gated to P&C admins. P&C
admins can open every manager dashboard, but anonymity suppression still applies.

## Manager dashboard

- **Cockpit** — the priority-weighted focus area ("what to act on this cycle"),
  strongest area, biggest movement, privacy impact, and team conversation prompts.
- **KPI cards** per theme — score, **vs-company benchmark**, and an expandable
  **weighted question breakdown**; plus an eNPS card from the closest pulse.
- **Visual overview** — current 1–5 scores; **team comparison** (child teams,
  sorted) for departments, or **lowest-scoring questions across all themes** for a
  leaf team; and a response-rate doughnut (responded vs eligible).
- **Trends** — theme scores across survey cycles and a separate monthly eNPS pulse.
- **Org tree** — the manager's unit and all child units after suppression;
  foldable, with team rows linking through to that team's dashboard.

## Tech stack

- FastAPI · Jinja2 · SQLite · SQLAlchemy · Chart.js · Inter (web font)

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.seed
python -m uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000` (health check at `/health`). The case-presentation
slide deck is served at `/deck/`.

## Demo flow

The recommended path drills top-down through the org:

1. Open `/login` and pick a seeded identity.
2. **Magnus Ahlberg** (CIO) sees the whole company roll up into one view; click a
   department bar or an org-tree row to drill into it.
3. **Olivia Ivkovic** (Engineering) — a department view: multi-team comparison,
   weighted scores, and company benchmarks.
4. **Marcus Eriksson** (Customer Operations) — the suppression showcase: the
   three-person CX Research Pod is hidden and a sibling is secondarily suppressed,
   while the department rollup stays visible.
5. **Alex Berg** (Platform Team) — a leaf-team view, including the
   lowest-scoring-questions panel.
6. **David Trajcev** (employee) — the My area / survey-taking side.
7. **Petra Lindqvist** (P&C admin) lands on `/admin` for the survey lifecycle and
   can open any manager dashboard. Create and **publish** a cycle (with a current
   date window) to exercise submission.

### Seeded edge cases

- **Managers report in a department Leadership team** (the department manager plus
  that department's team managers and staff), so each team's score reflects its
  ICs only — and no rollup unit is exposed by a lone manager's own response.
- **AI Lab** and **SMB Sales** have fewer than 4 respondents → hidden.
- **Secondary suppression** fires where a hidden team would otherwise be
  recoverable by subtraction: in **Revenue**, Enterprise Sales is suppressed to
  protect SMB Sales; in **Customer Operations**, the smallest visible sibling (the
  Leadership team) is suppressed to protect the CX Research Pod.
- **Customer Operations** has roughly 40 eligible employees; in the latest cycle
  ~30 responded, including two from the new three-person **CX Research Pod** that
  appears only in the latest snapshot. The department rollup and the larger teams
  (Core Support, Customer Success) stay visible, while the pod and the smallest
  sibling are suppressed.
- The **eNPS monthly pulse** runs Jul 2025 → Jun 2026 and climbs from
  net-negative to net-positive.

In production the natural next steps are materialising suppression at cycle
close, effective-dated org data sourced from an HRIS, and moving authentication
to a real OIDC provider (Entra ID).
