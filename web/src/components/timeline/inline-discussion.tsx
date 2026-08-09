/** Inline review discussion rendered under an assistant turn.
 *
 *  Phase C of the review-as-annotation refactor. The reviewer
 *  conversation lives visually attached to the Claude message it
 *  discusses, not in a separate panel. The side panel still exists as
 *  the advanced surface (skills, evidence toggles, history) — opened
 *  via the toolbar Review button or the "Open in full panel" link
 *  inside this component.
 *
 *  Three render states:
 *    1. Hidden — no anchored messages AND not expanded → returns null.
 *    2. Collapsed chip — anchored messages exist but not yet expanded.
 *       Single button: "▸ N reviewer notes".
 *    3. Expanded — message list + LatestPromptBox + composer +
 *       "Open in full panel" link.
 *
 *  Composer state is local React state. It survives collapse/expand
 *  because Turn keys on the turn uuid (stable) so this component does
 *  not unmount across timeline re-renders.
 *
 *  Send flow uses the canonical Quick Review skill with Claude-turn
 *  evidence only (no git/test/build). Anything more advanced should be
 *  done via the side panel — keeping the inline surface minimal is
 *  intentional. */
import * as React from "react";
import {
  ChevronDown,
  ChevronRight,
  ExternalLink,
  Loader2,
  MessageCircle,
  Send,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { useApp } from "@/store/app";
import { api } from "@/lib/api";
import {
  EMPTY_GROUPED_REVIEW_MESSAGES,
  findLatestPromptForTurn,
  getMessagesForTurn,
} from "@/lib/review-grouping";
import {
  ensureActiveThread,
  refreshGroupedReviewMessages,
} from "@/lib/review-loader";
import { LatestPromptBox } from "@/components/review-panel/parts/prompt-box";
import {
  ReviewerMessageView,
  UserMessageView,
} from "@/components/review-panel/parts/reviewer-message-view";
import { toast } from "sonner";

/** Quick-Review-only evidence: send the Claude turn the user is
 *  reviewing, but no git diff / test output / build output. The full
 *  panel is still the place to do an "evidence-rich" review. */
const QUICK_INLINE_EVIDENCE = {
  include_claude_turn: true,
  include_git_status: false,
  include_changed_files: false,
  include_git_diff: false,
  include_test_output: false,
  include_build_output: false,
} as const;

export function InlineDiscussion({
  turnUuid,
  turnRole,
  turnText,
  expanded,
  onSetExpanded,
}: {
  turnUuid: string;
  turnRole: string;
  turnText: string;
  /** Hoisted to TurnBody so the AssistantActionRow's "Discuss this
   *  result" button can toggle it. Local state would be cleaner but
   *  the click site is a sibling component, not a child. */
  expanded: boolean;
  onSetExpanded: (open: boolean) => void;
}) {
  const selectedSessionId = useApp((s) => s.selectedSessionId);
  const selectedBucket = useApp((s) => s.selectedBucket);
  const grouped =
    useApp((s) =>
      selectedSessionId
        ? s.reviewMessagesBySession[selectedSessionId]
        : undefined,
    ) ?? EMPTY_GROUPED_REVIEW_MESSAGES;
  const setReviewMessagesForSession = useApp(
    (s) => s.setReviewMessagesForSession,
  );
  const openReviewPanel = useApp((s) => s.openReviewPanel);

  const messages = getMessagesForTurn(grouped, turnUuid);
  const latestPrompt = findLatestPromptForTurn(grouped, turnUuid);
  const count = messages.length;

  // Composer text is preserved across collapse/expand because this
  // component stays mounted under the parent Turn (which keys on the
  // turn uuid).
  const [composer, setComposer] = React.useState("");
  const [sending, setSending] = React.useState(false);

  // ---- Hidden state ------------------------------------------------------
  if (!expanded && count === 0) return null;

  // ---- Collapsed chip ----------------------------------------------------
  if (!expanded) {
    return (
      <button
        type="button"
        onClick={() => onSetExpanded(true)}
        className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-border/60 bg-muted/30 px-2 py-1 text-[11.5px] text-muted-foreground transition-colors hover:border-primary/40 hover:bg-primary/5 hover:text-foreground"
        title="Open inline review discussion"
      >
        <ChevronRight className="h-3 w-3" />
        <MessageCircle className="h-3 w-3" />
        {count === 1 ? "1 reviewer note" : `${count} reviewer notes`}
      </button>
    );
  }

  // ---- Expanded ---------------------------------------------------------

  const onCopyPrompt = async (prompt: string) => {
    if (!prompt || !prompt.trim()) {
      toast.error("Nothing to copy yet");
      return;
    }
    try {
      await navigator.clipboard.writeText(prompt.trim());
      toast.success("Copied next prompt");
    } catch {
      toast.error("Clipboard write failed");
    }
  };

  const onOpenInPanel = () => {
    openReviewPanel({
      sourceTurnUuid: turnUuid,
      sourceTurnRole: turnRole,
      sourceTurnText: turnText,
    });
  };

  const onSend = async () => {
    if (!selectedBucket || !selectedSessionId) return;
    if (!composer.trim() || sending) return;
    setSending(true);
    try {
      const threadId = await ensureActiveThread(
        selectedBucket,
        selectedSessionId,
        defaultThreadName(turnText),
      );
      await api.reviewsSend({
        thread_id: threadId,
        question: composer.trim(),
        // Inline Discuss defaults to the Next Prompt Coach skill —
        // strategy partner, not auditor. The full panel exposes
        // Quick / Critical / Coach for users who want the audit
        // flavor instead.
        skill_id: "next_prompt_coach",
        claude_session_id: selectedSessionId,
        claude_turn_uuid: turnUuid,
        claude_turn_role: turnRole,
        claude_turn_text: turnText,
        evidence: { ...QUICK_INLINE_EVIDENCE },
      });
      // Refresh the global map so this exchange (the user message
      // persisted server-side AND the reviewer reply) becomes visible
      // inline immediately.
      await refreshGroupedReviewMessages(
        selectedBucket,
        selectedSessionId,
        setReviewMessagesForSession,
      );
      setComposer("");
    } catch (e: unknown) {
      const msg = String((e as { message?: string })?.message ?? "Send failed");
      const status = (e as { status?: number })?.status;
      if (status === 422) {
        toast.error(
          "Couldn't validate the request. Try rephrasing or simplifying.",
        );
      } else if (msg.includes("SECRET_DETECTED") || status === 409) {
        toast.error(
          "Secret-like value detected. Edit the message or use the full panel for override.",
        );
      } else {
        toast.error(msg);
      }
    } finally {
      setSending(false);
    }
  };

  return (
    <section className="mt-2 rounded-md border border-border/60 bg-muted/15 p-3">
      <header className="mb-2 flex items-center gap-2 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
        <button
          type="button"
          onClick={() => onSetExpanded(false)}
          className="flex items-center gap-1.5 transition-colors hover:text-foreground"
          title="Collapse discussion"
        >
          <ChevronDown className="h-3 w-3" />
          <MessageCircle className="h-3 w-3" />
          Discussion
          {count > 0 && (
            <span className="font-mono text-[10px] normal-case opacity-70">
              ({count})
            </span>
          )}
        </button>
        <button
          type="button"
          onClick={onOpenInPanel}
          className="ms-auto inline-flex items-center gap-1 text-[10.5px] font-normal normal-case text-muted-foreground/80 transition-colors hover:text-foreground"
          title="Open this turn in the full Review panel (skills, evidence, history)"
        >
          <ExternalLink className="h-3 w-3" />
          Open in full panel
        </button>
      </header>

      {count > 0 && (
        <div className="mb-2 flex flex-col gap-2">
          {messages.map((m) =>
            m.role === "reviewer" ? (
              <ReviewerMessageView
                key={m.id}
                msg={m}
                onCopyPrompt={onCopyPrompt}
                hidePromptBox
              />
            ) : (
              <UserMessageView key={m.id} msg={m} />
            ),
          )}
        </div>
      )}

      {latestPrompt && (
        <div className="mb-2">
          <LatestPromptBox
            prompt={latestPrompt.prompt}
            modeLabel={
              latestPrompt.renderMode === "prompt_coach"
                ? "Improved prompt"
                : "Prompt to send Claude Code"
            }
            createdAt={latestPrompt.msg.created_at}
            onCopy={() => onCopyPrompt(latestPrompt.prompt)}
            className="rounded-lg border-2 border-primary/50 bg-background shadow-sm"
          />
        </div>
      )}

      <InlineComposer
        value={composer}
        onChange={setComposer}
        onSend={onSend}
        sending={sending}
        disabled={!selectedBucket || !selectedSessionId}
      />
    </section>
  );
}

function InlineComposer({
  value,
  onChange,
  onSend,
  sending,
  disabled,
}: {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  sending: boolean;
  disabled: boolean;
}) {
  const canSend = !sending && !disabled && value.trim().length > 0;
  return (
    <div className="flex flex-col gap-2">
      <textarea
        value={value}
        placeholder="Discuss this with the prompt coach: intention, direction, next prompt…"
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
            e.preventDefault();
            if (canSend) onSend();
          }
        }}
        className="block w-full resize-y rounded-md border border-border bg-background px-2.5 py-1.5 text-[12.5px] outline-none placeholder:text-muted-foreground/60 focus:border-primary/60 min-h-[60px]"
      />
      <div className="flex items-center gap-2">
        <span className="text-[10.5px] text-muted-foreground/80">
          Next Prompt Coach · ⌘ + Enter
        </span>
        <Button
          onClick={onSend}
          size="sm"
          className="ms-auto h-7"
          disabled={!canSend}
          title={
            canSend
              ? "Send to reviewer (Cmd-Enter)"
              : disabled
              ? "Open a session first"
              : "Type guidance to send"
          }
        >
          {sending ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <Send className="h-3 w-3" />
          )}
          {sending ? "Sending…" : "Send"}
        </Button>
      </div>
    </div>
  );
}

/** Default thread name when an inline send creates a fresh thread.
 *  Mirrors the side panel's naming so users see consistent labels in
 *  the History popover regardless of which surface created the
 *  thread. */
function defaultThreadName(turnText: string): string {
  const text = turnText.trim().replace(/\s+/g, " ").slice(0, 60);
  if (!text) return "Review (inline)";
  return `Review: ${text}${text.length === 60 ? "…" : ""}`;
}
