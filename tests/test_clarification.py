"""Tests for plain-language Studio clarification helpers."""

from skyn3t.studio.clarification import (
    apply_user_intent_plan,
    category_assumption_spec,
    kickoff_specs,
    parse_user_intent,
    select_clarification_specs,
    user_keeps_category_defaults,
)


def test_kickoff_specs_use_plain_language():
    specs = kickoff_specs()
    questions = [spec["question"] for spec in specs]
    assert any("get at the end" in q.lower() for q in questions)
    assert any("what kind of thing" in q.lower() for q in questions)
    assert not any("fullstack" in q.lower() for q in questions)


def test_parse_user_intent_from_chip_labels():
    specs = kickoff_specs()
    questions = [spec["question"] for spec in specs]
    answers = [
        "Something I can run or use",
        "Web app (works in a browser)",
        "Just me",
        "Track daily habits",
    ]
    intent = parse_user_intent(questions, answers, specs)
    assert intent["deliverable_kind"] == "runnable"
    assert intent["platform_kind"] == "web_app"
    assert intent["audience_kind"] == "just_me"
    assert intent["must_do"] == "Track daily habits"


def test_parse_user_intent_runnable_wins_over_design_in_same_answer():
    """Regression: 'Complete build working with great design' must stay runnable."""
    specs = kickoff_specs()
    questions = [spec["question"] for spec in specs]
    answers = [
        "Complete build working - with great design and branding name ChoateLab full product",
        "Website Docker",
        "People with home labs",
        "Work better than Homarr and easier to configure - configure everything in UI instead of env files etc",
    ]
    intent = parse_user_intent(questions, answers, specs)
    assert intent["deliverable_kind"] == "runnable"
    assert intent["platform_kind"] == "web_app"


def test_apply_user_intent_plan_skips_code_for_planning():
    chosen = [
        "BrainstormAgent",
        "ArchitectAgent",
        "CodeAgent",
        "ReviewerAgent",
    ]
    arts = ["brainstorm.md", "architecture.md", "(source files)", "review.md"]
    why: dict[str, str] = {}
    chosen, arts, why = apply_user_intent_plan(
        {"deliverable_kind": "plan"},
        chosen,
        arts,
        why,
    )
    assert "CodeAgent" not in chosen
    assert "CodeAgent_skipped" in why


def test_select_clarification_specs_on_generic_brief():
    specs = select_clarification_specs("Launch a new product", mode="balanced")
    assert len(specs) >= 2
    assert specs[0]["id"] == "outcome"


def test_confirm_first_skips_specs_already_in_detailed_brief():
    brief = (
        "Build a personal daily habit tracker as a single-page web app. "
        "React + Vite + Tailwind. localStorage only. npm run dev must work. "
        "Daily check-ins with streaks."
    )
    specs = select_clarification_specs(brief, mode="confirm_first")
    ids = {spec["id"] for spec in specs}
    assert "outcome" not in ids
    assert "platform" not in ids


def test_category_assumption_spec_lists_hints():
    spec = category_assumption_spec(["Auth-ready routes", "Dark theme"])
    assert spec is not None
    assert spec["id"] == "category_defaults"
    assert "Auth-ready routes" in spec["question"]


def test_user_keeps_category_defaults_respects_skip():
    spec = category_assumption_spec(["Auth-ready routes"])
    assert spec is not None
    question = spec["question"]
    assert user_keeps_category_defaults([question], ["No, keep it simpler"], [spec]) is False
    assert user_keeps_category_defaults([question], ["Yes, include typical features"], [spec]) is True
