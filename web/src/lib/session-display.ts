import type { SessionMeta } from "@/lib/api";

/** Pick the most readable description for a session in lists/sidebars. */
export function sessionDisplayName(s: SessionMeta, max = 60): string {
  const raw = s.ai_title?.trim() || s.first_prompt?.trim() || "";
  if (!raw) return `Session ${s.session_id.slice(0, 8)}`;
  return truncate(raw, max);
}

function truncate(text: string, max: number): string {
  // Collapse newlines so the list stays single-line
  const flat = text.replace(/\s+/g, " ").trim();
  if (flat.length <= max) return flat;
  return flat.slice(0, max - 1).trimEnd() + "…";
}
