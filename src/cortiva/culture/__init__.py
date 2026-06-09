"""Culture-health measurement — the People & Culture Lead's instruments.

Culture stewardship is a continuous job: the People & Culture Lead reads
how the workforce is actually feeling, names the hotspots, and proposes
interventions. This package is the **measurement** half — a pure,
deterministic readout of the workforce's emotional weather and diversity
of voice, scored 0-100 on culture health, with a ranked list of who is
struggling and why. The verdict (which intervention, when) stays with the
agent — this only tells her where it hurts.
"""

from cortiva.culture.health import (
    CultureHealth,
    CultureHotspot,
    CultureMember,
    assess_culture_health,
)

__all__ = [
    "CultureHealth",
    "CultureHotspot",
    "CultureMember",
    "assess_culture_health",
]
