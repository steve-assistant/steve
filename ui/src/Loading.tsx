import { useEffect, useState } from "react";

/** How long a page waits on its first answer before saying it has none. */
const PATIENCE_MS = 10000;

/**
 * "Loading…" while a page waits for its first answer, and then, if none
 * came, that Steve's backend has not answered. A page's reads fail quietly
 * and are tried again, so a backend that never answered (stopped, or
 * restarting) left the page saying "Loading…" for ever, as though an
 * answer were on its way.
 */
export function Loading({ className = "muted small" }: { className?: string }) {
  const [late, setLate] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(() => setLate(true), PATIENCE_MS);
    return () => window.clearTimeout(timer);
  }, []);
  return (
    <p className={className}>
      {late ? "Steve's backend has not answered yet. This page keeps asking." : "Loading…"}
    </p>
  );
}
