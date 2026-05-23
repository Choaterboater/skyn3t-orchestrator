import { describe, expect, it } from "vitest";

import {
  buildClarificationAnswers,
  clarificationSubmitReady,
  missingClarificationQuestions,
} from "./clarificationForm";

const specs = [
  {
    id: "outcome",
    question: "What should you get at the end?",
    options: [{ id: "runnable", label: "Something I can run or use" }],
  },
  {
    id: "must_do",
    question: "What's the one thing it must do well first?",
    options: [],
    free_text: true,
  },
];

describe("clarificationForm", () => {
  it("allows submit when chip questions are answered and must_do is empty", () => {
    const questions = specs.map((spec) => spec.question);
    const drafts = ["Something I can run or use", ""];

    expect(clarificationSubmitReady(questions, drafts, specs)).toBe(true);
    expect(missingClarificationQuestions(questions, drafts, specs)).toEqual([]);
  });

  it("fills must_do from the brief when left blank", () => {
    const questions = specs.map((spec) => spec.question);
    const built = buildClarificationAnswers(
      questions,
      ["Something I can run or use", ""],
      specs,
      "Track daily habits",
    );

    expect(built[0]).toBe("Something I can run or use");
    expect(built[1]).toBe("Track daily habits");
  });
});
