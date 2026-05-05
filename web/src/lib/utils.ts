import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatTime(ts: string | number | null | undefined): string {
  if (!ts) return "";
  const d = typeof ts === "string" ? new Date(ts) : new Date(ts);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function formatRelative(ms: number | null | undefined): string {
  if (!ms) return "";
  const diff = Date.now() - ms;
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  return `${day}d ago`;
}

export function shortPath(p: string | null | undefined, max = 38): string {
  if (!p) return "";
  if (p.length <= max) return p;
  // keep last part
  const parts = p.split(/[\\/]/).filter(Boolean);
  if (parts.length <= 2) return "…" + p.slice(-(max - 1));
  const tail = parts.slice(-2).join("/");
  return `…/${tail}`.slice(0, max);
}

export function formatBytes(b: number): string {
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${(b / 1024 / 1024).toFixed(2)} MB`;
}
