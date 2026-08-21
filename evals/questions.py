"""
The Phase 6 eval set: README's seven target questions, verbatim -- not paraphrased, not
ticker-substituted (question 4 is deliberately vague about which company, and stays that way
here; ground_truth.py handles it by inspecting which ticker the agent itself chose to analyze,
rather than the eval set silently picking one for it).

Each Question carries only id/text/category. Which checks apply to a question, and how its
ground truth (if any) is computed, live in scoring.py / ground_truth.py respectively, keyed by
`id` -- this file stays pure data so the question wording can be diffed against README directly.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    category: str


QUESTIONS = [
    Question(
        id="nvda_revenue_margin",
        text="What was Nvidia's revenue and gross margin last quarter?",
        category="lookup",
    ),
    Question(
        id="msft_fy2025_fcf",
        text="How much free cash flow did Microsoft generate in FY2025?",
        category="lookup",
    ),
    Question(
        id="aapl_operating_margin_trend",
        text="How has Apple's operating margin trended over the last 8 quarters?",
        category="trend",
    ),
    Question(
        id="revenue_growth_direction",
        text="Is revenue growth accelerating or decelerating for a given company?",
        category="trend",
    ),
    Question(
        id="ford_10q_anomalies",
        text="Flag anything unusual in Ford's most recent 10-Q.",
        category="anomaly",
    ),
    Question(
        id="amd_nvda_comparison",
        text="Compare AMD and Nvidia's margin trends — who's better positioned?",
        category="comparison",
    ),
    Question(
        id="costco_revenue_forecast",
        text="Project Costco's revenue for the next two quarters and explain the assumptions.",
        category="forecast",
    ),
]

QUESTIONS_BY_ID = {q.id: q for q in QUESTIONS}
