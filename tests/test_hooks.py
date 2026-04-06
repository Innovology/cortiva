"""Tests for inbound hook routing and wake-on-hook."""

from __future__ import annotations

from cortiva.core.hooks import HookEvent, HookRoute, HookRouter


class TestHookRoute:
    def test_exact_match(self) -> None:
        route = HookRoute(source="github", events=["push"], agent="dev")
        assert route.matches("github", "push") is True
        assert route.matches("github", "pull_request") is False
        assert route.matches("slack", "push") is False

    def test_wildcard_events(self) -> None:
        route = HookRoute(source="github", events=["*"], agent="dev")
        assert route.matches("github", "push") is True
        assert route.matches("github", "pull_request") is True
        assert route.matches("slack", "push") is False

    def test_wildcard_source(self) -> None:
        route = HookRoute(source="*", events=["*"], agent="pm")
        assert route.matches("github", "push") is True
        assert route.matches("anything", "anything") is True

    def test_multiple_events(self) -> None:
        route = HookRoute(source="github", events=["push", "pull_request"], agent="dev")
        assert route.matches("github", "push") is True
        assert route.matches("github", "pull_request") is True
        assert route.matches("github", "issues") is False


class TestHookRouter:
    def test_route_match(self) -> None:
        router = HookRouter()
        router.load({
            "routes": [
                {"source": "github", "events": ["push"], "agent": "dev-cortiva"},
            ],
        })
        event = router.route("github", "push", {"ref": "main"})
        assert event is not None
        assert event.routed_to == "dev-cortiva"
        assert event.source == "github"

    def test_route_no_match(self) -> None:
        router = HookRouter()
        router.load({
            "routes": [
                {"source": "github", "events": ["push"], "agent": "dev"},
            ],
        })
        event = router.route("slack", "message", {})
        assert event is None

    def test_route_priority(self) -> None:
        router = HookRouter()
        router.load({
            "routes": [
                {
                    "source": "pagerduty",
                    "events": ["incident.trigger"],
                    "agent": "dev-cortiva",
                    "priority": "critical",
                },
            ],
        })
        event = router.route("pagerduty", "incident.trigger", {"title": "prod down"})
        assert event is not None
        assert event.priority == "critical"

    def test_catch_all_route(self) -> None:
        router = HookRouter()
        router.load({
            "routes": [
                {"source": "github", "events": ["push"], "agent": "dev"},
                {"source": "*", "events": ["*"], "agent": "pm"},
            ],
        })
        # Specific match
        event1 = router.route("github", "push", {})
        assert event1 is not None
        assert event1.routed_to == "dev"

        # Catch-all
        event2 = router.route("unknown", "something", {})
        assert event2 is not None
        assert event2.routed_to == "pm"

    def test_should_wake(self) -> None:
        router = HookRouter()
        router.load({
            "routes": [
                {
                    "source": "pagerduty",
                    "events": ["*"],
                    "agent": "dev",
                    "wake_if_sleeping": True,
                },
                {
                    "source": "github",
                    "events": ["push"],
                    "agent": "dev",
                    "wake_if_sleeping": False,
                },
            ],
        })
        event_pd = router.route("pagerduty", "incident", {})
        assert event_pd is not None
        assert router.should_wake(event_pd) is True

        event_gh = router.route("github", "push", {})
        assert event_gh is not None
        assert router.should_wake(event_gh) is False

    def test_pending_for(self) -> None:
        router = HookRouter()
        router.load({
            "routes": [
                {"source": "github", "events": ["*"], "agent": "dev"},
            ],
        })
        router.route("github", "push", {"ref": "main"})
        router.route("github", "pull_request", {"action": "opened"})

        pending = router.pending_for("dev")
        assert len(pending) == 2

        # Queue is cleared after consumption
        assert len(router.pending_for("dev")) == 0

    def test_pending_context(self) -> None:
        router = HookRouter()
        router.load({
            "routes": [
                {
                    "source": "pagerduty",
                    "events": ["*"],
                    "agent": "dev",
                    "priority": "critical",
                },
            ],
        })
        router.route("pagerduty", "incident.trigger", {"title": "prod down"})

        ctx = router.pending_context("dev")
        assert "Inbound Hooks" in ctx
        assert "CRITICAL" in ctx
        assert "prod down" in ctx

    def test_pending_context_empty(self) -> None:
        router = HookRouter()
        assert router.pending_context("dev") == ""

    def test_recent_hooks(self) -> None:
        router = HookRouter()
        router.load({"routes": [{"source": "*", "events": ["*"], "agent": "pm"}]})
        router.route("a", "b", {})
        router.route("c", "d", {})

        recent = router.recent_hooks()
        assert len(recent) == 2
        # Most recent first
        assert recent[0].source == "c"

    def test_hook_event_summary(self) -> None:
        event = HookEvent(
            id="abc",
            source="github",
            event_type="push",
            payload={"ref": "refs/heads/main"},
            priority="high",
        )
        summary = event.summary()
        assert "github/push" in summary
        assert "high" in summary
