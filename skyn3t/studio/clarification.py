"""Plain-language clarification helpers for Studio kickoff."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

OUTCOME_SPEC: Dict[str, Any] = {
    "id": "outcome",
    "question": "What should you get at the end?",
    "options": [
        {"id": "runnable", "label": "Something I can run or use"},
        {"id": "plan", "label": "A plan or write-up"},
        {"id": "design", "label": "Design or branding only"},
        {"id": "content", "label": "Content or copy only"},
    ],
}

PLATFORM_SPEC: Dict[str, Any] = {
    "id": "platform",
    "question": "What kind of thing is this?",
    "options": [
        {"id": "website", "label": "Website"},
        {"id": "web_app", "label": "Web app (works in a browser)"},
        {"id": "phone_app", "label": "Phone app (iPhone or Android)"},
        {"id": "computer_app", "label": "Computer app (Mac or Windows)"},
        {"id": "not_sure", "label": "Not sure yet"},
    ],
}

AUDIENCE_SPEC: Dict[str, Any] = {
    "id": "audience",
    "question": "Who is this mainly for?",
    "options": [
        {"id": "just_me", "label": "Just me"},
        {"id": "my_team", "label": "My team"},
        {"id": "public", "label": "Anyone on the internet"},
    ],
}

WORKFLOW_SPEC: Dict[str, Any] = {
    "id": "must_do",
    "question": "What's the one thing it must do well first?",
    "options": [],
    "free_text": True,
    "placeholder": "Example: track habits, show server status, collect signups…",
}

SPEC_BY_ID: Dict[str, Dict[str, Any]] = {
    "outcome": OUTCOME_SPEC,
    "platform": PLATFORM_SPEC,
    "audience": AUDIENCE_SPEC,
    "must_do": WORKFLOW_SPEC,
}

KICKOFF_SPEC_IDS: Tuple[str, ...] = ("outcome", "platform", "audience", "must_do")


def kickoff_specs() -> List[Dict[str, Any]]:
    """Fixed kickoff set for confirm_first / force modes."""
    return [dict(SPEC_BY_ID[spec_id]) for spec_id in KICKOFF_SPEC_IDS]


def clarification_payload(specs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build brainstorm/runner payload from selected specs."""
    questions: List[str] = []
    question_options: List[Dict[str, Any]] = []
    for spec in specs[:4]:
        question = str(spec.get("question") or "").strip()
        if not question:
            continue
        questions.append(question)
        entry: Dict[str, Any] = {
            "id": spec.get("id"),
            "question": question,
            "options": list(spec.get("options") or []),
        }
        if spec.get("free_text"):
            entry["free_text"] = True
        if spec.get("placeholder"):
            entry["placeholder"] = spec["placeholder"]
        question_options.append(entry)
    return {"questions": questions, "question_options": question_options}


