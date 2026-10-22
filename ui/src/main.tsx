import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
// The design's tokens and classes, before any screen's own styles.
import "./tokens.css";
import "./foundations.css";
// The old screens' stylesheet, before every component's own: loaded
// after them, it won each tie, and drew the back pages' choices as
// boxed buttons.
import "./styles.css";
import App from "./App";
import { DeskCard } from "./DeskCard";
import { QuickBar } from "./QuickBar";

// One bundle, three windows: the app, the quick bar and the desk card
// (tauri.conf.json loads them as index.html#quickbar and #deskcard).
const window_ = window.location.hash.slice(1);
if (window_ === "quickbar" || window_ === "deskcard") {
  document.documentElement.classList.add(`${window_}-window`);
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {window_ === "quickbar" ? <QuickBar /> : window_ === "deskcard" ? <DeskCard /> : <App />}
  </StrictMode>,
);
