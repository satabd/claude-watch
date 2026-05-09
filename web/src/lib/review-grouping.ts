/** Pure helpers for grouping review messages by the Claude turn they
 *  anchor to (``source_turn_uuid``). Used by the per-session loader and,
 *  in Phase C, by the inline-discussion render path that hangs reviewer
 *  messages under their source Claude turn in the timeline.
 *
 *  No React, no I/O — just data transforms over ``ReviewMessage[]`` so
 *  they're trivial to unit-test. Inputs may come from multiple threads
 *  (the loader fetches messages for every active thread of the current
 *  Claude session and concatenates them); the helpers accept that flat
 *  list and merge by turn uuid.
 *
 *  Messages with ``source_turn_uuid === null`` end up in the ``noAnchor``
 *  bucket — they were sent from a surface that didn't anchor to a
 *  specific Claude turn (e.g. the toolbar entry on a session with no
 *  assistant turn yet). The inline UI must NOT render them under any
 *  specific turn; they remain visible only via the side panel. */
import type { ReviewMessage } from "./api";
import { parseCoachReview, parseCriticalReview } from "./review-parser";
import {
  reviewerModeFromMessage,
  renderModeForSkill,
  type ReviewerRenderMode,
} from "@/components/review-panel/parts/message-helpers";

/** A flat ``ReviewMessage[]`` regrouped by anchor.
 *
 *  - ``byTurn``    — turn uuid → chronologically-ordered messages.
 *                    A turn with no anchored messages has no entry
 *                    (so ``has(uuid)`` answers "any discussion?").
 *  - ``noAnchor``  — messages whose ``source_turn_uuid`` is null.
 *  - ``count``     — ``byTurn.size`` for the common "are there any
 *                    inline anchors?" check.
 */
export interface GroupedReviewMessages {
  byTurn: ReadonlyMap<string, readonly ReviewMessage[]>;
  noAnchor: readonly ReviewMessage[];
  /** Convenience — number of *turns* with at least one anchored
   *  message (NOT total message count). */
  count: number;
}

/** Stable empty result — same identity across renders so consumers
 *  using object equality won't see false changes when a session has no
 *  reviews yet. */
export const EMPTY_GROUPED_REVIEW_MESSAGES: GroupedReviewMessages = {
  byTurn: new Map(),
  noAnchor: [],
  count: 0,
};

/** Group a flat message list by ``source_turn_uuid``. Each per-turn list
 *  is sorted ascending by ``created_at`` so the chat layer can render
 *  them in conversational order without re-sorting. Messages from
 *  different threads with the same anchor are merged into one list. */
export function groupReviewMessagesByTurn(
  messages: readonly ReviewMessage[],
): GroupedReviewMessages {
  if (!messages || messages.length === 0) {
    return EMPTY_GROUPED_REVIEW_MESSAGES;
  }
  const byTurn = new Map<string, ReviewMessage[]>();
  const noAnchor: ReviewMessage[] = [];
  for (const m of messages) {
    if (m.source_turn_uuid) {
      const existing = byTurn.get(m.source_turn_uuid);
      if (existing) existing.push(m);
      else byTurn.set(m.source_turn_uuid, [m]);
    } else {
      noAnchor.push(m);
    }
  }
  // Chronological order across merged threads. Stable on tie.
  for (const list of byTurn.values()) {
    list.sort((a, b) => a.created_at - b.created_at);
  }
  noAnchor.sort((a, b) => a.created_at - b.created_at);
  return {
    byTurn,
    noAnchor,
    count: byTurn.size,
  };
}

/** Read the (chronologically-ordered) messages anchored to a specific
 *  turn. Returns an empty array when no messages anchor there. Returns
 *  the same array reference across calls when the underlying
 *  ``GroupedReviewMessages`` is the same — safe to use as a React
 *  effect dependency. */
export function getMessagesForTurn(
  grouped: GroupedReviewMessages,
  turnUuid: string | null | undefined,
): readonly ReviewMessage[] {
  if (!turnUuid) return EMPTY_LIST;
  return grouped.byTurn.get(turnUuid) ?? EMPTY_LIST;
}

const EMPTY_LIST: readonly ReviewMessage[] = Object.freeze([]);

/** Information about the most recent reviewer-produced prompt for a
 *  given turn. The inline UI uses this to render the prominent prompt
 *  card under the Claude turn whose discussion produced it. */
export interface LatestPromptInfo {
  /** The reviewer message that contains the prompt (so the caller can
   *  show its created_at on the prompt card). */
  msg: ReviewMessage;
  /** The cleaned prompt body, fence-stripped by the parser. */
  prompt: string;
  /** Render mode that produced the prompt — lets the caller pick a
   *  label like "Prompt to send Claude" vs "Improved prompt". */
  renderMode: ReviewerRenderMode;
}

/** Walk a turn's messages newest-first and return the first reviewer
 *  message with a parseable, non-empty prompt section. Returns ``null``
 *  when no such message exists. Per spec the inline UI must never
 *  fabricate a prompt from raw text — if the parser didn't find one,
 *  the caller renders a "no prompt yet" hint. */
export function findLatestPromptForTurn(
  grouped: GroupedReviewMessages,
  turnUuid: string | null | undefined,
): LatestPromptInfo | null {
  const list = getMessagesForTurn(grouped, turnUuid);
  if (list.length === 0) return null;
  for (let i = list.length - 1; i >= 0; i--) {
    const m = list[i];
    if (m.role !== "reviewer") continue;
    const skill = reviewerModeFromMessage(m);
    const renderMode = renderModeForSkill(skill);
    if (renderMode === "critical_or_quick") {
      const p = parseCriticalReview(m.content);
      if (p.parsed && p.nextPrompt) {
        return { msg: m, prompt: p.nextPrompt, renderMode };
      }
    } else {
      const p = parseCoachReview(m.content);
      if (p.parsed && p.improvedPrompt) {
        return { msg: m, prompt: p.improvedPrompt, renderMode };
      }
    }
  }
  return null;
}
