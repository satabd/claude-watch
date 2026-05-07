import React from "react";
import ReactDOM from "react-dom/client";
import "./styles/globals.css";
import App from "./App";
import { useApp } from "@/store/app";
import { makePersistenceWriter, pickPersistable } from "@/lib/persisted-state";

// Persist a small subset of the UI store (selection, sidebar tree, scratchpad,
// prompt-writer mode) to localStorage. Writes are debounced so rapid changes
// (typing in the rough textarea, scrolling, etc.) don't pound storage.
const writePersisted = makePersistenceWriter();
useApp.subscribe((state) => writePersisted(pickPersistable(state)));

// In dev only, expose the store for quick debugging from the browser console.
// `import.meta.env.DEV` is statically replaced by Vite — `false` in `vite build`
// production output so this branch is dead-code-eliminated from the bundle.
if (import.meta.env.DEV && typeof window !== "undefined") {
  (window as any).__app = useApp;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
