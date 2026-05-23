export type ClarificationOption = {
  id?: string;
  question?: string;
  options?: Array<{ id?: string; label?: string }>;
  placeholder?: string;
  free_text?: boolean;
};

export function clarificationOptionEntry(
  question: string,
  index: number,
  questionOptions: ClarificationOption[],
): ClarificationOption | undefined {
  const normalized = String(question || "").trim();
  return (
    questionOptions.find(
      (entry) => String(entry?.question || "").trim() === normalized,
    ) ?? questionOptions[index]
  );
}

/** Chip-style questions must be answered; free-text-only must_do is optional. */
export function isClarificationQuestionRequired(
  optionEntry: ClarificationOption | undefined,
): boolean {
  const options = Array.isArray(optionEntry?.options) ? optionEntry.options : [];
  if (options.length > 0) {
    return true;
  }
  const specId = String(optionEntry?.id || "").trim().toLowerCase();
  return specId !== "must_do";
}

export function buildClarificationAnswers(
  questions: string[],
  draftAnswers: string[],
  questionOptions: ClarificationOption[],
  briefFallback = "",
): string[] {
  const brief = String(briefFallback || "").trim();
  return questions.map((question, index) => {
    const trimmed = String(draftAnswers[index] ?? "").trim();
    if (trimmed) {
      return trimmed;
    }
    const entry = clarificationOptionEntry(question, index, questionOptions);
    if (isClarificationQuestionRequired(entry)) {
      return "";
    }
    if (brief) {
      return brief.slice(0, 500);
    }
    return "Use the original brief and prior answers.";
  });
}

export function clarificationSubmitReady(
  questions: string[],
  draftAnswers: string[],
  questionOptions: ClarificationOption[],
): boolean {
  if (questions.length === 0) {
    return false;
  }
  return questions.every((question, index) => {
    const entry = clarificationOptionEntry(question, index, questionOptions);
    if (!isClarificationQuestionRequired(entry)) {
      return true;
    }
    return String(draftAnswers[index] ?? "").trim().length > 0;
  });
}

export function missingClarificationQuestions(
  questions: string[],
  draftAnswers: string[],
  questionOptions: ClarificationOption[],
): string[] {
  return questions.filter((question, index) => {
    const entry = clarificationOptionEntry(question, index, questionOptions);
    if (!isClarificationQuestionRequired(entry)) {
      return false;
    }
    return !String(draftAnswers[index] ?? "").trim();
  });
}
