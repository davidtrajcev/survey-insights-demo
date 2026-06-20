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
- **Secondary:** if a visible parent has exactly one hidden child, the smallest
  visible sibling is also hidden so the hidden team can't be recovered by
  subtraction — run to a fixpoint.
- **Per-question:** each question in a breakdown is suppressed independently.
- **Cross-dashboard:** a viewer's broad scope masks navigation into descendant
  dashboards that were suppressed in their wider view, so drilling in can't
  reopen the differencing attack.

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
- **Org tree** — the manager's unit and all child units after suppression; team
  rows link through to that team's dashboard.

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

Open `http://127.0.0.1:8000` (health check at `/health`).

## Demo flow

1. Open `/login` and pick a seeded identity (suggested: Petra Lindqvist, Olivia
   Ivkovic, David Trajcev, Marcus Eriksson).
2. **Petra Lindqvist** (P&C admin) lands on `/admin` — the survey lifecycle, and
   can open any manager dashboard. To exercise survey submission, create and
   **publish** a cycle here, then sign in as an employee to submit it.
3. **Olivia Ivkovic** (Engineering) shows a department view: multi-team
   comparison, weighted scores, benchmarks, and nearby suppression.
4. **A team-level manager** (e.g. Alex Berg / Platform Team) shows the leaf-team
   view, including the lowest-scoring-questions panel.
5. **David Trajcev** (employee) shows the My area / survey-taking side.

### Seeded edge cases

- **AI Lab** and **SMB Sales** have fewer than 4 respondents → hidden.
- **Secondary suppression** fires where a lone hidden child would otherwise be
  recoverable (e.g. in the Sales/Revenue and Customer Operations branches).
- **Customer Operations** has roughly 40 eligible employees; in the latest cycle
  28 responded, including two from the new three-person **CX Research Pod** that
  appears only in the latest snapshot. The department rollup stays visible while
  the pod and one sibling breakdown are suppressed.
- The **eNPS monthly pulse** runs Jul 2025 → Jun 2026 and climbs from
  net-negative to net-positive.

In production the natural next steps are materialising suppression at cycle
close, effective-dated org data sourced from an HRIS, and moving authentication
to a real OIDC provider (Entra ID).
