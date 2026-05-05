import * as React from "react";
import { Languages, Sparkles, MessageSquare, Copy, FileText, Code2, ListTree, Wand2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { useApp } from "@/store/app";
import { toast } from "sonner";

const TOTALLY_NEW_MARKER_XYZQ123 = "v3-rewrite";

interface Sel {
  text: string;
  rect: DOMRect;
  turnId: string | null;
}

export function SelectionToolbar({ containerRef }: { containerRef: React.RefObject<HTMLElement> }) {
  // Read store atoms separately — never return computed objects/functions from selectors.
  const session = useApp((s) => s.session);
  const prependScratchpad = useApp((s) => s.prependScratchpad);
  const scratchpadOpen = useApp((s) => s.scratchpadOpen);
  const toggleScratchpad = useApp((s) => s.toggleScratchpad);
  const openPromptWriter = useApp((s) => s.openPromptWriter);

  const [sel, setSel] = React.useState<Sel | null>(null);
  const [busy, setBusy] = React.useState<string | null>(null);

  const ensureScratchpadOpen = () => {
    if (!scratchpadOpen) toggleScratchpad();
  };

  React.useEffect(() => {
    const onSelectionChange = () => {
      const s = window.getSelection();
      if (!s || s.isCollapsed || s.rangeCount === 0) {
        setSel(null);
        return;
      }
      const range = s.getRangeAt(0);
      const node = range.commonAncestorContainer;
      const el = (node.nodeType === 1 ? (node as Element) : node.parentElement) as HTMLElement | null;
      const root = containerRef.current;
      if (!el || !root || !root.contains(el)) {
        setSel(null);
        return;
      }
      const text = s.toString().trim();
      if (text.length < 2) {
        setSel(null);
        return;
      }
      const rect = range.getBoundingClientRect();
      const turnEl = el.closest<HTMLElement>("[data-turn-id]");
      setSel({ text, rect, turnId: turnEl?.dataset.turnId ?? null });
    };
    document.addEventListener("selectionchange", onSelectionChange);
    return () => document.removeEventListener("selectionchange", onSelectionChange);
  }, [containerRef]);

  if (!sel) return null;
  void TOTALLY_NEW_MARKER_XYZQ123;

  const { rect, text, turnId } = sel;
  const top = Math.max(8, rect.top - 44);
  const left = Math.min(window.innerWidth - 360, Math.max(8, rect.left + rect.width / 2 - 180));

  const runAction = async (
    action: "clarify" | "summarize" | "explain" | "glossary" | "comment",
    note?: string
  ) => {
    if (busy) return;
    setBusy(action);
    try {
      const item = await api.scratchpadRun({
        action,
        text,
        note,
        source_turn: turnId ?? undefined,
        session_id: session?.meta.session_id,
      });
      prependScratchpad(item);
      ensureScratchpadOpen();
      toast.success(action === "comment" ? "Comment saved" : `${action} added`);
    } catch (e: any) {
      toast.error(e?.message ?? "Action failed");
    } finally {
      setBusy(null);
      setSel(null);
      window.getSelection()?.removeAllRanges();
    }
  };

  const translate = async () => {
    if (busy) return;
    setBusy("translate");
    try {
      const r = await api.translate(text, "ar");
      const item = await api.scratchpadRun({
        action: "comment",
        text,
        note: r.translation,
        source_turn: turnId ?? undefined,
        session_id: session?.meta.session_id,
      });
      prependScratchpad({ ...item, action: "translate", model: r.model });
      ensureScratchpadOpen();
      toast.success(r.cached ? "Translation (cached)" : "Translated");
    } catch (e: any) {
      toast.error(e?.message ?? "Translation failed");
    } finally {
      setBusy(null);
      setSel(null);
      window.getSelection()?.removeAllRanges();
    }
  };

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      toast.success("Copied");
    } catch {}
    setSel(null);
  };

  return (
    <div
      className="pop-in fixed z-40 flex items-center gap-0.5 rounded-md border border-border bg-popover p-1 shadow-lg"
      style={{ top, left }}
      onMouseDown={(e) => e.preventDefault()}
    >
      <Btn icon={<Languages />} label="Translate" busy={busy === "translate"} onClick={translate} />
      <Btn icon={<Sparkles />} label="Clarify" busy={busy === "clarify"} onClick={() => runAction("clarify")} />
      <Btn icon={<FileText />} label="Summarize" busy={busy === "summarize"} onClick={() => runAction("summarize")} />
      <Btn icon={<Code2 />} label="Explain code" busy={busy === "explain"} onClick={() => runAction("explain")} />
      <Btn icon={<ListTree />} label="Glossary" busy={busy === "glossary"} onClick={() => runAction("glossary")} />
      <Btn
        icon={<MessageSquare />}
        label="Comment"
        busy={busy === "comment"}
        onClick={() => {
          const note = window.prompt("Your comment on this passage:");
          if (note != null && note.trim()) runAction("comment", note.trim());
        }}
      />
      <div className="mx-1 h-5 w-px bg-border" />
      <Btn
        icon={<Wand2 />}
        label="Turn into prompt"
        busy={false}
        onClick={() => {
          openPromptWriter({
            sourceEventUuid: turnId ?? null,
            selectedText: text,
            contextMode: "selected_plus_nearby",
          });
          setSel(null);
          window.getSelection()?.removeAllRanges();
        }}
      />
      <Btn icon={<Copy />} label="Copy" busy={false} onClick={copy} />
    </div>
  );
}

function Btn({
  icon,
  label,
  busy,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  busy: boolean;
  onClick: () => void;
}) {
  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={onClick}
      title={label}
      disabled={busy}
      className={busy ? "animate-pulse" : ""}
    >
      {icon}
    </Button>
  );
}
