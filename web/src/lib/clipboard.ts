/** Clipboard write that also works off-localhost.
 *
 *  `navigator.clipboard` only exists in a *secure context*. Claude Watch is
 *  served over plain http, so it is present when you open the app on the host
 *  (http://localhost is treated as secure) and **undefined** the moment you
 *  open the very same server from another machine (http://192.168.x.x). Every
 *  copy button then threw `Cannot read properties of undefined`.
 *
 *  The fallback is the pre-Clipboard-API trick: a throwaway off-screen
 *  textarea + `document.execCommand("copy")`. Deprecated, but it is the only
 *  thing browsers still honour on insecure origins, and it works in Chrome,
 *  Safari and Firefox as long as it runs inside a user-gesture handler —
 *  which every caller here does.
 *
 *  Rejects (rather than returning false) when both paths fail, so the
 *  existing `try { … } catch { toast.error(…) }` call sites keep working.
 */
export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // Permission denied / document not focused — try the legacy path.
    }
  }
  if (!legacyCopy(text)) {
    throw new Error("Copy blocked by the browser — select the text and copy manually");
  }
}

function legacyCopy(text: string): boolean {
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    // Keep it in the layout (display:none isn't selectable) but invisible,
    // and readOnly so mobile Safari doesn't pop the keyboard.
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "0";
    ta.style.left = "0";
    ta.style.width = "1px";
    ta.style.height = "1px";
    ta.style.padding = "0";
    ta.style.border = "none";
    ta.style.outline = "none";
    ta.style.boxShadow = "none";
    ta.style.background = "transparent";
    ta.style.opacity = "0";
    document.body.appendChild(ta);

    const selection = document.getSelection();
    const previous = selection && selection.rangeCount > 0 ? selection.getRangeAt(0) : null;

    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    const ok = document.execCommand("copy");

    document.body.removeChild(ta);
    if (previous && selection) {
      selection.removeAllRanges();
      selection.addRange(previous);
    }
    return ok;
  } catch {
    return false;
  }
}
