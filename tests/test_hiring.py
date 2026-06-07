"""Tests for agent-commanded hiring — the founder's spec: 85% female,
diverse ambition and social style."""

from __future__ import annotations

import random
from collections import Counter

from cortiva.core.hiring import (
    AMBITION_ARCHETYPES,
    SOCIAL_ARCHETYPES,
    HiringManager,
)


def test_85_percent_female_over_a_large_sample():
    mgr = HiringManager(random.Random(42))
    genders = Counter(
        mgr.generate(role="Developer").gender for _ in range(2000)
    )
    frac_female = genders["female"] / 2000
    # 0.85 target; allow sampling slack
    assert 0.82 <= frac_female <= 0.88, genders
    assert genders["male"] > 0  # not 100% — real diversity


def test_ambition_and_social_styles_are_diverse():
    mgr = HiringManager(random.Random(7))
    hires = [mgr.generate(role="Developer") for _ in range(200)]
    ambitions = {h.ambition.label for h in hires}
    socials = {h.social.label for h in hires}
    # Most of the archetype space gets used — genuine variety
    assert len(ambitions) >= len(AMBITION_ARCHETYPES) - 1
    assert len(socials) >= len(SOCIAL_ARCHETYPES) - 1


def test_dispositions_vary_between_hires():
    mgr = HiringManager(random.Random(1))
    a = mgr.generate(role="Developer")
    b = mgr.generate(role="Developer")
    # Jitter + independent archetypes → not clones
    assert a.disposition != b.disposition or a.ambition != b.ambition


def test_disposition_weights_clamped_sane():
    mgr = HiringManager(random.Random(3))
    for _ in range(500):
        for v in mgr.generate(role="X").disposition.values():
            assert 0.6 <= v <= 1.5


def test_identity_files_render_persona():
    mgr = HiringManager(random.Random(9))
    p = mgr.generate(role="Developer", department="engineering",
                     justification="dev capacity gap")
    files = mgr.identity_files(p)
    assert set(files) == {"identity", "responsibilities", "skills", "procedures"}
    assert p.name in files["identity"]
    assert p.ambition.label in files["identity"]
    assert p.social.label in files["identity"]
    assert "dev capacity gap" in files["identity"]


def test_slug_is_unique_and_meaningful():
    mgr = HiringManager(random.Random(5))
    p = mgr.generate(role="Product Owner")
    assert p.name.lower() in p.slug
    assert p.slug == p.slug.lower()
