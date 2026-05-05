import * as React from "react";
import { Toaster } from "sonner";
import { Topbar } from "@/components/topbar";
import { Sidebar } from "@/components/sidebar";
import { Timeline } from "@/components/timeline/timeline";
import { Scratchpad } from "@/components/scratchpad";
import { SettingsSheet } from "@/components/settings-sheet";
import { PromptWriter } from "@/components/prompt-writer/prompt-writer";
import { StatusBar } from "@/components/status-bar";
import { useApp } from "@/store/app";
import { api } from "@/lib/api";
import { liveStream, type LiveEvent } from "@/lib/sse";

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

  // Apply theme class
  React.useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
  }, [theme]);

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
      <SettingsSheet />
      <PromptWriter />
      <Toaster position="bottom-right" theme={theme} richColors />
    </div>
  );
}
