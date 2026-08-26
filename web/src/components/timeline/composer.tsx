import * as React from "react";
import {
  Loader2,
  Pencil,
  PenLine,
  PowerOff,
  Send,
  ShieldAlert,
  SlidersHorizontal,
  Square,
  TerminalSquare,
  Trash2,
  X,
} from "lucide-react";
import { copyText } from "@/lib/clipboard";
import { Button } from "@/components/ui/button";
import {
  api,
  type PendingPrompt,
  type PermissionMode,
  type RuntimeState,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { useApp } from "@/store/app";
import { toast } from "sonner";

/** Chat-style composer anchored under the timeline.
 *
 *  Only rendered for sessions claude-watch can drive. Sending resumes the
 *  session into a Zellij pane if it is not already in one, then types into
 *  that pane; the user turn comes back via the normal JSONL -> SSE read path.
 *
 *  A session someone else is running is view-only — the server refuses with a
 *  409 and the reason lands in a toast. There is no takeover: see CLAUDE.md,
 *  "Ownership".
 *
 *  The prompt is recorded server-side as a "pending" row a beat before it is
 *  injected — that row is the atomic double-send guard, not a queue the user
 *  has to shepherd. Rows only stay visible when a send *failed*, so the text
 *  is never lost; the happy path never shows a card.
 */
/** `sendingId` value used while the *draft* is being turned into a row —
 *  before it has a real id. Negative so it can never collide with one. */
const DRAFT_SENDING = -1;

export function Composer({ bucket, sessionId }: { bucket: string; sessionId: string }) {
  // The composer owns the poll loop; the store copy is what the status bar
  // reads, so there is exactly one `zellij dump-screen` per interval.
  const runtime = useApp((s) => s.runtime);
  const setRuntime = useApp((s) => s.setRuntime);
  const [pending, setPending] = React.useState<PendingPrompt[]>([]);
  const [composing, setComposing] = React.useState(false);
  const [draft, setDraft] = React.useState("");
  const [editingId, setEditingId] = React.useState<number | null>(null);
  const [editText, setEditText] = React.useState("");
  const [sendingId, setSendingId] = React.useState<number | null>(null);

  const refreshRuntime = React.useCallback(() => {
    api.runtimeState(bucket, sessionId).then(setRuntime).catch(() => setRuntime(null));
  }, [bucket, sessionId, setRuntime]);

  const refreshPending = React.useCallback(() => {
    api.pendingList(bucket, sessionId).then(setPending).catch(() => {});
  }, [bucket, sessionId]);

  React.useEffect(() => {
    setComposing(false);
    setDraft("");
    setEditingId(null);
    // Drop the previous session's runtime immediately: the status bar reads
    // this, and showing another session's pane for a poll interval would be
    // worse than showing nothing.
    setRuntime(null);
    refreshRuntime();
    refreshPending();
  }, [bucket, sessionId, refreshRuntime, refreshPending, setRuntime]);

  // The status bar outlives this component, so leave nothing behind.
  React.useEffect(() => () => setRuntime(null), [setRuntime]);

  // Poll fast while a managed session is live (so the working indicator and
  // permission dialogs feel immediate), slowly otherwise to keep the cost of
  // the per-poll `zellij dump-screen` subprocess down.
  const pollMs =
    runtime?.state === "managed" ? (runtime.working ? 1500 : 3000) : 8000;
  React.useEffect(() => {
    const t = setInterval(refreshRuntime, pollMs);
    return () => clearInterval(t);
  }, [refreshRuntime, pollMs]);

  /** Type -> Send in one action: record the guard row, then deliver it. */
  const sendDraft = async () => {
    const text = draft.trim();
    if (!text || sendingId === DRAFT_SENDING) return;
    setSendingId(DRAFT_SENDING);
    let created: PendingPrompt;
    try {
      created = await api.pendingCreate(bucket, sessionId, text);
    } catch (e: any) {
      setSendingId(null);
      toast.error(e?.message ?? "Failed to send prompt");
      return;
    }
    // Clear the box optimistically — on failure the row stays on screen as a
    // card with the full text, so nothing the user typed can be lost.
    setDraft("");
    setComposing(false);
    setSendingId(null);
    refreshPending();
    await sendPrompt(created.id);
  };

  const saveEdit = async (id: number) => {
    const text = editText.trim();
    if (!text) return;
    try {
      await api.pendingEdit(id, text);
      setEditingId(null);
      refreshPending();
    } catch (e: any) {
      toast.error(e?.message ?? "Edit failed");
    }
  };

  const discard = async (id: number) => {
    try {
      await api.pendingDelete(id);
      refreshPending();
    } catch (e: any) {
      toast.error(e?.message ?? "Delete failed");
    }
  };

  const sendPrompt = async (id: number) => {
    setSendingId(id);
    try {
      if (runtime?.state !== "managed") {
        // Resumes only when nothing else is alive on the transcript; a 409
        // carries the reason straight to the toast below.
        await api.runtimeControl(bucket, sessionId);
      }
      await api.pendingSend(bucket, sessionId, id);
      toast.success("Prompt sent to Claude");
      // A delivered prompt leaves the pending list server-side, so the card
      // disappears on its own — no queue to tidy up.
      refreshPending();
      refreshRuntime();
    } catch (e: any) {
      toast.error(e?.message ?? "Send failed");
      refreshRuntime();
      refreshPending();
    } finally {
      setSendingId(null);
    }
  };

  /** Resume the session into a Zellij pane without sending anything, so it
   *  can be attached to and watched. Same guarded path a send takes. */
  const [opening, setOpening] = React.useState(false);
  const openRuntime = async () => {
    setOpening(true);
    try {
      const st = await api.runtimeControl(bucket, sessionId);
      setRuntime(st);
      toast.success(
        st.attach_command
          ? `Running in zellij — ${st.attach_command}`
          : "Session is now managed"
      );
    } catch (e: any) {
      toast.error(e?.detail?.reason ?? e?.message ?? "Could not resume the session");
      refreshRuntime();
    } finally {
      setOpening(false);
    }
  };

  /** Close the pane and confirm its claude actually exited. */
  const [releasing, setReleasing] = React.useState(false);
  const release = async () => {
    setReleasing(true);
    try {
      const r = await api.runtimeRelease(bucket, sessionId);
      if (r.surviving_pids?.length) {
        toast.error(
          `Pane closed, but claude ${r.surviving_pids.join(", ")} is still ` +
            "running and writing to this transcript."
        );
      } else if (r.reaped_pids?.length) {
        toast.success(
          `Pane closed (claude ${r.reaped_pids.join(", ")} had to be signalled).`
        );
      } else {
        toast.success("Pane closed");
      }
    } catch (e: any) {
      toast.error(e?.message ?? "Could not close the pane");
    } finally {
      setReleasing(false);
      refreshRuntime();
    }
  };

  const interrupt = async () => {
    try {
      await api.runtimeInterrupt(bucket, sessionId);
      toast.success("Sent interrupt (Esc)");
    } catch (e: any) {
      toast.error(e?.message ?? "Interrupt failed");
    }
  };

  const [settingMode, setSettingMode] = React.useState(false);
  const changeMode = async (mode: PermissionMode) => {
    setSettingMode(true);
    try {
      await api.runtimeSetMode(bucket, sessionId, mode);
      toast.success(`Mode: ${MODE_LABELS[mode]}`);
    } catch (e: any) {
      toast.error(e?.message ?? "Could not change mode");
    } finally {
      setSettingMode(false);
      refreshRuntime();
    }
  };

  const [responding, setResponding] = React.useState(false);
  const respond = async (choice: string) => {
    setResponding(true);
    try {
      await api.runtimeRespond(bucket, sessionId, choice);
      toast.success(choice === "esc" ? "Cancelled" : `Answered: option ${choice}`);
    } catch (e: any) {
      toast.error(e?.message ?? "Respond failed");
    } finally {
      setResponding(false);
      refreshRuntime();
    }
  };

  const controlUnavailable =
    runtime !== null && !runtime.controllable && runtime.state === "inactive";

  return (
    <div className="mx-4 mb-6 mt-2 border-t border-border pt-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <StatusChip runtime={runtime} onInterrupt={interrupt} />
        {runtime?.state === "managed" && (
          <ModeSelector
            mode={runtime.mode}
            busy={settingMode}
            working={runtime.working}
            onChange={changeMode}
          />
        )}
        {runtime?.attach_command && <AttachHint runtime={runtime} />}
        {runtime?.state === "managed" && (
          <button
            type="button"
            disabled={releasing}
            onClick={release}
            className="inline-flex items-center gap-1 rounded-md border border-border px-1.5 py-0.5 text-[11px] text-muted-foreground transition-colors hover:border-red-400/60 hover:text-red-500 disabled:opacity-50"
            title="Close the zellij pane and stop the claude running in it"
          >
            {releasing ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <PowerOff className="h-3 w-3" />
            )}
            Close pane
          </button>
        )}
        {runtime !== null &&
          runtime.state !== "managed" &&
          runtime.controllable &&
          runtime.state !== "external_idle" && (
            <button
              type="button"
              disabled={opening}
              onClick={() => openRuntime()}
              className="inline-flex items-center gap-1 rounded-md border border-border px-1.5 py-0.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground disabled:opacity-50"
              title="Resume this session in a Zellij pane you can attach to"
            >
              {opening ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <TerminalSquare className="h-3 w-3" />
              )}
              Resume in claude-watch
            </button>
          )}
      </div>

      {runtime?.awaiting_input && (
        <div className="mt-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3">
          <div className="mb-1 flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-amber-700 dark:text-amber-300">
            <ShieldAlert className="h-3.5 w-3.5" /> Claude is asking
          </div>
          <div dir="auto" className="text-sm">
            {runtime.awaiting_input.question}
          </div>
          <div className="mt-2 flex flex-wrap justify-end gap-1.5">
            <Button
              variant="ghost"
              size="sm"
              disabled={responding}
              onClick={() => respond("esc")}
              title="Send Esc (cancel the dialog)"
            >
              Cancel (Esc)
            </Button>
            {runtime.awaiting_input.options.map((o) => (
              <Button
                key={o.n}
                variant={o.n === "1" ? "default" : "secondary"}
                size="sm"
                disabled={responding}
                onClick={() => respond(o.n)}
              >
                {o.n}. {o.label}
              </Button>
            ))}
          </div>
        </div>
      )}

      {pending.map((p) => (
        <div
          key={p.id}
          className="mt-2 rounded-lg border border-primary/25 bg-primary/5 p-3"
        >
          <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            Not delivered — retry or discard
          </div>
          {editingId === p.id ? (
            <>
              <textarea
                value={editText}
                onChange={(e) => setEditText(e.target.value)}
                dir="auto"
                rows={Math.min(10, Math.max(3, editText.split("\n").length))}
                className="w-full resize-y rounded-md border border-border bg-background p-2 text-sm outline-none focus:border-primary/50"
              />
              <div className="mt-2 flex justify-end gap-1.5">
                <Button variant="ghost" size="sm" onClick={() => setEditingId(null)}>
                  Cancel
                </Button>
                <Button size="sm" onClick={() => saveEdit(p.id)}>
                  Save
                </Button>
              </div>
            </>
          ) : (
            <>
              <div dir="auto" className="whitespace-pre-wrap break-words text-sm">
                {p.text}
              </div>
              <div className="mt-2 flex justify-end gap-1.5">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => discard(p.id)}
                  title="Discard this pending prompt"
                >
                  <Trash2 className="mr-1 h-3 w-3" /> Discard
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => {
                    setEditingId(p.id);
                    setEditText(p.text);
                  }}
                >
                  <Pencil className="mr-1 h-3 w-3" /> Edit
                </Button>
                <Button
                  size="sm"
                  disabled={sendingId === p.id || controlUnavailable}
                  onClick={() => sendPrompt(p.id)}
                >
                  {sendingId === p.id ? (
                    <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                  ) : (
                    <Send className="mr-1 h-3 w-3" />
                  )}
                  Retry send
                </Button>
              </div>
            </>
          )}
        </div>
      ))}

      {composing ? (
        <div className="mt-2">
          <textarea
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              // Enter inserts a newline (prompts are usually multi-line);
              // Cmd/Ctrl+Enter is the send chord, same as the TUI composer.
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                void sendDraft();
              }
            }}
            placeholder="Write a prompt for this Claude session…"
            dir="auto"
            rows={Math.min(12, Math.max(3, draft.split("\n").length))}
            className="w-full resize-y rounded-md border border-border bg-background p-2 text-sm outline-none focus:border-primary/50"
          />
          <div className="mt-1.5 flex items-center justify-between">
            <span className="text-[11px] text-muted-foreground">
              ⌘/Ctrl + Enter to send. Goes straight into the live Claude pane.
            </span>
            <div className="flex gap-1.5">
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setComposing(false);
                  setDraft("");
                }}
              >
                <X className="mr-1 h-3 w-3" /> Cancel
              </Button>
              <Button
                size="sm"
                disabled={!draft.trim() || sendingId !== null || controlUnavailable}
                onClick={sendDraft}
                title={
                  controlUnavailable
                    ? runtime?.reason ?? "Control unavailable"
                    : "Send this prompt to the live Claude session"
                }
              >
                {sendingId !== null ? (
                  <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                ) : (
                  <Send className="mr-1 h-3 w-3" />
                )}
                Send
              </Button>
            </div>
          </div>
        </div>
      ) : (
        <div className="mt-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setComposing(true)}
            disabled={controlUnavailable}
            title={
              controlUnavailable
                ? runtime?.reason ?? "Control unavailable"
                : "Write a prompt for this session"
            }
          >
            <PenLine className="mr-1.5 h-3.5 w-3.5" /> Write Prompt
          </Button>
          {controlUnavailable && (
            <span className="ms-2 text-[11px] text-muted-foreground">
              {runtime?.reason}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

const STATE_STYLE: Record<
  RuntimeState["state"],
  { label: string; dot: string }
> = {
  managed: { label: "Managed by claude-watch", dot: "bg-emerald-500" },
  external_idle: { label: "External · idle", dot: "bg-sky-500" },
  external_busy: { label: "External · working", dot: "bg-amber-500 animate-pulse" },
  inactive: { label: "Not running", dot: "bg-zinc-400" },
  resumable: { label: "Resumable", dot: "bg-violet-500" },
};

function StatusChip({
  runtime,
  onInterrupt,
}: {
  runtime: RuntimeState | null;
  onInterrupt: () => void;
}) {
  if (!runtime) {
    return (
      <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
        <span className="h-2 w-2 rounded-full bg-zinc-300" /> Runtime state unknown
      </div>
    );
  }
  const s = STATE_STYLE[runtime.state];
  const working = runtime.working || (runtime.state === "managed" && runtime.busy);
  return (
    <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
      {working ? (
        <span className="relative flex h-2 w-2">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500" />
        </span>
      ) : (
        <span className={cn("h-2 w-2 rounded-full", s.dot)} />
      )}
      <span>{s.label}</span>
      {working && (
        <>
          <span className="inline-flex items-center gap-1 font-medium text-emerald-600 dark:text-emerald-400">
            <Loader2 className="h-3 w-3 animate-spin" />
            {runtime.activity
              ? `${runtime.activity.verb}… ${runtime.activity.elapsed_s}s`
              : "Working…"}
          </span>
          {runtime.activity?.detail && (
            <span className="text-muted-foreground">
              ({runtime.activity.detail})
            </span>
          )}
          <button
            type="button"
            onClick={onInterrupt}
            className="inline-flex items-center gap-1 rounded-md border border-border px-1.5 py-0.5 transition-colors hover:border-red-400/60 hover:text-red-500"
            title="Send Esc to interrupt the current turn"
          >
            <Square className="h-2.5 w-2.5" /> Interrupt
          </button>
        </>
      )}
    </div>
  );
}

/** "This session lives in zellij — here's how to look at it." Claude Watch
 *  always runs managed sessions inside a project-named zellij session, so
 *  the attach command is stable and worth having one click away. */
function AttachHint({ runtime }: { runtime: RuntimeState }) {
  const cmd = runtime.attach_command!;
  return (
    <button
      type="button"
      onClick={async () => {
        try {
          await copyText(cmd);
          toast.success(`Copied: ${cmd}`);
        } catch (e: any) {
          toast.error(e?.message ?? "Copy failed");
        }
      }}
      className="inline-flex items-center gap-1 rounded-md border border-border px-1.5 py-0.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
      title={`Copy "${cmd}" — attaches a terminal to the zellij session holding this pane`}
    >
      <TerminalSquare className="h-3 w-3" />
      <span className="font-mono">{runtime.pane_title ?? runtime.zellij_session}</span>
    </button>
  );
}

const MODE_LABELS: Record<PermissionMode, string> = {
  manual: "Manual",
  accept_edits: "Accept edits",
  plan: "Plan",
  auto: "Auto",
  dont_ask: "Don't ask",
  bypass: "Bypass",
};
/** Switchable from the UI. `auto` leads because it is what claude-watch
 *  launches its own sessions in. `bypass` is deliberately absent: it is never
 *  in the Shift+Tab cycle, so a button for it would always fail — and landing
 *  there by accident is not a mistake worth making easy. */
const MODE_CHOICES: PermissionMode[] = ["auto", "manual", "accept_edits", "plan"];
/** Modes that let Claude act without asking — worth flagging visually. */
const PERMISSIVE_MODES: PermissionMode[] = ["dont_ask", "bypass"];

function ModeSelector({
  mode,
  busy,
  working,
  onChange,
}: {
  mode: PermissionMode | null;
  busy: boolean;
  working: boolean;
  onChange: (m: PermissionMode) => void;
}) {
  // A launch-time mode (auto / don't ask / bypass) can't be reached by
  // cycling, so the segmented control shows no active button — surface the
  // real mode as a badge instead of silently looking like "no mode".
  const offCycle = mode !== null && !MODE_CHOICES.includes(mode);
  const permissive = mode !== null && PERMISSIVE_MODES.includes(mode);

  return (
    <div className="flex items-center gap-1 text-[11px]">
      <SlidersHorizontal className="h-3 w-3 text-muted-foreground" />
      <span className="text-muted-foreground">Mode</span>
      <div className="ms-0.5 inline-flex overflow-hidden rounded-md border border-border">
        {MODE_CHOICES.map((m) => (
          <button
            key={m}
            type="button"
            disabled={busy || working || m === mode}
            onClick={() => onChange(m)}
            title={
              working
                ? "Wait for the current turn to finish"
                : `Switch to ${MODE_LABELS[m]} (Shift+Tab in the TUI)`
            }
            className={cn(
              "px-1.5 py-0.5 transition-colors",
              m === mode
                ? "bg-primary/15 font-medium text-foreground"
                : "text-muted-foreground hover:bg-accent/40 hover:text-foreground",
              (busy || working) && m !== mode && "opacity-50"
            )}
          >
            {MODE_LABELS[m]}
          </button>
        ))}
      </div>
      {offCycle && (
        <span
          className={cn(
            "ms-1 rounded-md border px-1.5 py-0.5",
            permissive
              ? "border-red-500/40 bg-red-500/10 text-red-600 dark:text-red-400"
              : "border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-300"
          )}
          title={
            `Session is in ${MODE_LABELS[mode!]} mode, which this claude ` +
            "build's Shift+Tab cycle doesn't visit — restart the session " +
            "with --permission-mode to change it."
          }
        >
          {MODE_LABELS[mode!]}
          {permissive && " ⚠"}
        </span>
      )}
      {mode === null && (
        <span className="ms-1 text-muted-foreground">unknown</span>
      )}
      {busy && <Loader2 className="h-3 w-3 animate-spin" />}
    </div>
  );
}
