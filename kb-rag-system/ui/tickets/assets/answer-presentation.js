/**
 * Decide how every recorded answer channel is presented before touching the DOM.
 *
 * A persisted `generatedAnswer` is the canonical final answer. Structured
 * response channels can enrich it, disagree with it, or repeat it. This planner
 * makes those relationships explicit so the renderer neither drops a channel
 * nor floods the reviewer with duplicate content.
 */

import { parseStructuredText } from "./structured.js";

export const GUIDED_ANSWER_FIELDS = ["opening", "key_points", "steps", "warnings"];
export const ANSWER_WRAPPER_FIELDS = ["response_to_participant", "answer", "response"];

function isPlainRecord(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function normalized(value) {
  if (typeof value !== "string") return value;
  return parseStructuredText(value) ?? value;
}

export function answersEquivalent(left, right) {
  const first = normalized(left);
  const second = normalized(right);
  if (Object.is(first, second)) return true;
  if (Array.isArray(first) || Array.isArray(second)) {
    return Array.isArray(first) && Array.isArray(second) &&
      first.length === second.length &&
      first.every((value, index) => answersEquivalent(value, second[index]));
  }
  if (!isPlainRecord(first) || !isPlainRecord(second)) return false;
  const firstKeys = Object.keys(first).sort();
  const secondKeys = Object.keys(second).sort();
  return firstKeys.length === secondKeys.length &&
    firstKeys.every((key, index) =>
      key === secondKeys[index] && answersEquivalent(first[key], second[key])
    );
}

function guidedProjection(response) {
  const entries = GUIDED_ANSWER_FIELDS
    .filter((key) => Object.hasOwn(response, key))
    .map((key) => [key, response[key]]);
  return entries.length === 0 ? null : Object.fromEntries(entries);
}

function wrapperChannels(response) {
  return ANSWER_WRAPPER_FIELDS
    .filter((key) => Object.hasOwn(response, key))
    .map((key) => ({
      source: `structured_response.${key}`,
      label: key,
      path: `$/structured_response/${key}`,
      value: response[key],
      recorded: true,
    }));
}

function firstUsableWrapper(channels) {
  return channels.find((channel) => channel.value !== null && channel.value !== undefined) ??
    channels[0] ??
    null;
}

function reference(source, path, sameAs, relation) {
  return { source, path, sameAs, relation };
}

/** Return a JSON-serializable, lossless presentation plan for one execution. */
export function planAnswerPresentation(execution = {}) {
  const response = isPlainRecord(execution.structuredResponse)
    ? execution.structuredResponse
    : Object.create(null);
  const guided = guidedProjection(response);
  const wrappers = wrapperChannels(response);
  const generatedRecorded = execution.generatedAnswer !== null &&
    execution.generatedAnswer !== undefined;

  let primary;
  if (generatedRecorded) {
    primary = {
      source: "generated_answer",
      label: "Generated answer",
      path: "$/generated_answer",
      value: execution.generatedAnswer,
      recorded: true,
    };
  } else if (guided !== null) {
    primary = {
      source: "structured_response.guided",
      label: "Guided structured response",
      path: "$/structured_response",
      value: guided,
      recorded: true,
    };
  } else {
    const fallback = firstUsableWrapper(wrappers);
    primary = fallback ?? {
      source: "generated_answer",
      label: "Generated answer",
      path: "$/generated_answer",
      value: undefined,
      recorded: false,
    };
  }

  const presented = [primary];
  const secondaries = [];
  const references = [];

  function consider(channel) {
    if (presented.some((item) => item.path === channel.path)) return;
    const match = presented.find((item) => answersEquivalent(item.value, channel.value));
    if (match !== undefined) {
      references.push(reference(
        channel.source,
        channel.path,
        match.path,
        `Same content as ${match.label}`
      ));
      return;
    }
    secondaries.push(channel);
    presented.push(channel);
    references.push(reference(
      channel.source,
      channel.path,
      channel.path,
      "Shown separately because its recorded content differs"
    ));
  }

  if (guided !== null) {
    consider({
      source: "structured_response.guided",
      label: "Guided structured response",
      path: "$/structured_response",
      value: guided,
      recorded: true,
    });
  }
  for (const channel of wrappers) consider(channel);

  const responseKeys = Object.keys(response);
  const wholeResponseMatches = responseKeys.length > 0 &&
    answersEquivalent(primary.value, response);
  let residual = null;
  if (wholeResponseMatches && primary.path !== "$/structured_response") {
    references.push(reference(
      "structured_response",
      "$/structured_response",
      primary.path,
      `Same content as ${primary.label}`
    ));
  } else if (!wholeResponseMatches) {
    const hiddenKeys = new Set([
      ...GUIDED_ANSWER_FIELDS,
      ...ANSWER_WRAPPER_FIELDS,
      "outcome_reason",
    ]);
    const residualEntries = Object.entries(response)
      .filter(([key]) => !hiddenKeys.has(key));
    if (residualEntries.length > 0) {
      residual = {
        label: "Additional response record",
        path: "$/structured_response",
        value: Object.fromEntries(residualEntries),
      };
    }
  }

  const executionOutcomeRecorded = execution.outcomeReason !== null &&
    execution.outcomeReason !== undefined;
  const structuredOutcomeRecorded = Object.hasOwn(response, "outcome_reason");
  const primaryOutcome = executionOutcomeRecorded
    ? {
        source: "outcome_reason",
        path: "$/outcome_reason",
        value: execution.outcomeReason,
        recorded: true,
      }
    : structuredOutcomeRecorded
      ? {
          source: "structured_response.outcome_reason",
          path: "$/structured_response/outcome_reason",
          value: response.outcome_reason,
          recorded: true,
        }
      : null;
  const secondaryOutcome = executionOutcomeRecorded && structuredOutcomeRecorded &&
    !answersEquivalent(execution.outcomeReason, response.outcome_reason)
    ? {
        source: "structured_response.outcome_reason",
        path: "$/structured_response/outcome_reason",
        value: response.outcome_reason,
        recorded: true,
      }
    : null;

  return {
    primary,
    secondaries,
    references,
    residual,
    outcome: { primary: primaryOutcome, secondary: secondaryOutcome },
  };
}
