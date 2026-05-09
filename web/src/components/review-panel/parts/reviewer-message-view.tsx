/** Message rendering for review chats. Extracted from review-panel.tsx so
 *  the inline-discussion path (Phase C) can render the same message
 *  shapes under each Claude turn that the side panel renders inside its
 *  scroll today.
 *
 *  Three layers:
 *    UserMessageView     — the user's typed guidance, rendered compact.
 *    ReviewerMessageView — the reviewer's reply. Routes to one of:
 *        CriticalReviewView  (Quick or Critical skill)
 *        CoachReviewView     (Prompt Coach skill)
 *        RawReviewerView     (parser failed; raw fallback)
 *    Section / CollapsibleDetails / ShowFullReviewToggle — small leaves
 *    used by the view variants. They live here because they're tightly
 *    coupled to those views (style, expected content, etc.).
 *
 *  The ``hidePromptBox`` prop on the reviewer view tells the variant to
 *  omit its in-message prompt card. The side panel sets it true because
 *  it hoists the prompt into a prominent LatestPromptBox below the
 *  scroll; the inline-discussion surface (Phase C) will likewise hoist
 *  the prompt under the Claude turn and pass true. Behavior is
 *  unchanged from the in-line versions that lived in review-panel.tsx.
 */
import * as React from "react";
import { ChevronDown, ChevronRight, MessageSquare, ShieldCheck } from "lucide-react";
import { copyTargetForReply } from "@/lib/extract-next-prompt";
import {
  parseCoachReview,
  parseCriticalReview,
  type CoachReview,
  type CriticalReview,
} from "@/lib/review-parser";
import type { ReviewMessage } from "@/lib/api";
import { formatRelative } from "@/lib/utils";
import {
  reviewerModeFromMessage,
  renderModeForSkill,
} from "./message-helpers";
import { NextPromptBox, NoPromptHint } from "./prompt-box";

// ---------------------------------------------------------------------------
// User message
// ---------------------------------------------------------------------------

/** The user's question — kept compact, secondary to the reviewer reply. */
export function UserMessageView({ msg }: { msg: ReviewMessage }) {
  return (
    <article className="rounded-md border border-border bg-background/60 p-3 text-[13px] leading-relaxed">
      <header className="mb-1.5 flex items-center gap-2 text-[10.5px] uppercase tracking-wider text-muted-foreground">
        <MessageSquare className="h-3 w-3" />
        <span>you</span>
        <span className="ms-auto">{formatRelative(msg.created_at)}</span>
        {msg.estimated_tokens != null && (
          <span className="font-mono text-[10px]">
            ~{msg.estimated_tokens} tk
          </span>
        )}
      </header>
      <pre className="whitespace-pre-wrap font-sans text-[12.5px]">
        {msg.content}
      </pre>
    </article>
  );
}

// ---------------------------------------------------------------------------
// Reviewer message — top-level dispatcher
// ---------------------------------------------------------------------------

export function ReviewerMessageView({
  msg,
  onCopyPrompt,
  hidePromptBox,
}: {
  msg: ReviewMessage;
  onCopyPrompt: (prompt: string) => void;
  /** When true, the in-message prompt box is omitted. Caller is
   *  responsible for rendering a hoisted prompt card elsewhere. */
  hidePromptBox?: boolean;
}) {
  const skill = reviewerModeFromMessage(msg);
  const renderMode = renderModeForSkill(skill);
  const parsed = React.useMemo(
    () =>
      renderMode === "critical_or_quick"
        ? parseCriticalReview(msg.content)
        : parseCoachReview(msg.content),
    [msg.content, renderMode],
  );

  // Short label shown in the message header — Quick / Critical / Coach.
  const skillLabel =
    skill === "quick_review"
      ? "quick"
      : skill === "prompt_coach"
      ? "coach"
      : "critical";

  return (
    <article className="rounded-lg border border-primary/30 bg-primary/5 p-4">
      <header className="mb-3 flex items-center gap-2 text-[10.5px] uppercase tracking-wider text-muted-foreground">
        <ShieldCheck className="h-3 w-3 text-primary" />
        <span>reviewer · {skillLabel}</span>
        {msg.model && (
          <span className="font-mono text-[10px]">{msg.model}</span>
        )}
        <span className="ms-auto">{formatRelative(msg.created_at)}</span>
        {msg.provider_tokens != null && (
          <span className="font-mono text-[10px]">
            {msg.provider_tokens} tk
          </span>
        )}
      </header>

      {parsed.parsed && renderMode === "critical_or_quick" ? (
        <CriticalReviewView
          parsed={parsed as CriticalReview}
          fullContent={msg.content}
          onCopyPrompt={onCopyPrompt}
          hidePromptBox={hidePromptBox}
        />
      ) : parsed.parsed && renderMode === "prompt_coach" ? (
        <CoachReviewView
          parsed={parsed as CoachReview}
          fullContent={msg.content}
          onCopyPrompt={onCopyPrompt}
          hidePromptBox={hidePromptBox}
        />
      ) : (
        <RawReviewerView msg={msg} onCopyPrompt={onCopyPrompt} />
      )}
    </article>
  );
}

// ---------------------------------------------------------------------------
// Critical / Quick reviewer compact view
// ---------------------------------------------------------------------------

