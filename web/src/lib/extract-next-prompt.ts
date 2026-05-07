/** Extract the "NEXT PROMPT FOR CLAUDE CODE" section from a reviewer reply.
 *
 *  The Critical Reviewer / Prompt Coach modes both return a structured reply
 *  with a "NEXT PROMPT FOR CLAUDE CODE" section. We pull it out so the
 *  "Copy next prompt" button gives the user a clean copy-ready prompt
 *  rather than the entire critique.
 *
 *  Returns null when the heading isn't present (e.g. the reviewer formatted
 *  the response differently). The UI falls back to the full reply in that
 *  case so we never silently lose content.
 */

const HEADING_PATTERNS: RegExp[] = [
  // The exact prompt-coach / critical-reviewer instruction asks for this
  // verbatim heading. Match it case-insensitively, with optional leading
  // numbering ("6. NEXT PROMPT…") and trailing punctuation/colon.
  /^\s*(?:\d+[.)]\s*)?(?:#{1,4}\s*)?(?:\*\*\s*)?next\s+prompt\s+for\s+claude(?:\s+code)?\s*(?:\*\*)?\s*[:.\-—]?\s*$/im,
  // Looser fallback: bold or heading variants without "code".
  /^\s*(?:#{1,4}\s*)?(?:\*\*\s*)?next\s+prompt\s*(?:\*\*)?\s*[:.\-—]?\s*$/im,
];

const NEXT_HEADING = /^\s*(?:#{1,4}\s*)?(?:\d+[.)]\s+)?\S+/m;

/** Trim a fenced code block off the result if the reviewer wrapped it
 *  (```text … ``` or ``` … ```). */
function stripFences(text: string): string {
  const fenced = text.match(/^\s*```[a-zA-Z]*\n([\s\S]*?)\n```\s*$/);
  return fenced ? fenced[1] : text;
}

export function extractNextPrompt(reply: string): string | null {
  if (!reply || !reply.trim()) return null;

  for (const pat of HEADING_PATTERNS) {
    const m = reply.match(pat);
    if (!m || m.index === undefined) continue;
    // Slice everything AFTER the heading line.
    const after = reply.slice(m.index + m[0].length).replace(/^\n+/, "");
    if (!after.trim()) continue;
    // Stop at the next heading-like line (bold heading, markdown heading,
    // or numbered section). We accept the whole tail when no further
    // heading appears.
    const tailLines = after.split("\n");
    const stopIdx = tailLines.findIndex((ln, i) => {
      if (i === 0) return false; // first line of the body
      // "## Next Section", "**Section**", "1. Risk:" — common boundaries
      return (
        /^\s*#{1,4}\s+\S/.test(ln) ||
        /^\s*\*\*[A-Za-z][^*]+\*\*\s*$/.test(ln) ||
        /^\s*\d+[.)]\s+\S/.test(ln) ||
        /^---+\s*$/.test(ln)
      );
    });
    const body =
      stopIdx === -1 ? after : tailLines.slice(0, stopIdx).join("\n");
    const cleaned = stripFences(body).trim();
    return cleaned || null;
  }

  return null;
}

/** Best effort: return either the extracted next prompt or the full reply,
 *  so the Copy button always gives the user something useful. */
export function copyTargetForReply(reply: string): string {
  const extracted = extractNextPrompt(reply);
  return (extracted ?? reply).trim();
}