def select_clarification_specs(
    brief: str,
    *,
    force: bool = False,
    mode: str = "balanced",
) -> List[Dict[str, Any]]:
    """Pick up to four plain-language specs worth asking for this brief."""
    text = re.sub(r"\s+", " ", (brief or "").lower()).strip()
    if force:
        return kickoff_specs()
    words = [word for word in re.split(r"[^a-z0-9]+", text) if word]
    generic_brief = len(words) <= 12

    shape_signals = (
        "landing page",
        "marketing site",
        "website",
        "web app",
        "webapp",
        "dashboard",
        "portal",
        "mobile app",
        "phone app",
        "iphone",
        "android",
        "desktop app",
        "computer app",
        "mac app",
        "windows app",
    )
    deliverable_signals = (
        "working app",
        "fully working",
        "working prototype",
        "npm run dev",
        "npm run build",
        "codebase",
        "scaffold",
        "landing page",
        "marketing site",
        "website",
        "web app",
        "single-page",
        "localstorage",
        "habit tracker",
        "mobile app",
        "desktop app",
        "brand kit",
        "design system",
        "docs",
        "documentation",
        "plan",
        "strategy",
        "content",
        "copy",
    )
    user_signals = (
        "user",
        "customer",
        "team",
        "admin",
        "public",
        "personal",
        "family",
        "company",
    )
    workflow_signals = (
        "upload",
        "search",
        "track",
        "streak",
        "habit",
        "check-in",
        "check in",
        "check-ins",
        "checkout",
        "book",
        "schedule",
        "sync",
        "message",
        "create",
        "edit",
        "share",
        "review",
        "approve",
        "export",
    )
    ambiguous_outcome_signals = (
        "product",
        "platform",
        "solution",
        "tool",
        "system",
        "idea",
        "launch",
    )

    selected: List[Dict[str, Any]] = []

    needs_outcome = not any(signal in text for signal in deliverable_signals) and (
        generic_brief or any(signal in text for signal in ambiguous_outcome_signals)
    )
    if needs_outcome:
        selected.append(dict(OUTCOME_SPEC))

    needs_platform = not any(signal in text for signal in shape_signals)
    if needs_platform and (needs_outcome or generic_brief):
        selected.append(dict(PLATFORM_SPEC))

    needs_audience = generic_brief or not any(signal in text for signal in user_signals)
    if needs_audience:
        selected.append(dict(AUDIENCE_SPEC))

    needs_workflow = generic_brief or not any(signal in text for signal in workflow_signals)
    if needs_workflow:
        selected.append(dict(WORKFLOW_SPEC))

    seen: set[str] = set()
    deduped: List[Dict[str, Any]] = []
    for spec in selected:
        spec_id = str(spec.get("id") or "")
        if not spec_id or spec_id in seen:
            continue
        seen.add(spec_id)
        deduped.append(spec)
    return deduped[:4]


def merge_clarification_specs(
    *groups: List[Dict[str, Any]],
    limit: int = 4,
) -> List[Dict[str, Any]]:
    """Merge spec lists by id while preserving order."""
    merged: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for spec in group:
            spec_id = str(spec.get("id") or "")
            if not spec_id or spec_id in seen:
                continue
            seen.add(spec_id)
            merged.append(dict(spec))
            if len(merged) >= limit:
                return merged
    return merged


# Sensible default option ids per kickoff spec, used when auto-answering so the
# build proceeds instead of stalling at awaiting_clarification. These reuse the
# same option ids the chips expose and that parse_user_intent already understands.
_AUTO_DEFAULT_OPTION_IDS: Dict[str, str] = {
    "outcome": "runnable",
    "platform": "web_app",
    "audience": "just_me",
    "category_defaults": "keep",
}

# Auto-answer modes that should synthesize defaults and proceed to code.
_AUTO_ANSWER_MODES: frozenset[str] = frozenset({"balanced", "move_fast"})


