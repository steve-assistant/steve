/* The specimen sheet (MarkSheet.tsx). Built only by `vite build --mode
   specimen` (vite.config.ts), for the screen tests (tests/unit/ui/
   test_mark_screen.py) and for looking at; the app never ships it. */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { MarkSheet } from "./MarkSheet";
import "./tokens.css";
import "./foundations.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <MarkSheet />
  </StrictMode>,
);
