import { describe, expect, it, vi, afterEach } from "vitest";
import { copyText } from "./clipboard";

/** The whole point of the helper is the insecure-origin case: opening the app
 *  from another machine leaves `navigator.clipboard` undefined. These tests
 *  run in vitest's `node` environment, so the DOM the fallback needs is
 *  stubbed by hand rather than pulling in jsdom for one file. */
function fakeDom(execCommand: () => boolean) {
  const appended: unknown[] = [];
  const removed: unknown[] = [];
  const document = {
    createElement: () => ({
      value: "",
      style: {} as Record<string, string>,
      setAttribute: () => {},
      select: () => {},
      setSelectionRange: () => {},
    }),
    body: {
      appendChild: (n: unknown) => appended.push(n),
      removeChild: (n: unknown) => removed.push(n),
    },
    getSelection: () => null,
    execCommand,
  };
  vi.stubGlobal("document", document);
  return { appended, removed };
}

afterEach(() => vi.unstubAllGlobals());

describe("copyText", () => {
  it("uses the Clipboard API when it exists", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    await copyText("hello");
    expect(writeText).toHaveBeenCalledWith("hello");
  });

  it("falls back to execCommand when navigator.clipboard is missing", async () => {
    vi.stubGlobal("navigator", {});
    const exec = vi.fn().mockReturnValue(true);
    fakeDom(exec);
    await expect(copyText("hello")).resolves.toBeUndefined();
    expect(exec).toHaveBeenCalledWith("copy");
  });

  it("falls back when the Clipboard API rejects (denied / unfocused)", async () => {
    vi.stubGlobal("navigator", {
      clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });
    const exec = vi.fn().mockReturnValue(true);
    fakeDom(exec);
    await copyText("hello");
    expect(exec).toHaveBeenCalledWith("copy");
  });

  it("rejects when both paths fail, so callers can toast", async () => {
    vi.stubGlobal("navigator", {});
    fakeDom(() => false);
    await expect(copyText("hello")).rejects.toThrow(/copy manually/i);
  });

  it("removes the scratch textarea it appended", async () => {
    vi.stubGlobal("navigator", {});
    const { appended, removed } = fakeDom(() => true);
    await copyText("hello");
    expect(appended).toHaveLength(1);
    expect(removed).toEqual(appended);
  });
});
