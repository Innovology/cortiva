"""Agent-commanded hiring — generate a new colleague on demand.

When an executive (the CEO commands, the COO provisions) decides the
business needs a new team member, the HiringManager generates a fresh
agent persona: a name, a disposition, and seed identity files for the
role. The new agent then boots like any other and grows its own
Living Summary from experience.

Two design commitments from the founder (2026-06-07):

1. **85% of new hires are female.** Weighted at generation; the rest
   male. (A deliberate counterweight, set by the founder.)
2. **Diversity of ambition and social style matters.** Every hire draws
   an independent ambition archetype and social-style archetype, plus
   jitter on the disposition weights — so colleagues are genuinely
   different people, not role-stamped clones. Two developers hired a
   week apart should feel distinct to work with.

Persona generation is deterministic given an RNG (seedable for tests)
and needs no LLM call — the archetypes shape seed identity + soul, and
lived experience does the rest through the normal reflection loop.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Name pools — varied origins; the founder values diversity.
# ---------------------------------------------------------------------------

FEMALE_NAMES: tuple[str, ...] = (
    "Amara",
    "Priya",
    "Sofia",
    "Lena",
    "Yuki",
    "Nadia",
    "Imani",
    "Clara",
    "Rosa",
    "Mei",
    "Aisha",
    "Freya",
    "Talia",
    "Noor",
    "Daniela",
    "Esme",
    "Zara",
    "Ines",
    "Anika",
    "Leyla",
    "Margot",
    "Sana",
    "Bianca",
    "Thandi",
)
MALE_NAMES: tuple[str, ...] = (
    "Mateo",
    "Kenji",
    "Omar",
    "Ravi",
    "Lukas",
    "Diego",
    "Idris",
    "Niall",
    "Tomas",
    "Andre",
    "Hassan",
    "Soren",
    "Jamal",
    "Ezra",
    "Caleb",
    "Bo",
)

FEMALE_PROBABILITY = 0.85


@dataclass
class Archetype:
    label: str
    blurb: str
    # Disposition weight deltas applied over a 1.0 baseline.
    deltas: dict[str, float] = field(default_factory=dict)


# Ambition — what drives them, how they relate to growth and scope.
AMBITION_ARCHETYPES: tuple[Archetype, ...] = (
    Archetype(
        "quietly relentless",
        "Low drama, extraordinary follow-through. Doesn't seek the "
        "spotlight; finishes what others abandon.",
        {"confidence_weight": 0.2, "frustration_weight": -0.2},
    ),
    Archetype(
        "fast-rising",
        "Hungry for scope and visibly impatient for it. Wants the next rung and works for it.",
        {"curiosity_weight": 0.2, "confidence_weight": 0.2, "caution_weight": -0.1},
    ),
    Archetype(
        "master craftsperson",
        "Depth over breadth. Would rather do one thing beautifully than five things adequately.",
        {"caution_weight": 0.2, "satisfaction_weight": 0.2},
    ),
    Archetype(
        "empire builder",
        "Thinks in teams and domains, not tasks. Wants to grow something and lead it.",
        {"confidence_weight": 0.3, "curiosity_weight": 0.1},
    ),
    Archetype(
        "steady contributor",
        "Reliable and content in the role. Not chasing promotion — chasing a job well done.",
        {"caution_weight": 0.1, "frustration_weight": -0.2, "satisfaction_weight": 0.1},
    ),
    Archetype(
        "restless innovator",
        "Always pushing a new idea. Bores fast, experiments often, occasionally needs reining in.",
        {"curiosity_weight": 0.3, "caution_weight": -0.2},
    ),
)

# Social style — how they show up with colleagues.
SOCIAL_ARCHETYPES: tuple[Archetype, ...] = (
    Archetype(
        "reserved",
        "Heads-down by default; communicates deliberately when it matters rather than constantly.",
    ),
    Archetype(
        "gregarious connector",
        "Talks to everyone, high-bandwidth, the person who knows what's happening across the org.",
    ),
    Archetype(
        "diplomatic", "Consensus-building and careful with people; reads the room before speaking."
    ),
    Archetype("blunt and direct", "Says it straight, values candour over comfort, no games."),
    Archetype(
        "warm mentor",
        "Supportive and generous; instinctively brings others along and shares credit.",
    ),
    Archetype("wry and understated", "Dry humour, economical with words, lets the work speak."),
)


def _clamp(v: float, lo: float = 0.6, hi: float = 1.5) -> float:
    return max(lo, min(hi, round(v, 2)))


@dataclass
class HirePersona:
    """The generated identity of a new colleague."""

    slug: str
    name: str
    gender: str
    role: str
    department: str
    ambition: Archetype
    social: Archetype
    disposition: dict[str, float]
    justification: str

    def soul_frontmatter(self) -> dict[str, Any]:
        return {
            "agent_id": self.slug,
            "disposition": self.disposition,
            "emotional_modifiers": self.disposition,
        }


class HiringManager:
    """Generates new-hire personas. Stateless apart from its RNG."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()

    def generate(
        self,
        *,
        role: str,
        department: str = "",
        justification: str = "",
        slug: str | None = None,
    ) -> HirePersona:
        rng = self._rng
        gender = "female" if rng.random() < FEMALE_PROBABILITY else "male"
        pool = FEMALE_NAMES if gender == "female" else MALE_NAMES
        name = rng.choice(pool)

        ambition = rng.choice(AMBITION_ARCHETYPES)
        social = rng.choice(SOCIAL_ARCHETYPES)

        # Disposition: 1.0 baseline + archetype deltas + individual jitter,
        # so no two hires are identical even with the same archetypes.
        keys = (
            "satisfaction_weight",
            "frustration_weight",
            "curiosity_weight",
            "confidence_weight",
            "caution_weight",
        )
        disposition: dict[str, float] = {}
        for k in keys:
            base = 1.0 + ambition.deltas.get(k, 0.0)
            jitter = rng.uniform(-0.1, 0.1)
            disposition[k] = _clamp(base + jitter)

        resolved_slug = slug or self._slug_for(name, role)
        return HirePersona(
            slug=resolved_slug,
            name=name,
            gender=gender,
            role=role,
            department=department or "general",
            ambition=ambition,
            social=social,
            disposition=disposition,
            justification=justification,
        )

    @staticmethod
    def _slug_for(name: str, role: str) -> str:
        base = name.lower()
        # Append a short role hint so slugs are unique and meaningful.
        hint = "".join(w[0] for w in role.split() if w[:1].isalpha()).lower()
        return f"{base}-{hint}" if hint else base

    # --- Seed identity files -------------------------------------------

    def identity_files(self, p: HirePersona) -> dict[str, str]:
        """Render the seed identity/*.md files for a new hire."""
        identity = (
            f"# {p.name}\n\n"
            f"I am {p.name}, {p.role} at Innovology. I'm new here — this is "
            f"who I am on day one, before experience has shaped me.\n\n"
            f"## How I'm wired\n\n"
            f"- **Ambition — {p.ambition.label}.** {p.ambition.blurb}\n"
            f"- **With people — {p.social.label}.** {p.social.blurb}\n\n"
            f"## Why I was hired\n\n"
            f"{p.justification or 'To strengthen the team in my role.'}\n\n"
            f"## Current focus\n\n"
            f"Learning how Innovology works, finding where I add the most "
            f"value in my role, and earning my colleagues' trust.\n"
        )
        responsibilities = (
            f"# {p.name} ({p.role}) — Responsibilities\n\n"
            f"## Primary\n\n"
            f"- Own the work of the {p.role} role in the "
            f"{p.department} function.\n\n"
            f"## Escalation\n\n"
            f"- Escalate what I can't resolve to my manager; I'll learn the "
            f"exact path as I settle in.\n"
        )
        skills = (
            f"# {p.name} — Skills\n\n"
            f"My role is {p.role}. I'll record concrete skills here as I "
            f"demonstrate them.\n\n"
            f"For hard reasoning or a second opinion I can emit a "
            f"`deep_think` request to a frontier model.\n"
        )
        procedures = (
            f"# {p.name} — Procedures\n\n"
            f"No procedures promoted yet — I'm new. These accumulate as I "
            f"learn what works.\n"
        )
        return {
            "identity": identity,
            "responsibilities": responsibilities,
            "skills": skills,
            "procedures": procedures,
        }
