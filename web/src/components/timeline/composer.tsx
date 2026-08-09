import * as React from "react";
import {
  Loader2,
  Pencil,
  PenLine,
  Send,
  ShieldAlert,
  SlidersHorizontal,
  Square,
  Trash2,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  api,
  type PendingPrompt,
  type PermissionMode,
  type RuntimeState,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { toast } from "sonner";

/** Chat-style composer + pending-prompt queue anchored under the timeline.
 *
 *  Workflow (deliberately explicit; nothing auto-sends):
 *    Write Prompt -> compose/edit -> Queue -> pending card -> Send
 *  Send ensures a managed Zellij runtime first (asking for confirmation
 *  when that would take over an external claude TUI), then delivers the
 *  prompt through the pane. Double-send is prevented server-side; the
 *  user turn then arrives via the normal JSONL -> SSE read path.
 */
export function Composer({ bucket, sessionId }: { bucket: string; sessionId: string }) {
  const [runtime, setRuntime] = React.useState<RuntimeState | null>(null);
  const [pending, setPending] = React.useState<PendingPrompt[]>([]);
  const [composing, setComposing] = React.useState(false);
  const [draft, setDraft] = React.useState("");
  const [editingId, setEditingId] = React.useState<number | null>(null);
  const [editText, setEditText] = React.useState("");
  const [sendingId, setSendingId] = React.useState<number | null>(null);
  const [confirmTakeover, setConfirmTakeover] = React.useState<{
    promptId: number;
    reason: string;
  } | null>(null);

  const refreshRuntime = React.useCallback(() => {
    api.runtimeState(bucket, sessionId).then(setRuntime).catch(() => setRuntime(null));
  }, [bucket, sessionId]);

  const refreshPending = React.useCallback(() => {
    api.pendingList(bucket, sessionId).then(setPending).catch(() => {});
  }, [bucket, sessionId]);

  React.useEffect(() => {
    setComposing(false);
    setDraft("");
    setEditingId(null);
    setConfirmTakeover(null);
    refreshRuntime();
    refreshPending();
  }, [bucket, sessionId, refreshRuntime, refreshPending]);

  // Poll fast while a managed session is live (so the working indicator and
  // permission dialogs feel immediate), slowly otherwise to keep the cost of
  // the per-poll `zellij dump-screen` subprocess down.
  const pollMs =
    runtime?.state === "managed" ? (runtime.working ? 1500 : 3000) : 8000;
  React.useEffect(() => {
    const t = setInterval(refreshRuntime, pollMs);
    return () => clearInterval(t);
  }, [refreshRuntime, pollMs]);

  const queuePrompt = async () => {
    const text = draft.trim();
    if (!text) return;
    try {
      await api.pendingCreate(bucket, sessionId, text);
      setDraft("");
      setComposing(false);
      refreshPending();
    } catch (e: any) {
      toast.error(e?.message ?? "Failed to queue prompt");
    }
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

  const sendPrompt = async (id: number, allowTakeover = false) => {
    setSendingId(id);
    setConfirmTakeover(null);
    try {
      if (runtime?.state !== "managed") {
        try {
          await api.runtimeControl(bucket, sessionId, allowTakeover);
        } catch (e: any) {
          if (e?.detail?.needs_takeover_confirmation) {
            setConfirmTakeover({
              promptId: id,
              reason:
                runtime?.reason ??
                "An external claude TUI owns this session. Taking control closes it and resumes the session under claude-watch.",
            });
            return;
          }
          throw e;
        }
      }
      await api.pendingSend(bucket, sessionId, id);
      toast.success("Prompt sent to Claude");
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
            Pending prompt
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
              {confirmTakeover?.promptId === p.id ? (
                <div className="mt-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-2 text-[12px]">
                  <div className="flex items-start gap-1.5 text-amber-700 dark:text-amber-300">
                    <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    {confirmTakeover.reason}
                  </div>
                  <div className="mt-2 flex justify-end gap-1.5">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setConfirmTakeover(null)}
                    >
                      Cancel
                    </Button>
                    <Button
                      size="sm"
                      variant="destructive"
                      disabled={sendingId === p.id}
                      onClick={() => sendPrompt(p.id, true)}
                    >
                      Take over & send
                    </Button>
                  </div>
                </div>
              ) : (
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
                    Send
                  </Button>
                </div>
              )}
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
            placeholder="Write the prompt to queue for this Claude session…"
            dir="auto"
            rows={Math.min(12, Math.max(3, draft.split("\n").length))}
            className="w-full resize-y rounded-md border border-border bg-background p-2 text-sm outline-none focus:border-primary/50"
          />
          <div className="mt-1.5 flex items-center justify-between">
            <span className="text-[11px] text-muted-foreground">
              Queuing does not send — you review and press Send explicitly.
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
              <Button size="sm" disabled={!draft.trim()} onClick={queuePrompt}>
                Queue prompt
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
                : "Compose a prompt for this session"
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

const MODE_LABELS: Record<PermissionMode, string> = {
  manual: "Manual",
  accept_edits: "Accept edits",
  plan: "Plan",
  auto: "Auto",
  dont_ask: "Don't ask",
  bypass: "Bypass",
};
/** Switchable from the UI — these are the modes the TUI's Shift+Tab cycle
 *  visits. `auto` / `dont_ask` / `bypass` are launch-time
 *  `--permission-mode` choices: shown as a badge when active, but cycling
 *  cannot reach them, so we don't offer a button that would always fail. */
const MODE_CHOICES: PermissionMode[] = ["manual", "accept_edits", "plan"];
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
            `Session was started with --permission-mode ${mode}. ` +
            "Shift+Tab cycling can't reach it; restart the session to change it."
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
