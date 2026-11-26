"""Shared definitions, used by every tool (stats, phases, opponent report) and matching season_report.R.

Efficiency, as a share of all the touches of that skill:
- serve:     (# + '+' + '/' - '=' - '-') / N     ('/' = reception returned the ball: good for the server)
- reception: (# + '+' - '=' - '-') / N
- attack and the other skills: (# - '=' - '/') / N
"""

EFFICIENCY = {
    "Serve": (("#", "+", "/"), ("=", "-")),
    "Reception": (("#", "+"), ("=", "-")),
}
DEFAULT_EFFICIENCY = (("#",), ("=", "/"))


def efficiency_codes(skill: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return EFFICIENCY.get(skill, DEFAULT_EFFICIENCY)


def efficiency_sql(skill: str, col: str = "evaluation_code", where: str = "true") -> str:
    """Percent efficiency over the rows matching `where` (a SQL condition); codes are constants, not user input."""
    good, bad = efficiency_codes(skill)
    q = lambda codes: ", ".join(f"'{c}'" for c in codes)
    return (f"round(100.0 * (count(*) filter (where {where} and {col} in ({q(good)})) - "
            f"count(*) filter (where {where} and {col} in ({q(bad)}))) / nullif(count(*) filter (where {where}), 0), 1)")