function CriticalReviewView({
  parsed,
  fullContent,
  onCopyPrompt,
  hidePromptBox,
}: {
  parsed: CriticalReview;
  fullContent: string;
  onCopyPrompt: (prompt: string) => void;
  /** When true, the prompt is rendered separately below the message
   *  list (LatestPromptBox) so we omit it here to avoid duplication. */
  hidePromptBox?: boolean;
}) {
  return (
    <div className="space-y-3 text-[13px] leading-relaxed">
      {parsed.understanding && (
        // Small italic preamble. Only the Next Prompt Coach skill emits
        // an UNDERSTANDING section; older skills leave this null and
        // the line is skipped. Distinct from the verdict so the
        // reader sees "what the user is trying to do" before the
        // reviewer's take on it.
        <p className="text-[12.5px] italic leading-relaxed text-muted-foreground">
          {parsed.understanding}
        </p>
      )}

      {parsed.verdict && (
        <p className="text-[14px] font-medium text-foreground">
          {parsed.verdict}
        </p>
      )}

      {parsed.why.length > 0 && (
        <ul className="space-y-1 text-foreground/90">
          {parsed.why.slice(0, 3).map((f, i) => (
            <li key={i} className="flex gap-2">
              <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-foreground/60" />
              <span>{f}</span>
            </li>
          ))}
        </ul>
      )}

      {parsed.nextAction && (
        <p className="text-foreground/90">
          <span className="font-semibold text-foreground">Next: </span>
          {parsed.nextAction}
        </p>
      )}

      {!hidePromptBox &&
        (parsed.nextPrompt ? (
          <NextPromptBox
            label="Prompt to send Claude"
            prompt={parsed.nextPrompt}
            onCopy={() => onCopyPrompt(parsed.nextPrompt!)}
          />
        ) : (
          <NoPromptHint />
        ))}

      {parsed.details && <CollapsibleDetails text={parsed.details} />}
      <ShowFullReviewToggle content={fullContent} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Prompt Coach compact view
// ---------------------------------------------------------------------------

function CoachReviewView({
  parsed,
  fullContent,
  onCopyPrompt,
  hidePromptBox,
}: {
  parsed: CoachReview;
  fullContent: string;
  onCopyPrompt: (prompt: string) => void;
  hidePromptBox?: boolean;
}) {
  return (
    <div className="space-y-3 text-[13px] leading-relaxed">
      {parsed.clarifiedIntent && (
        <Section title="Clarified intent">
          <p className="text-foreground/90">{parsed.clarifiedIntent}</p>
        </Section>
      )}

      {!hidePromptBox &&
        (parsed.improvedPrompt ? (
          <NextPromptBox
            label="Improved prompt"
            prompt={parsed.improvedPrompt}
            onCopy={() => onCopyPrompt(parsed.improvedPrompt!)}
          />
        ) : (
          <NoPromptHint />
        ))}

      {parsed.whyThisIsBetter.length > 0 && (
        <Section title="Why this is better">
          <ul className="space-y-1">
            {parsed.whyThisIsBetter.slice(0, 3).map((r, i) => (
              <li key={i} className="flex gap-2">
                <span className="mt-[6px] h-1 w-1 shrink-0 rounded-full bg-foreground/60" />
                <span>{r}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {parsed.details && <CollapsibleDetails text={parsed.details} />}
      <ShowFullReviewToggle content={fullContent} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Raw fallback when parsing fails
// ---------------------------------------------------------------------------

function RawReviewerView({
  msg,
  onCopyPrompt,
}: {
  msg: ReviewMessage;
  onCopyPrompt: (prompt: string) => void;
}) {
  // Even when the parser bailed, the model may still have written one of
  // the recognized prompt headings. The strict extractor returns null
  // when no heading is present — we never copy a "best guess" of the
  // whole reply.
  const extracted = copyTargetForReply(msg.content);
  return (
    <div className="space-y-3 text-[13px] leading-relaxed">
      <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-[11px] text-amber-700 dark:text-amber-300">
        Couldn't parse a structured response — showing the raw reply below.
      </div>
      <pre className="whitespace-pre-wrap font-sans text-[12.5px]">
        {msg.content}
      </pre>
      {extracted ? (
        <NextPromptBox
          label="Prompt to send Claude"
          prompt={extracted}
          onCopy={() => onCopyPrompt(extracted)}
        />
      ) : (
        <NoPromptHint />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components shared between Critical / Coach views
// ---------------------------------------------------------------------------

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <h3 className="mb-1 text-[10.5px] font-semibold uppercase tracking-wider text-muted-foreground">
        {title}
      </h3>
      <div>{children}</div>
    </section>
  );
}

/** Inline collapsible "Details" section the model writes only when the
 *  user explicitly asked for deeper analysis. Default-collapsed so the
 *  conversational view stays brief. */
function CollapsibleDetails({ text }: { text: string }) {
  const [open, setOpen] = React.useState(false);
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
      >
        {open ? (
          <ChevronDown className="h-3 w-3" />
        ) : (
          <ChevronRight className="h-3 w-3" />
        )}
        {open ? "Hide details" : "More details"}
      </button>
      {open && (
        <p className="mt-1.5 text-foreground/85 whitespace-pre-wrap">
          {text}
        </p>
      )}
    </div>
  );
}

function ShowFullReviewToggle({ content }: { content: string }) {
  const [open, setOpen] = React.useState(false);
  return (
    <div className="border-t border-border/60 pt-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
      >
        {open ? (
          <ChevronDown className="h-3 w-3" />
        ) : (
          <ChevronRight className="h-3 w-3" />
        )}
        {open ? "Hide full review" : "Show full review"}
      </button>
      {open && (
        <pre className="mt-2 max-h-[420px] overflow-auto whitespace-pre-wrap rounded border border-border/60 bg-muted/30 p-2 font-sans text-[12px] leading-relaxed text-foreground/90 scrollbar-thin">
          {content}
        </pre>
      )}
    </div>
  );
}
