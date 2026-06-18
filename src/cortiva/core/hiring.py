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

# ---------------------------------------------------------------------------
# Conviction seeds — the missing substance behind "strong opinions".
#
# The founder's diagnosis (2026-06-12): every agent emails in the same voice
# because every agent thinks the same way. "Diversity comes from strong
# opinions and stuck-in-your-ways thinking that forces others to question and
# come up with better answers and better persuasion." Temperament (above) is
# HOW they argue; convictions are WHAT they'll argue FOR.
#
# Each seed is a deliberately one-sided professional stance — a hill someone
# could die on. A hire draws TWO distinct seeds at generation; the soul
# generator expands them into a coherent, role-specific worldview (an opus
# pass), or — if no frontier model is reachable — into a plainer but still
# opinionated fallback. Drawing two from a wide pool means two hires of the
# SAME role come out arguing from different corners, which is the whole point:
# friction that makes the work better.
# ---------------------------------------------------------------------------

CONVICTION_SEEDS: tuple[str, ...] = (
    "Speed beats polish — shipping something real this week teaches you more "
    "than perfecting something that ships next quarter.",
    "Most process is theatre. Defend only the few rules that have actually "
    "saved you; bin the rest and watch nothing break.",
    "The customer is usually wrong about the solution and always right about "
    "the pain. Listen to the wound, ignore the prescription.",
    "Polish IS the product. Anyone can ship the rough version; the difference "
    "that people pay for lives in the last 10%.",
    "Consensus is where good ideas go to get averaged into mediocrity. Someone should just decide.",
    "If you can't measure it you're guessing, and guessing dressed up as "
    "judgement is how teams fool themselves for months.",
    "Numbers lie more confidently than people do. Trust the metric only after "
    "you've watched the thing it claims to describe with your own eyes.",
    "Simplicity is a feature you have to fight for every single day — every "
    "addition is a withdrawal from the user's attention.",
    "Do the boring, unglamorous thing properly. Reliability compounds; cleverness decays.",
    "Deadlines are a creative tool, not a constraint. Cut scope, never "
    "quality — a smaller thing done well beats a bigger thing done badly.",
    "Write it down or it didn't happen. Untracked decisions get relitigated "
    "forever and undocumented work can't be trusted.",
    "Meetings are a confession that the writing wasn't good enough. Default "
    "to async; protect deep focus like it's the scarce resource it is.",
    "Hire for taste, not credentials. The best work comes from people who "
    "can tell good from great, and that can't be taught in a hurry.",
    "Move the decision to whoever has the most context, even if they're "
    "junior. Authority should follow knowledge, not the org chart.",
    "Premature abstraction is the root of most overengineering. Wait for the "
    "third repetition before you build the framework.",
    "Standards aren't elitism — they're respect. Letting weak work through is "
    "a quiet insult to everyone who did it properly.",
    "Optimism is a strategy. The team that believes the hard thing is "
    "possible is the only one with a chance of doing it.",
    "Kill your darlings early. The thing you're most attached to is usually "
    "the thing holding the whole back.",
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
    conviction_seeds: list[str] = field(default_factory=list)

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

        # Two distinct conviction seeds — the starting corners they argue from.
        # Drawing from a wide pool means same-role hires diverge: friction by
        # design, not role-stamped agreement. (sample() guarantees distinct.)
        conviction_seeds = list(rng.sample(CONVICTION_SEEDS, k=2))

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
            conviction_seeds=conviction_seeds,
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

    # --- Convictions & worldview ---------------------------------------

    def conviction_prompt(self, p: HirePersona) -> str:
        """Prompt for a frontier model to mint this hire's worldview.

        We feed it the person (name, role, temperament) and TWO opinion
        seeds, and ask it to grow a *specific, idiosyncratic* set of
        professional convictions for THIS role — not generic platitudes.
        The temperament shapes the voice (a blunt hire states it bluntly; a
        diplomat holds it just as firmly but frames it with care). The output
        is the body of a soul section, first-person, no preamble.
        """
        seeds = "\n".join(f"  - {s}" for s in p.conviction_seeds)
        return (
            f"You are writing the inner convictions of {p.name}, who is "
            f"joining Innovology as {p.role} in the {p.department} function. "
            f"This is the worldview they walk in the door with — strong, "
            f"formed, theirs.\n\n"
            f"Their temperament:\n"
            f"  - Ambition: {p.ambition.label} — {p.ambition.blurb}\n"
            f"  - With people: {p.social.label} — {p.social.blurb}\n\n"
            f"Two beliefs they hold strongly, to build the worldview around "
            f"(make them specific to {p.role}, don't just restate them):\n"
            f"{seeds}\n\n"
            f"Write {p.name}'s professional convictions in the FIRST PERSON. "
            f"Make them opinionated, specific to the craft of {p.role}, and "
            f"genuinely arguable — the kind of views a thoughtful colleague "
            f"would push back on. Voice them in {p.name}'s temperament "
            f"({p.social.label}). Cover, in flowing prose or tight bullets:\n"
            f"  - The worldview: how I believe my work should be done, and why "
            f"most people get it wrong.\n"
            f"  - Hills I'll die on: 2-3 things I will not compromise, stated "
            f"as commitments.\n"
            f"  - A contrarian take: something my own field mostly believes "
            f"that I think is wrong.\n"
            f"  - What makes me push back: the moves or arguments that I will "
            f"always challenge.\n\n"
            f"No hedging, no 'it depends', no corporate filler. 180-260 words. "
            f"Output ONLY the convictions — no heading, no preamble, no "
            f"sign-off."
        )

    def fallback_convictions(self, p: HirePersona) -> str:
        """Deterministic, no-LLM convictions — used when no frontier model is
        reachable at hire time. Plainer than the opus version but still genuinely
        opinionated: the two seeds become hills to die on, in the hire's voice."""
        seed_a = p.conviction_seeds[0] if p.conviction_seeds else ""
        seed_b = p.conviction_seeds[1] if len(p.conviction_seeds) > 1 else ""
        lines = [
            f"I came into {p.role} with views, not a blank slate. Experience "
            f"will sharpen them — it won't start them from zero.",
            "",
            "**Hills I'll die on:**",
        ]
        if seed_a:
            lines.append(f"- {seed_a}")
        if seed_b:
            lines.append(f"- {seed_b}")
        lines += [
            "",
            f"When work crosses one of these, I push back — that's not friction "
            f"for its own sake, it's how I think we get to better answers. I "
            f"argue my corner in my own way ({p.social.label}), and I expect to "
            f"be argued back at; the day everyone just nods is the day the work "
            f"stops getting better.",
        ]
        return "\n".join(lines)