def _default_option_for_spec(spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pick a sensible default option for a multiple-choice spec.

    Prefers the curated default id for known specs (outcome/platform/audience/
    category_defaults) and falls back to the first option otherwise.
    """
    options = [opt for opt in (spec.get("options") or []) if isinstance(opt, dict)]
    if not options:
        return None
    spec_id = str(spec.get("id") or "")
    preferred = _AUTO_DEFAULT_OPTION_IDS.get(spec_id)
    if preferred:
        for option in options:
            if str(option.get("id") or "") == preferred:
                return option
    return options[0]


def _default_free_text_answer(spec: Dict[str, Any], brief: str) -> str:
    """Synthesize a default free-text answer from the brief for open specs."""
    text = re.sub(r"\s+", " ", (brief or "")).strip()
    if text:
        # Prefer the first sentence as the "one thing it must do well" hint.
        first_sentence = re.split(r"(?<=[.!?])\s+", text)[0].strip()
        candidate = first_sentence or text
        return candidate[:500]
    placeholder = str(spec.get("placeholder") or "").strip()
    if placeholder:
        return placeholder[:500]
    return "Build the core experience the brief describes."


def _default_answer_for_spec(spec: Dict[str, Any], brief: str) -> str:
    """Return the default answer text for a single spec (label or free text)."""
    option = _default_option_for_spec(spec)
    if option is not None:
        label = str(option.get("label") or "").strip()
        if label:
            return label
        opt_id = str(option.get("id") or "").strip()
        if opt_id:
            return opt_id
    return _default_free_text_answer(spec, brief)


def auto_answer_specs(
    specs: List[Dict[str, Any]],
    brief: str,
    *,
    mode: str,
) -> Dict[str, Any]:
    """Synthesize default answers to kickoff specs so auto-* builds proceed.

    Pure function (no LLM/IO): for balanced/move_fast autonomy it picks the
    curated default option id (or first option) for each multiple-choice spec
    and derives a free-text default from the brief, then reuses
    parse_user_intent + format_user_intent_brief_block to build the same
    user_intent/brief_block the interactive clarification path would.

    Returns {questions, answers, user_intent, brief_block, auto_answered: True}
    on success, or {auto_answered: False} for confirm_first / empty specs so the
    runner leaves the existing awaiting_clarification halt intact.
    """
    normalized_mode = str(mode or "").strip().lower()
    cleaned_specs = [spec for spec in (specs or []) if isinstance(spec, dict)]
    if normalized_mode not in _AUTO_ANSWER_MODES or not cleaned_specs:
        return {"auto_answered": False}

    payload = clarification_payload(cleaned_specs)
    question_options = payload.get("question_options") or []
    # Build questions/answers aligned the same way clarification_payload does
    # (skipping blank questions) so parse_user_intent maps each answer back to
    # the right spec id via question_options.
    questions: List[str] = []
    answers: List[str] = []
    for spec in cleaned_specs[:4]:
        question = str(spec.get("question") or "").strip()
        if not question:
            continue
        questions.append(question)
        answers.append(_default_answer_for_spec(spec, brief))

    if not questions:
        return {"auto_answered": False}

    user_intent = parse_user_intent(questions, answers, question_options)
    brief_block = format_user_intent_brief_block(user_intent)
    return {
        "questions": questions,
        "answers": answers,
        "user_intent": user_intent,
        "brief_block": brief_block,
        "auto_answered": True,
    }


def parse_user_intent(
    questions: List[str],
    answers: List[str],
    question_options: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Turn clarification Q&A into structured planner hints."""
    intent: Dict[str, Any] = {}
    options_by_question = {
        str(entry.get("question") or "").strip(): entry
        for entry in (question_options or [])
        if isinstance(entry, dict)
    }

    for question, raw_answer in zip(questions, answers):
        answer = str(raw_answer or "").strip()
        if not answer:
            continue
        entry = options_by_question.get(str(question).strip()) or {}
        spec_id = str(entry.get("id") or "")
        matched_option_id = _match_option_id(entry, answer)

        if spec_id == "outcome" or _looks_like_outcome_question(question):
            key = matched_option_id or _infer_outcome_id(answer)
            if key:
                intent["deliverable_kind"] = key
        elif spec_id == "platform" or _looks_like_platform_question(question):
            answer_lower = answer.lower()
            if any(
                token in answer_lower
                for token in ("docker", "docker compose", "compose up")
            ):
                intent["platform_kind"] = "web_app"
            else:
                key = matched_option_id or _infer_platform_id(answer)
                if key:
                    intent["platform_kind"] = key
        elif spec_id == "audience" or _looks_like_audience_question(question):
            key = matched_option_id or _infer_audience_id(answer)
            if key:
                intent["audience_kind"] = key
        elif spec_id == "must_do" or _looks_like_workflow_question(question):
            intent["must_do"] = answer[:500]

    return intent


def format_user_intent_brief_block(user_intent: Dict[str, Any]) -> str:
    """Append a structured, planner-friendly block to the brief."""
    if not user_intent:
        return ""
    lines = ["## User intent (plain-language clarifications)"]
    deliverable = user_intent.get("deliverable_kind")
    platform = user_intent.get("platform_kind")
    audience = user_intent.get("audience_kind")
    must_do = user_intent.get("must_do")

    deliverable_labels = {
        "runnable": "Runnable software the user can run or use",
        "plan": "Planning or strategy document — not runnable code",
        "design": "Design or branding deliverables — not runnable code",
        "content": "Content or copy — not runnable code",
    }
    platform_labels = {
        "website": "Website (mostly pages to read)",
        "web_app": "Web app in the browser",
        "phone_app": "Phone app (start with responsive web/PWA unless native is explicitly required)",
        "computer_app": "Computer/desktop app (start with web or Electron-style unless native is required)",
        "not_sure": "Platform not decided — prefer a simple web app first",
    }
    audience_labels = {
        "just_me": "Primary user: just the builder",
        "my_team": "Primary user: an internal team",
        "public": "Primary user: public internet users (may need accounts later)",
    }

    if deliverable:
        lines.append(f"- Deliverable: {deliverable_labels.get(str(deliverable), deliverable)}")
    if platform:
        lines.append(f"- Platform: {platform_labels.get(str(platform), platform)}")
    if audience:
        lines.append(f"- Audience: {audience_labels.get(str(audience), audience)}")
    if must_do:
        lines.append(f"- Must-do workflow: {must_do}")

    lines.append(
        "- Use everyday product shape; do not ask the user for technical stack jargon."
    )
    return "\n".join(lines) + "\n"


def category_assumption_spec(hints: List[str]) -> Optional[Dict[str, Any]]:
    """Build a confirm-first question for implicit category features."""
    cleaned = [str(item).strip() for item in hints if str(item).strip()]
    if not cleaned:
        return None
    preview = "; ".join(cleaned[:4])
    if len(cleaned) > 4:
        preview += f"; +{len(cleaned) - 4} more"
    return {
        "id": "category_defaults",
        "question": (
            "SkyN3t may add typical features for this product type "
            f"({preview}). Keep them?"
        ),
        "options": [
            {"id": "keep", "label": "Yes, include typical features"},
            {"id": "skip", "label": "No, keep the scope minimal"},
        ],
        "free_text": True,
        "placeholder": "Or say what to drop or change…",
    }


def user_keeps_category_defaults(
    questions: List[str],
    answers: List[str],
    question_options: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """Return False when the user opted out of implicit category features."""
    options_by_question = {
        str(entry.get("question") or "").strip(): entry
        for entry in (question_options or [])
        if isinstance(entry, dict)
    }
    for question, raw_answer in zip(questions, answers):
        answer = str(raw_answer or "").strip().lower()
        if not answer:
            continue
        entry = options_by_question.get(str(question).strip()) or {}
        if str(entry.get("id") or "") != "category_defaults":
            continue
        matched = _match_option_id(entry, str(raw_answer or ""))
        if matched == "skip":
            return False
        if any(token in answer for token in ("no", "minimal", "skip", "simpler", "don't", "do not")):
            return False
        return True
    return True


def apply_user_intent_plan(
    user_intent: Optional[Dict[str, Any]],
    chosen_agents: List[str],
    expected_artifacts: List[str],
    rationales: Dict[str, str],
) -> Tuple[List[str], List[str], Dict[str, str]]:
    """Adjust a planned agent list based on structured user intent."""
    if not user_intent:
        return chosen_agents, expected_artifacts, rationales

    deliverable = str(user_intent.get("deliverable_kind") or "").strip().lower()
    platform = str(user_intent.get("platform_kind") or "").strip().lower()

    non_code_deliverables = {"plan", "design", "content"}
    if deliverable in non_code_deliverables:
        for agent in ("CodeAgent", "CodeImproverAgent"):
            if agent in chosen_agents:
                chosen_agents = [a for a in chosen_agents if a != agent]
                rationales.pop(agent, None)
        expected_artifacts = [
            a
            for a in expected_artifacts
            if "scaffold" not in str(a).lower() and "source" not in str(a).lower()
        ]
        rationales["CodeAgent_skipped"] = (
            f"user chose deliverable={deliverable}; skip runnable code generation"
        )
        if deliverable == "design" and "DesignerAgent" not in chosen_agents:
            insert_at = next(
                (i for i, a in enumerate(chosen_agents) if a == "WriterAgent"),
                len(chosen_agents),
            )
            chosen_agents.insert(insert_at, "DesignerAgent")
            expected_artifacts.insert(insert_at, "brand.md")
            rationales["DesignerAgent"] = "user asked for design/branding deliverables"

    if deliverable == "runnable" and platform == "website":
        rationales.setdefault(
            "ArchitectAgent",
            "user chose a website; prefer lightweight marketing/site stack over full app backend",
        )

    return chosen_agents, expected_artifacts, rationales


def skip_force_code_for_intent(user_intent: Optional[Dict[str, Any]]) -> bool:
    """Whether _should_force_code_agent should defer to user intent."""
    if not user_intent:
        return False
    deliverable = str(user_intent.get("deliverable_kind") or "").strip().lower()
    return deliverable in {"plan", "design", "content"}


def _match_option_id(entry: Dict[str, Any], answer: str) -> Optional[str]:
    normalized = re.sub(r"\s+", " ", answer.strip().lower())
    for option in entry.get("options") or []:
        if not isinstance(option, dict):
            continue
        opt_id = str(option.get("id") or "")
        label = str(option.get("label") or "").strip().lower()
        if normalized == opt_id.lower() or normalized == label:
            return opt_id or None
        if label and label in normalized:
            return opt_id or None
    return None


def _infer_outcome_id(answer: str) -> Optional[str]:
    text = answer.lower()
    # Runnable signals must win over incidental "design"/"brand" mentions in the
    # same sentence (e.g. "Complete build working with great design and branding").
    runnable_signals = (
        "complete build",
        "fully working",
        "working app",
        "working product",
        "production ready",
        "production-ready",
        "runnable",
        "full product",
        "full stack",
        "docker compose",
        "npm run",
    )
    if any(token in text for token in runnable_signals):
        return "runnable"
    if any(
        token in text
        for token in ("run", "use", "app", "code", "working", "prototype", "build")
    ):
        return "runnable"
    if any(token in text for token in ("plan", "write-up", "writeup", "strategy", "roadmap")):
        return "plan"
    if any(token in text for token in ("design only", "branding only", "logo only")):
        return "design"
    if any(token in text for token in ("design", "brand", "logo", "palette")):
        return "design"
    if any(token in text for token in ("content", "copy", "blog", "email", "landing copy")):
        return "content"
    return None


def _infer_platform_id(answer: str) -> Optional[str]:
    text = answer.lower()
    # "Website Docker" means a containerized web app, not a static marketing site.
    if any(token in text for token in ("docker", "docker compose", "compose up")):
        return "web_app"
    if "web app" in text or "browser" in text:
        return "web_app"
    if "website" in text or "marketing site" in text or "landing" in text:
        return "website"
    if any(token in text for token in ("phone", "mobile", "iphone", "android")):
        return "phone_app"
    if any(token in text for token in ("computer", "desktop", "mac", "windows")):
        return "computer_app"
    if "web app" in text or "browser" in text:
        return "web_app"
    if "not sure" in text:
        return "not_sure"
    return None


def _infer_audience_id(answer: str) -> Optional[str]:
    text = answer.lower()
    if any(token in text for token in ("just me", "myself", "personal", "only me")):
        return "just_me"
    if any(token in text for token in ("team", "company", "internal")):
        return "my_team"
    if any(token in text for token in ("public", "internet", "customers", "everyone")):
        return "public"
    return None


def _looks_like_outcome_question(question: str) -> bool:
    q = question.lower()
    return "get at the end" in q or "deliverable" in q or "working app" in q


def _looks_like_platform_question(question: str) -> bool:
    q = question.lower()
    return "what kind of thing" in q or "marketing site" in q or "fullstack" in q


def _looks_like_audience_question(question: str) -> bool:
    q = question.lower()
    return "who is" in q or "primary user" in q or "mainly for" in q


def _looks_like_workflow_question(question: str) -> bool:
    q = question.lower()
    return "one thing" in q or "must do" in q or "workflow" in q
