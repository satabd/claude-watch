/** Per-session review-message loader.
 *
 *  Three responsibilities, split into a pure helper + two thin I/O
 *  wrappers so the pure part stays test-friendly without module-level
 *  fetch mocking:
 *
 *    1. ``combineThreadMessages`` — pure: filter threads by Claude
 *       session + archive flag, gather their messages, group by
 *       ``source_turn_uuid``. Used both at session-load time and
 *       after every ``/api/reviews/send`` to refresh the global map.
 *
 *    2. ``loadGroupedReviewMessages`` — I/O wrapper around
 *       ``combineThreadMessages``. Hits ``api.reviewsList`` then
 *       ``api.reviewsListMessages`` once per matching thread.
 *
 *    3. ``ensureActiveThread`` — find-or-create the active review
 *       thread for the inline send path. Returns the thread id. The
 *       side panel has its own auto-prepare logic with extra UI
 *       state; this is the slim variant for inline. */
import { api } from "./api";
import type { ReviewMessage, ReviewThread } from "./api";
import {
  EMPTY_GROUPED_REVIEW_MESSAGES,
  groupReviewMessagesByTurn,
  type GroupedReviewMessages,
} from "./review-grouping";

/** Pure combiner: given a list of threads, the current Claude session
 *  id, and a per-thread message map, produce the grouped result. Filters
 *  to threads with ``claude_session_id === claudeSessionId`` and
 *  ``archived_at === null``. Threads with no entry in
 *  ``messagesByThread`` (e.g. when the loader hadn't fetched them yet)
 *  are treated as having no messages. */
export function combineThreadMessages(
  threads: readonly ReviewThread[],
  claudeSessionId: string,
  messagesByThread: Record<number, readonly ReviewMessage[]>,
): GroupedReviewMessages {
  const matching = threads.filter(
    (t) => t.claude_session_id === claudeSessionId && !t.archived_at,
  );
  if (matching.length === 0) return EMPTY_GROUPED_REVIEW_MESSAGES;
  const flat: ReviewMessage[] = [];
  for (const t of matching) {
    const list = messagesByThread[t.id];
    if (list && list.length > 0) flat.push(...list);
  }
  if (flat.length === 0) return EMPTY_GROUPED_REVIEW_MESSAGES;
  return groupReviewMessagesByTurn(flat);
}

/** I/O wrapper. Returns the stable empty grouping when no threads match
 *  the current Claude session — saves a per-thread fetch round-trip and
 *  keeps the React reference stable for empty sessions. */
export async function loadGroupedReviewMessages(
  bucket: string,
  claudeSessionId: string,
): Promise<GroupedReviewMessages> {
  const threads = await api.reviewsList(bucket);
  const matching = threads.filter(
    (t) => t.claude_session_id === claudeSessionId && !t.archived_at,
  );
  if (matching.length === 0) return EMPTY_GROUPED_REVIEW_MESSAGES;
  const lists = await Promise.all(
    matching.map((t) => api.reviewsListMessages(t.id)),
  );
  const messagesByThread: Record<number, ReviewMessage[]> = {};
  matching.forEach((t, i) => {
    messagesByThread[t.id] = lists[i];
  });
  return combineThreadMessages(threads, claudeSessionId, messagesByThread);
}

/** Find-or-create helper for the inline send path. Picks the most
 *  recently-updated active thread for ``(bucket, claudeSessionId)``;
 *  creates a new thread with ``defaultName`` if none exists. The side
 *  panel keeps its own richer ``ensureThread`` because it also drives
 *  the panel's setupState UI. */
export async function ensureActiveThread(
  bucket: string,
  claudeSessionId: string,
  defaultName: string,
): Promise<number> {
  const threads = await api.reviewsList(bucket);
  // ``reviewsList`` returns most-recently-updated first per the backend
  // route's ORDER BY updated_at DESC, so ``find`` picks the freshest
  // active match without an extra sort.
  const match = threads.find(
    (t) => t.claude_session_id === claudeSessionId && !t.archived_at,
  );
  if (match) return match.id;
  const created = await api.reviewsCreateThread({
    name: defaultName,
    project_bucket: bucket,
    claude_session_id: claudeSessionId,
    provider: "codex",
  });
  return created.id;
}

/** After-send refresh: fetch the latest grouped state and store it.
 *  Failures are non-fatal — the next session-load reconciles. Both the
 *  inline send path and the side panel's send-success handler call this
 *  to keep ``reviewMessagesBySession`` fresh for inline rendering. */
export async function refreshGroupedReviewMessages(
  bucket: string,
  claudeSessionId: string,
  setForSession: (sessionId: string, grouped: GroupedReviewMessages) => void,
): Promise<void> {
  try {
    const grouped = await loadGroupedReviewMessages(bucket, claudeSessionId);
    setForSession(claudeSessionId, grouped);
  } catch (e) {
    // Non-fatal: the send IS persisted on the server; the user has the
    // immediate reviewer reply via local state. The map will catch up
    // on the next session-load.
    console.warn("review messages refresh failed", e);
  }
}
