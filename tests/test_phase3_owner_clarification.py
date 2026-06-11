"""Phase 3 tests for clarification.auto_answer_specs (owner_clarification).

auto_answer_specs synthesizes default answers to the kickoff specs so that
balanced/move_fast autonomy builds proceed to code instead of stalling at
awaiting_clarification. It is a PURE function (no LLM/IO) and must reuse the
existing default-inference helpers (parse_user_intent +
format_user_intent_brief_block) rather than duplicate them.
"""

from skyn3t.studio.clarification import (
    auto_answer_specs,
    category_assumption_spec,
    kickoff_specs,
    select_clarification_specs,
    user_keeps_category_defaults,
)


def test_auto_answer_balanced_returns_default_runnable_intent():
    specs = kickoff_specs()
    result = auto_answer_specs(
        specs,
        "Track daily habits with streaks in the browser.",
        mode="balanced",
    )
    assert result["auto_answered"] is True
    # questions/answers stay aligned 1:1 for parse_user_intent
    assert len(result["questions"]) == len(result["answers"]) == len(specs)
    intent = result["user_intent"]
    # Reuses the curated default option ids the chips already expose.
    assert intent["deliverable_kind"] == "runnable"
    assert intent["platform_kind"] == "web_app"
    assert intent["audience_kind"] == "just_me"
    # Free-text must_do defaults from the brief's first sentence.
    assert intent["must_do"] == "Track daily habits with streaks in the browser."
    # Brief block is built via the existing formatter (non-empty).
    assert "User intent" in result["brief_block"]


def test_auto_answer_move_fast_also_proceeds():
    specs = kickoff_specs()
    result = auto_answer_specs(specs, "Launch a new product", mode="move_fast")
    assert result["auto_answered"] is True
    assert result["user_intent"]["deliverable_kind"] == "runnable"
    assert result["user_intent"]["must_do"] == "Launch a new product"


def test_auto_answer_confirm_first_leaves_halt_intact():
    specs = kickoff_specs()
    result = auto_answer_specs(specs, "anything", mode="confirm_first")
    assert result == {"auto_answered": False}


def test_auto_answer_empty_specs_does_not_auto_answer():
    assert auto_answer_specs([], "anything", mode="balanced") == {"auto_answered": False}


def test_auto_answer_unknown_mode_does_not_auto_answer():
    specs = kickoff_specs()
    result = auto_answer_specs(specs, "x", mode="totally_unknown")
    assert result == {"auto_answered": False}


def test_auto_answer_from_selected_generic_brief():
    """The exact zero-code stall: a generic brief in balanced mode must resolve."""
    brief = "Launch a new product"
    specs = select_clarification_specs(brief, mode="balanced")
    assert specs  # generic briefs do produce specs
    result = auto_answer_specs(specs, brief, mode="balanced")
    assert result["auto_answered"] is True
    # The synthesized intent is the safe runnable web-app-for-self default.
    assert result["user_intent"].get("deliverable_kind") == "runnable"
    assert result["user_intent"].get("platform_kind") == "web_app"


def test_auto_answer_category_defaults_keeps_features():
    spec = category_assumption_spec(["Auth-ready routes", "Dark theme"])
    assert spec is not None
    result = auto_answer_specs([spec], "Some brief", mode="balanced")
    assert result["auto_answered"] is True
    # Default for category_defaults is 'keep' -> user_keeps_category_defaults True.
    assert (
        user_keeps_category_defaults(
            result["questions"], result["answers"], [spec]
        )
        is True
    )


def test_auto_answer_unknown_spec_falls_back_to_first_option():
    custom = {
        "id": "theme",
        "question": "Pick a theme",
        "options": [
            {"id": "dark", "label": "Dark mode"},
            {"id": "light", "label": "Light mode"},
        ],
    }
    result = auto_answer_specs([custom], "b", mode="balanced")
    assert result["auto_answered"] is True
    assert result["answers"] == ["Dark mode"]


def test_auto_answer_free_text_falls_back_to_placeholder_without_brief():
    free_text = {
        "id": "must_do",
        "question": "What's the one thing it must do well first?",
        "options": [],
        "free_text": True,
        "placeholder": "Example: track habits",
    }
    result = auto_answer_specs([free_text], "", mode="balanced")
    assert result["auto_answered"] is True
    assert result["answers"] == ["Example: track habits"]
    assert result["user_intent"].get("must_do") == "Example: track habits"


def test_auto_answer_is_pure_does_not_mutate_input_specs():
    specs = kickoff_specs()
    snapshot = [dict(s) for s in specs]
    auto_answer_specs(specs, "Build a thing", mode="balanced")
    assert specs == snapshot
