import * as React from "react";
import { Toaster } from "sonner";
import { Topbar } from "@/components/topbar";
import { Sidebar } from "@/components/sidebar";
import { Timeline } from "@/components/timeline/timeline";
import { Scratchpad } from "@/components/scratchpad";
import { StatusBar } from "@/components/status-bar";
import { useApp } from "@/store/app";
import { api } from "@/lib/api";
import { liveStream, type LiveEvent } from "@/lib/sse";

// Lazy-load heavy sheets so they don't ship in the initial bundle. SettingsSheet
// pulls RemotesManager (~685 lines) with it; PromptWriter is the largest single
// component (~716 lines) and pulls react-markdown/remark-gfm via its preview pane.
// Suspense fallback is `null` because both are off-screen until their open flag
// flips, so no fallback UI is ever visible to the user.
const SettingsSheet = React.lazy(() =>
  import("@/components/settings-sheet").then((m) => ({ default: m.SettingsSheet }))
);
const PromptWriter = React.lazy(() =>
  import("@/components/prompt-writer/prompt-writer").then((m) => ({
    default: m.PromptWriter,
  }))
);

export default function App() {
  const theme = useApp((s) => s.theme);
  const selectedBucket = useApp((s) => s.selectedBucket);
  const selectedSessionId = useApp((s) => s.selectedSessionId);
  const setSession = useApp((s) => s.setSession);
  const appendEvent = useApp((s) => s.appendEvent);
  const setSseConnected = useApp((s) => s.setSseConnected);
  const toggleScratchpad = useApp((s) => s.toggleScratchpad);
  const session = useApp((s) => s.session);
  const mergeTranslations = useApp((s) => s.mergeTranslations);
  const settingsOpen = useApp((s) => s.settingsOpen);
  const promptWriterOpen = useApp((s) => s.promptWriter.open);
  const setSettings = useApp((s) => s.setSettings);

  // Apply theme class
  React.useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
  }, [theme]);

  // Load settings on mount so the topbar can show the active provider badge
  // without forcing the SettingsSheet chunk to download. (SettingsSheet used
  // to own this fetch back when it was eagerly imported.)
  React.useEffect(() => {
    api.getSettings().then(setSettings).catch(() => {});
  }, [setSettings]);

  // Load selected session
  React.useEffect(() => {
    if (!selectedBucket || !selectedSessionId) {
      setSession(null);
      return;
    }
    api
      .getSession(selectedBucket, selectedSessionId)
      .then(setSession)
      .catch(console.error);
  }, [selectedBucket, selectedSessionId, setSession]);

  // After a session loads, pre-fetch any cached translations so the
  // per-turn translate toggle is instant for already-translated turns.
  React.useEffect(() => {
    if (!session) return;
    const items: { key: string; text: string }[] = [];
    for (const ev of session.events) {
      if (ev.is_command_artifact) continue;
      if (ev.role === "assistant" && ev.text_blocks.length) {
        items.push({ key: ev.uuid, text: ev.text_blocks.join("\n\n") });
      } else if (ev.role === "user" && ev.user_text) {
        items.push({ key: ev.uuid, text: ev.user_text });
      }
    }
    if (items.length === 0) return;
    api
      .translateLookupBatch(items, "ar")
      .then((r) => {
        if (Object.keys(r.hits).length) mergeTranslations(r.hits);
      })
      .catch(() => {});
  }, [session?.meta.session_id, session?.events.length, mergeTranslations]);

  // SSE
  React.useEffect(() => {
    const off = liveStream.on((e: LiveEvent) => {
      if (e.kind === "hello") {
        setSseConnected(true);
      }
      if (e.kind === "event" && e.event && e.bucket && e.session_id) {
        appendEvent(e.bucket, e.session_id, e.event);
      }
    });
    liveStream.start();
    const heartbeat = setInterval(() => {
      setSseConnected(liveStream.isConnected());
    }, 2000);
    return () => {
      off();
      liveStream.stop();
      clearInterval(heartbeat);
    };
  }, [appendEvent, setSseConnected]);

  // Cmd-J / Ctrl-J toggles scratchpad
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "j") {
        e.preventDefault();
        toggleScratchpad();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggleScratchpad]);

  return (
    <div className="flex h-full flex-col">
      <Topbar />
      <div className="flex min-h-0 flex-1">
        <Sidebar />
        <main className="flex min-w-0 flex-1">
          <Timeline />
          <Scratchpad />
        </main>
      </div>
      <StatusBar />
      <LazyOnce open={settingsOpen}>
        <React.Suspense fallback={null}>
          <SettingsSheet />
        </React.Suspense>
      </LazyOnce>
      <LazyOnce open={promptWriterOpen}>
        <React.Suspense fallback={null}>
          <PromptWriter />
        </React.Suspense>
      </LazyOnce>
      <Toaster position="bottom-right" theme={theme} richColors />
    </div>
  );
}

/** Mounts children once `open` first flips true, then keeps them mounted so the
 *  Sheet's close animation still plays. The chunk download therefore happens
 *  exactly once, when the user first opens the surface. */
function LazyOnce({ open, children }: { open: boolean; children: React.ReactNode }) {
  const [mounted, setMounted] = React.useState(open);
  React.useEffect(() => {
    if (open && !mounted) setMounted(true);
  }, [open, mounted]);
  return mounted ? <>{children}</> : null;
}
