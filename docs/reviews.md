# Performance Reviews

Cortiva includes a periodic performance review system that aggregates agent work data into structured reviews with trend analysis. Reviews cover weekly, monthly, or quarterly periods and are persisted as JSON in the agent's journal directory.

## What Reviews Track

Each review computes the following metrics from an agent's daily work log:

| Metric | Description |
|---|---|
| `total_hours` | Total hours worked in the period |
| `scheduled_hours` | Total scheduled hours in the period |
| `overtime_hours` | Hours worked beyond the scheduled amount (per-day, summed) |
| `tasks_completed` | Total tasks completed |
| `tasks_escalated` | Total tasks escalated to a higher authority |
| `escalation_ratio` | Escalated / (completed + escalated) |
| `consciousness_calls` | Total LLM API calls made |
| `budget_efficiency` | Consciousness calls per completed task (lower is better) |
| `days_active` | Number of days with hours > 0 |
| `avg_hours_per_day` | Total hours / active days |

## Work Log Format

Reviews are computed from a `work-log.json` file in the agent's journal directory. The file can be either a JSON array or a dict with an `entries` key:

```json
[
  {
    "date": "2026-03-28",
    "hours_worked": 8.0,
    "scheduled_hours": 8.0,
    "tasks_completed": 5,
    "tasks_escalated": 1,
    "consciousness_calls": 3
  },
  {
    "date": "2026-03-29",
    "hours_worked": 7.5,
    "scheduled_hours": 8.0,
    "tasks_completed": 4,
    "tasks_escalated": 0,
    "consciousness_calls": 2
  }
]
```

Or equivalently:

```json
{
  "entries": [
    { "date": "2026-03-28", "hours_worked": 8.0, "tasks_completed": 5 }
  ]
}
```

Cortiva agents write to this log automatically during their daily cycle. You can also write entries manually or from external tooling.

## Review Periods

Three periods are supported:

| Period | Duration |
|---|---|
| `WEEKLY` | 7 days |
| `MONTHLY` | 30 days |
| `QUARTERLY` | 90 days |

```python
from cortiva.core.reviews import ReviewPeriod

period = ReviewPeriod.WEEKLY
print(period.days)  # 7
```

## Generating Reviews

Use `ReviewManager` to generate a review for an agent:

```python
from pathlib import Path
from cortiva.core.reviews import ReviewManager, ReviewPeriod

mgr = ReviewManager()
review = mgr.generate_review(
    agent_dir=Path("agents/dev-cortiva"),
    period=ReviewPeriod.WEEKLY,
)

print(f"Agent: {review.agent_id}")
print(f"Period: {review.start_date} to {review.end_date}")
print(f"Tasks completed: {review.metrics.tasks_completed}")
print(f"Trend: {review.trend}")
```

The `agent_dir` is the root directory of the agent (e.g. `agents/dev-cortiva`). The manager reads work entries from `{agent_dir}/journal/work-log.json` for the date range covered by the period.

By default, the period ends on today's date. Pass `ref_date` to generate a review relative to a different date:

```python
from datetime import date

review = mgr.generate_review(
    agent_dir=Path("agents/dev-cortiva"),
    period=ReviewPeriod.MONTHLY,
    ref_date=date(2026, 3, 29),
)
```

## Saving and Loading Reviews

Reviews are persisted to the agent's journal directory:

```python
path = mgr.save_review(Path("agents/dev-cortiva"), review)
# Writes to: agents/dev-cortiva/journal/review-weekly-2026-03-29.json
```

Load all saved reviews for an agent:

```python
reviews = mgr.load_reviews(Path("agents/dev-cortiva"))
for r in reviews:
    print(f"{r.period.value} ending {r.end_date}: {r.trend}")
```

## Trend Analysis

Each review includes a trend label: `improving`, `stable`, or `declining`. The trend is computed by comparing the current period's metrics to the previous period of the same length.

Four signals are compared:

| Signal | Improving when... |
|---|---|
| `tasks_completed` | Higher than previous period |
| `escalation_ratio` | Lower than previous period |
| `avg_hours_per_day` | Higher than previous period |
| `budget_efficiency` | Lower than previous period |

Each signal contributes +1 (improved), 0 (unchanged), or -1 (worsened) to a score. The final trend is:

- **improving** if the score is >= 2
- **declining** if the score is <= -2
- **stable** otherwise

If there is no data for the previous period, the trend defaults to `stable`.

You can also compute the trend independently:

```python
trend = mgr.compare_to_previous(
    agent_dir=Path("agents/dev-cortiva"),
    period=ReviewPeriod.WEEKLY,
)
print(trend)  # "improving", "stable", or "declining"
```

## Computing Metrics Directly

The `compute_metrics` function is a pure function you can use independently of the review manager:

```python
from datetime import date
from cortiva.core.reviews import WorkEntry, compute_metrics

entries = [
    WorkEntry(date=date(2026, 3, 28), hours_worked=8.0, tasks_completed=5),
    WorkEntry(date=date(2026, 3, 29), hours_worked=7.5, tasks_completed=4),
]

metrics = compute_metrics(entries)
print(f"Total hours: {metrics.total_hours}")
print(f"Days active: {metrics.days_active}")
print(f"Avg hours/day: {metrics.avg_hours_per_day}")
```

## File Layout

```
agents/dev-cortiva/
  journal/
    work-log.json                         # Input: daily work entries
    review-weekly-2026-03-29.json         # Output: saved reviews
    review-monthly-2026-03-29.json
```
