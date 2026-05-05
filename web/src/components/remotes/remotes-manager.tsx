import * as React from "react";
import {
  Plus,
  Trash2,
  RefreshCw,
  Plug,
  Check,
  X,
  Loader2,
  ChevronDown,
  ChevronRight,
  Globe,
  AlertCircle,
  Search,
  Terminal,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  api,
  type RemoteHost,
  type RemoteHostCreate,
  type WslDistroInfo,
} from "@/lib/api";
import { cn, formatRelative } from "@/lib/utils";
import { toast } from "sonner";

export function RemotesManager() {
  const [hosts, setHosts] = React.useState<RemoteHost[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [adding, setAdding] = React.useState(false);
  const [busy, setBusy] = React.useState<Record<number, "test" | "sync" | "delete" | null>>(
    {}
  );
  const [discoveringWsl, setDiscoveringWsl] = React.useState(false);
  const [wslResults, setWslResults] = React.useState<{
    items: WslDistroInfo[];
    suggestions: RemoteHostCreate[];
  } | null>(null);

  const reload = React.useCallback(async () => {
    setLoading(true);
    try {
      setHosts(await api.remotesList());
    } catch (e: any) {
      toast.error(e?.message ?? "Failed to load remotes");
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    reload();
    // Live-poll the host list so the status badge / last-poll / activity
    // timestamps stay fresh while the settings sheet is open. Cheap: just a
    // SQLite read on the backend. Stops automatically when the sheet unmounts.
    const t = setInterval(() => {
      api.remotesList().then(setHosts).catch(() => {});
    }, 2000);
    return () => clearInterval(t);
  }, [reload]);

  const onCreate = async (req: RemoteHostCreate) => {
    try {
      await api.remotesCreate(req);
      setAdding(false);
      reload();
      toast.success(`Added '${req.name}'`);
    } catch (e: any) {
      toast.error(e?.message ?? "Add failed");
    }
  };

  const onDiscoverWsl = async () => {
    setDiscoveringWsl(true);
    try {
      const r = await api.remotesDiscoverWsl();
      setWslResults(r);
      if (r.items.length === 0) {
        toast.info("No WSL distros detected");
      } else {
        toast.success(
          `Found ${r.items.length} distro${r.items.length === 1 ? "" : "s"} · ${r.suggestions.length} ready for SSH`
        );
      }
    } catch (e: any) {
      toast.error(e?.message ?? "Discover failed");
    } finally {
      setDiscoveringWsl(false);
    }
  };

  const addSuggestion = async (s: RemoteHostCreate) => {
    try {
      await api.remotesCreate(s);
      // Mark this distro as added so we hide its Add button
      setWslResults((prev) =>
        prev ? { ...prev, suggestions: prev.suggestions.filter((x) => x.name !== s.name) } : prev
      );
      reload();
      toast.success(`Added '${s.name}'. Click Test, then Sync now.`);
    } catch (e: any) {
      toast.error(e?.message ?? "Add failed");
    }
  };

  const onTest = async (h: RemoteHost) => {
    setBusy((b) => ({ ...b, [h.id]: "test" }));
    try {
      const r = await api.remotesTest(h.id);
      if (r.ok) {
        toast.success(
          `Connected · ${r.platform ?? "?"} · ${r.bucket_count ?? 0} buckets at ${r.projects_path}`
        );
      } else {
        toast.error(r.error ?? "Test failed");
      }
      reload();
    } finally {
      setBusy((b) => ({ ...b, [h.id]: null }));
    }
  };

  const onSync = async (h: RemoteHost) => {
    setBusy((b) => ({ ...b, [h.id]: "sync" }));
    try {
      const r = await api.remotesSync(h.id);
      if (r.ok) {
        toast.success(
          `Synced ${h.name}: ${r.files_downloaded} new, ${r.files_unchanged} unchanged · ${(
            r.bytes_downloaded / 1024
          ).toFixed(1)} KB · ${r.elapsed_ms} ms`
        );
      } else {
        toast.error(r.error ?? "Sync failed");
      }
      reload();
    } catch (e: any) {
      toast.error(e?.message ?? "Sync failed");
    } finally {
      setBusy((b) => ({ ...b, [h.id]: null }));
    }
  };

  const onDelete = async (h: RemoteHost) => {
    if (!confirm(`Delete remote '${h.name}'? This also removes its mirrored files.`))
      return;
    setBusy((b) => ({ ...b, [h.id]: "delete" }));
    try {
      const r = await api.remotesDelete(h.id);
      toast.success(`Deleted (${r.files_removed} mirrored files removed)`);
      reload();
    } finally {
      setBusy((b) => ({ ...b, [h.id]: null }));
    }
  };

  const onToggle = async (h: RemoteHost) => {
    try {
      await api.remotesUpdate(h.id, { enabled: !h.enabled });
      reload();
    } catch (e: any) {
      toast.error(e?.message ?? "Update failed");
    }
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          <Globe className="h-3 w-3" /> Remote SSH hosts
        </div>
        <div className="flex items-center gap-1.5">
          <Button
            size="sm"
            variant="outline"
            className="h-7"
            onClick={onDiscoverWsl}
            disabled={discoveringWsl}
            title="Find WSL distros on this machine"
          >
            {discoveringWsl ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <Search className="h-3 w-3" />
            )}
            Discover WSL
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-7"
            onClick={() => setAdding((a) => !a)}
          >
            <Plus className="h-3 w-3" /> Add host
          </Button>
        </div>
      </div>

      {wslResults && wslResults.items.length > 0 && (
        <div className="rounded-md border border-border bg-card/60 p-2.5">
          <div className="mb-1.5 flex items-center justify-between text-[11px] uppercase tracking-wider text-muted-foreground">
            <span className="flex items-center gap-1.5">
              <Terminal className="h-3 w-3" /> WSL distros found
            </span>
            <button
              onClick={() => setWslResults(null)}
              className="hover:text-foreground"
              title="Dismiss"
            >
              <X className="h-3 w-3" />
            </button>
          </div>
          <div className="space-y-1.5">
            {wslResults.items.map((d) => {
              const suggestion = wslResults.suggestions.find(
                (s) => s.username === d.user && s.port === d.ssh_port
              );
              return (
                <div
                  key={d.name}
                  className={cn(
                    "rounded-md border p-2 text-[12px]",
                    d.ssh_running ? "border-emerald-500/40 bg-emerald-500/5" : "border-border"
                  )}
                >
                  <div className="flex items-center gap-2">
                    <span className="font-mono font-semibold">{d.name}</span>
                    {d.is_default && <Badge variant="info">default</Badge>}
                    <Badge variant="muted" className="font-mono normal-case">
                      {d.state}
                    </Badge>
                    {d.user && (
                      <span className="font-mono text-[11px] text-muted-foreground">
                        {d.user}@127.0.0.1:{d.ssh_port}
                      </span>
                    )}
                    <span className="ms-auto">
                      {d.ssh_running ? (
                        <Badge variant="success">sshd live</Badge>
                      ) : d.sshd_installed ? (
                        <Badge variant="warning">sshd installed</Badge>
                      ) : (
                        <Badge variant="outline">no sshd</Badge>
                      )}
                    </span>
                  </div>
                  {d.error && (
                    <div className="mt-1 text-[11px] text-destructive">{d.error}</div>
                  )}
                  {d.hint && (
                    <pre className="mt-1.5 whitespace-pre-wrap rounded bg-muted/50 p-2 font-mono text-[10.5px] leading-snug text-muted-foreground">
                      {d.hint}
                    </pre>
                  )}
                  {suggestion && (
                    <div className="mt-2 flex items-center gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7"
                        onClick={() => addSuggestion(suggestion)}
                      >
                        <Plus className="h-3 w-3" /> Add as remote
                      </Button>
                      <span className="text-[11px] text-muted-foreground">
                        will mirror {d.suggested_projects_path}
                      </span>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {adding && <AddHostForm onSubmit={onCreate} onCancel={() => setAdding(false)} />}

      {loading && hosts.length === 0 && (
        <div className="rounded-md border border-dashed border-border p-3 text-center text-[11px] text-muted-foreground">
          Loading…
        </div>
      )}

      {!loading && hosts.length === 0 && !adding && (
        <div className="rounded-md border border-dashed border-border bg-background/50 p-3 text-[12px] leading-relaxed text-muted-foreground">
          <div className="mb-1.5 text-[12px] font-medium text-foreground">
            No remote hosts yet
          </div>
          <ul className="space-y-1">
            <li className="flex items-start gap-2">
              <Plus className="mt-[2px] h-3 w-3 shrink-0 opacity-70" />
              <span>
                Click <b>Add host</b> to point the watcher at a remote machine's{" "}
                <code className="font-mono text-[11px]">~/.claude/projects</code>.
              </span>
            </li>
            <li className="flex items-start gap-2">
              <Search className="mt-[2px] h-3 w-3 shrink-0 opacity-70" />
              <span>
                On Windows, <b>Discover WSL</b> finds local distros and suggests
                ready-to-add hosts.
              </span>
            </li>
            <li className="flex items-start gap-2">
              <RefreshCw className="mt-[2px] h-3 w-3 shrink-0 opacity-70" />
              <span>
                Manual <b>Sync now</b> is optional — once a host is enabled, the
                live watcher tails active sessions automatically.
              </span>
            </li>
          </ul>
        </div>
      )}

      {hosts.map((h) => (
        <HostRow
          key={h.id}
          h={h}
          busy={busy[h.id] ?? null}
          onTest={() => onTest(h)}
          onSync={() => onSync(h)}
          onDelete={() => onDelete(h)}
          onToggle={() => onToggle(h)}
        />
      ))}
    </div>
  );
}

function HostRow({
  h,
  busy,
  onTest,
  onSync,
  onDelete,
  onToggle,
}: {
  h: RemoteHost;
  busy: "test" | "sync" | "delete" | null;
  onTest: () => void;
  onSync: () => void;
  onDelete: () => void;
  onToggle: () => void;
}) {
  const [expanded, setExpanded] = React.useState(false);
  const enabled = !!h.enabled;
  const lastPoll = h.last_poll_ms ?? h.last_synced_ms;
  const lastPollRel = lastPoll ? formatRelative(lastPoll) : "never";
  const lastEventRel = h.last_event_ms ? formatRelative(h.last_event_ms) : "—";
  const status = effectiveStatus(h);
  return (
    <div
      className={cn(
        "rounded-md border p-2.5 text-[12px]",
        status.tone === "error"
          ? "border-destructive/50"
          : status.tone === "warn"
          ? "border-amber-500/40"
          : status.tone === "ok"
          ? "border-emerald-500/40"
          : "border-border",
        !enabled && "opacity-60"
      )}
    >
      <div className="flex items-start gap-2">
        <button
          onClick={() => setExpanded((e) => !e)}
          className="mt-0.5 text-muted-foreground hover:text-foreground"
        >
          {expanded ? (
            <ChevronDown className="h-3 w-3" />
          ) : (
            <ChevronRight className="h-3 w-3" />
          )}
        </button>
        <div className="min-w-0 flex-1">
          <TooltipProvider delayDuration={150}>
            <div className="flex items-center gap-2">
              <span className="truncate font-semibold">{h.name}</span>
              <Tooltip>
                <TooltipTrigger asChild>
                  <span>
                    <StatusBadge status={status} />
                  </span>
                </TooltipTrigger>
                <TooltipContent className="max-w-[280px] text-[11px] leading-snug">
                  {STATUS_HELP[status.tone]}
                </TooltipContent>
              </Tooltip>
              {h.platform && (
                <Badge variant="muted" className="font-mono normal-case">
                  {h.platform}
                </Badge>
              )}
              <span className="ms-auto flex items-center gap-2 text-[11px] text-muted-foreground">
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span>poll {lastPollRel}</span>
                  </TooltipTrigger>
                  <TooltipContent className="max-w-[260px] text-[11px] leading-snug">
                    Time since the watcher last completed an SFTP poll cycle
                    against this host. Updates every {Math.round(2)}–10s while
                    the host is enabled. If this falls behind, the connection
                    has stalled.
                  </TooltipContent>
                </Tooltip>
                <span className="text-muted-foreground/60">·</span>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span>activity {lastEventRel}</span>
                  </TooltipTrigger>
                  <TooltipContent className="max-w-[260px] text-[11px] leading-snug">
                    Time since the watcher last observed a real change on the
                    remote (a JSONL grew or a new session appeared). "—" means
                    no activity since the watcher started.
                  </TooltipContent>
                </Tooltip>
                {h.next_retry_ms && h.next_retry_ms > Date.now() && (
                  <>
                    <span className="text-muted-foreground/60">·</span>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <span className="text-amber-600 dark:text-amber-300">
                          retry in{" "}
                          {Math.max(0, Math.ceil((h.next_retry_ms - Date.now()) / 1000))}s
                        </span>
                      </TooltipTrigger>
                      <TooltipContent className="max-w-[260px] text-[11px] leading-snug">
                        SSH connection failed; the watcher will retry at this
                        time using exponential backoff (capped at 60s). Click
                        Test if you want to attempt a manual reconnect.
                      </TooltipContent>
                    </Tooltip>
                  </>
                )}
              </span>
            </div>
          </TooltipProvider>
          <div className="mt-0.5 truncate font-mono text-[11px] text-muted-foreground">
            {h.username}@{h.host}:{h.port}
          </div>
          {h.last_error && (
            <div className="mt-1 flex items-start gap-1.5 rounded border border-destructive/50 bg-destructive/10 p-1.5 text-[11px] text-destructive">
              <AlertCircle className="mt-0.5 h-3 w-3 shrink-0" />
              <span className="break-all">{h.last_error}</span>
            </div>
          )}
        </div>
      </div>

      {expanded && (
        <div className="mt-2 space-y-1 rounded bg-muted/40 p-2 text-[11px] font-mono text-muted-foreground">
          <Field label="key_path" value={h.key_path || "(default ssh-agent / ~/.ssh/id_*)"} />
          <Field label="projects_path" value={h.projects_path || "(auto-detect from $HOME)"} />
          <Field label="home_dir" value={h.home_dir || "—"} />
          <Field label="enabled" value={enabled ? "yes" : "no"} />
          <Field label="status" value={status.label} />
          {h.next_retry_ms && (
            <Field
              label="next retry"
              value={
                h.next_retry_ms > Date.now()
                  ? `in ${Math.max(0, Math.ceil((h.next_retry_ms - Date.now()) / 1000))}s`
                  : "now"
              }
            />
          )}
        </div>
      )}

      <div className="mt-2 flex gap-1.5">
        <Button
          size="sm"
          variant="outline"
          className="h-7"
          onClick={onTest}
          disabled={busy !== null}
          title="Verify SSH auth and locate ~/.claude/projects"
        >
          {busy === "test" ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <Plug className="h-3 w-3" />
          )}
          Test
        </Button>
        <Button
          size="sm"
          variant="outline"
          className="h-7"
          onClick={onSync}
          disabled={busy !== null || !enabled}
          title="Download new/changed JSONLs to local mirror"
        >
          {busy === "sync" ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <RefreshCw className="h-3 w-3" />
          )}
          Sync now
        </Button>
        <Button
          size="sm"
          variant="ghost"
          className="h-7"
          onClick={onToggle}
          disabled={busy !== null}
        >
          {enabled ? <X className="h-3 w-3" /> : <Check className="h-3 w-3" />}
          {enabled ? "Disable" : "Enable"}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          className="ms-auto h-7 text-destructive/80 hover:text-destructive"
          onClick={onDelete}
          disabled={busy !== null}
        >
          {busy === "delete" ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <Trash2 className="h-3 w-3" />
          )}
          Remove
        </Button>
      </div>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="shrink-0 w-[90px] text-muted-foreground/70">{label}</span>
      <span className="break-all text-foreground/90">{value}</span>
    </div>
  );
}

interface Status {
  label: string;
  tone: "ok" | "warn" | "error" | "muted";
}

const STATUS_HELP: Record<Status["tone"], string> = {
  ok:
    "Live. Persistent SSH connection is up and the watcher is polling for changes. " +
    "New events on the remote propagate to the timeline within ~2s.",
  warn:
    "Connecting or reconnecting. The watcher is establishing the SSH connection or " +
    "retrying after a transient failure. Re-tries use exponential backoff up to 60s.",
  error:
    "Connection failed. Check the host's last error message below — typical causes: " +
    "wrong port, sshd not running, SSH key not configured, or network unreachable. " +
    "Use Test to retry on demand.",
  muted:
    "Disabled or stopped. The watcher is not running for this host. The local mirror " +
    "stays available for browsing but won't receive new updates. Enable to resume.",
};

/** Resolve a host's user-facing status from the persisted fields. */
function effectiveStatus(h: RemoteHost): Status {
  if (!h.enabled) return { label: "disabled", tone: "muted" };
  const raw = (h.status || "").toLowerCase();
  if (raw === "live" || raw === "polling") return { label: "live", tone: "ok" };
  if (raw === "connecting" || raw === "starting")
    return { label: raw, tone: "warn" };
  if (raw.startsWith("reconnecting")) return { label: h.status!, tone: "warn" };
  if (raw === "stopped") return { label: "stopped", tone: "muted" };
  if (h.last_error) return { label: "error", tone: "error" };
  if (h.last_poll_ms) return { label: "live", tone: "ok" };
  return { label: raw || "idle", tone: "muted" };
}

function StatusBadge({ status }: { status: Status }) {
  if (status.tone === "error") {
    return (
      <span className="inline-flex items-center rounded-md border border-transparent bg-destructive/15 px-1.5 py-0.5 text-[10.5px] font-medium leading-none text-destructive">
        {status.label}
      </span>
    );
  }
  const variant =
    status.tone === "ok" ? "success" : status.tone === "warn" ? "warning" : "muted";
  return <Badge variant={variant}>{status.label}</Badge>;
}

function AddHostForm({
  onSubmit,
  onCancel,
}: {
  onSubmit: (r: RemoteHostCreate) => void;
  onCancel: () => void;
}) {
  const [name, setName] = React.useState("");
  const [host, setHost] = React.useState("");
  const [port, setPort] = React.useState("22");
  const [username, setUsername] = React.useState("");
  const [keyPath, setKeyPath] = React.useState("");
  const [projectsPath, setProjectsPath] = React.useState("");

  const valid = name.trim() && host.trim() && username.trim();
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!valid) return;
    onSubmit({
      name: name.trim(),
      host: host.trim(),
      port: parseInt(port, 10) || 22,
      username: username.trim(),
      key_path: keyPath.trim() || null,
      projects_path: projectsPath.trim() || null,
    });
  };

  return (
    <form
      onSubmit={submit}
      className="space-y-2 rounded-md border border-border bg-card/60 p-3"
    >
      <div className="grid grid-cols-2 gap-2">
        <LabeledInput
          label="Display name"
          value={name}
          onChange={setName}
          placeholder="my-wsl"
        />
        <LabeledInput
          label="Host"
          value={host}
          onChange={setHost}
          placeholder="192.168.1.42 or hostname"
        />
        <LabeledInput
          label="Username"
          value={username}
          onChange={setUsername}
          placeholder="sat"
        />
        <LabeledInput
          label="Port"
          value={port}
          onChange={setPort}
          placeholder="22"
        />
      </div>
      <LabeledInput
        label="SSH key path (optional)"
        value={keyPath}
        onChange={setKeyPath}
        placeholder="~/.ssh/id_ed25519  (blank = ssh-agent + defaults)"
      />
      <LabeledInput
        label="projects_path override (optional)"
        value={projectsPath}
        onChange={setProjectsPath}
        placeholder="(auto = $HOME/.claude/projects)"
      />
      <div className="flex gap-2 pt-1">
        <Button type="submit" size="sm" className="h-7" disabled={!valid}>
          <Plus className="h-3 w-3" /> Add
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="h-7"
          onClick={onCancel}
        >
          Cancel
        </Button>
      </div>
    </form>
  );
}

function LabeledInput({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10.5px] uppercase tracking-wider text-muted-foreground">
        {label}
      </span>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="rounded-md border border-border bg-background px-2 py-1 text-[12px] outline-none focus:border-primary/60"
      />
    </label>
  );
}
