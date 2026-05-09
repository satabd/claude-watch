/** Prompt rendering and "no prompt" hint, extracted from review-panel.tsx
 *  so the inline-discussion path (Phase C) can render the same prompt
 *  card shape under each Claude turn that the side panel renders below
 *  the chat scroll today.
 *
 *  Three components:
 *    - PromptMarkdown    — Markdown renderer with prompt-shaped styling.
 *    - NextPromptBox     — small, in-message prompt card. Inside reviewer
 *                          message cards. Stays tight to its parent.
 *    - LatestPromptBox   — the "first-class" prominent prompt card. Used
 *                          by the side panel below the chat scroll, and
 *                          will be reused inline (Phase C) under the
 *                          relevant Claude turn.
 *    - NoPromptHint      — inline notice when no prompt was extracted.
 *                          Per spec we never copy a "best guess".
 *
 *  Behavior is identical to the in-line versions that lived in
 *  review-panel.tsx. No styling or wiring changes. */
import * as React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Copy, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { formatRelative } from "@/lib/utils";

/** Renders a Markdown-formatted prompt: bullets, numbered lists, and
 *  fenced code blocks come through as proper visuals; plain prose stays
 *  legible. The wrapper class keeps it visually anchored as a "prompt
 *  block" with light typographic spacing.
 *
 *  We DON'T strip outer fences here — the parser already did that via
 *  ``stripFences`` so the prompt is clean by the time it reaches us. */
export function PromptMarkdown({ text }: { text: string }) {
  return (
    <div className="prompt-markdown text-[13px] leading-relaxed text-foreground">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // Tight spacing — matches the existing chat density.
          p: ({ children, ...props }) => (
            <p {...props} className="my-1.5 whitespace-pre-wrap">
              {children}
            </p>
          ),
          ul: ({ children, ...props }) => (
            <ul {...props} className="my-1.5 ms-5 list-disc space-y-0.5">
              {children}
            </ul>
          ),
          ol: ({ children, ...props }) => (
            <ol {...props} className="my-1.5 ms-5 list-decimal space-y-0.5">
              {children}
            </ol>
          ),
          li: ({ children, ...props }) => (
            <li {...props} className="leading-relaxed">
              {children}
            </li>
          ),
          code: ({ children, ...props }) => {
            const inline = !(props as any).className?.includes("language-");
            if (inline) {
              return (
                <code
                  className="rounded bg-muted px-1 py-0.5 font-mono text-[12px]"
                >
                  {children}
                </code>
              );
            }
            return (
              <code className="font-mono text-[12px]">{children}</code>
            );
          },
          pre: ({ children, ...props }) => (
            <pre
              {...props}
              className="my-2 overflow-x-auto rounded-md bg-muted/60 p-2.5 font-mono text-[12px] leading-relaxed scrollbar-thin"
            >
              {children}
            </pre>
          ),
          h1: ({ children, ...props }) => (
            <h3 {...props} className="mt-2 mb-1 text-[14px] font-semibold">
              {children}
            </h3>
          ),
          h2: ({ children, ...props }) => (
            <h4 {...props} className="mt-2 mb-1 text-[13px] font-semibold">
              {children}
            </h4>
          ),
          h3: ({ children, ...props }) => (
            <h5 {...props} className="mt-1.5 mb-1 text-[12.5px] font-semibold">
              {children}
            </h5>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

/** Tight "next prompt" card rendered inside a reviewer message bubble.
 *  Gets dropped from the message body when ``hidePromptBox`` is true
 *  (which the side panel does, since it hoists the prompt into a
 *  prominent LatestPromptBox below the chat scroll). */
export function NextPromptBox({
  label,
  prompt,
  onCopy,
}: {
  label: string;
  prompt: string;
  onCopy: () => void;
}) {
  return (
    <section className="rounded-md border border-primary/40 bg-background p-3">
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <h3 className="text-[10.5px] font-semibold uppercase tracking-wider text-primary">
          {label}
        </h3>
        <Button
          size="sm"
          variant="outline"
          className="h-7"
          onClick={onCopy}
          title="Copy this prompt to the clipboard (no fences, no preamble)"
        >
          <Copy className="h-3 w-3" />
          Copy next prompt
        </Button>
      </div>
      <PromptMarkdown text={prompt} />
    </section>
  );
}

/** Hoisted "first-class" prompt box. The side panel renders one of these
 *  below the message scroll; the inline-discussion surface (Phase C)
 *  will render one under the Claude turn whose discussion produced it. */
export function LatestPromptBox({
  prompt,
  modeLabel,
  createdAt,
  onCopy,
  className,
}: {
  prompt: string;
  modeLabel: string;
  createdAt: number;
  onCopy: () => void;
  /** Caller-supplied container styling so the side panel and the
   *  inline surface can position the card without wrapping a
   *  redundant <div>. The default keeps the side-panel margins. */
  className?: string;
}) {
  return (
    <section
      className={
        className ??
        "mx-4 mb-3 rounded-lg border-2 border-primary/50 bg-background shadow-sm"
      }
    >
      <header className="flex items-center justify-between gap-2 border-b border-primary/20 bg-primary/5 px-3 py-2">
        <h3 className="flex items-center gap-1.5 text-[11.5px] font-semibold uppercase tracking-wider text-primary">
          <Sparkles className="h-3.5 w-3.5" />
          {modeLabel}
        </h3>
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-muted-foreground">
            {formatRelative(createdAt)}
          </span>
          <Button
            size="sm"
            variant="outline"
            className="h-7"
            onClick={onCopy}
            title="Copy this prompt to the clipboard (no fences, no preamble)"
          >
            <Copy className="h-3 w-3" />
            Copy prompt
          </Button>
        </div>
      </header>
      <div className="px-3 py-2.5">
        <PromptMarkdown text={prompt} />
      </div>
    </section>
  );
}

/** Inline hint shown when the reviewer didn't include a copy-ready
 *  prompt section. Per spec, we don't copy a "best guess" of the whole
 *  reply — we tell the user nothing was extracted instead. */
export function NoPromptHint() {
  return (
    <div className="rounded-md border border-dashed border-border bg-muted/30 px-3 py-2 text-[11.5px] text-muted-foreground">
      No prompt found yet — ask the reviewer to write one (or try a
      follow-up like "give me the prompt to send Claude").
    </div>
  );
}
