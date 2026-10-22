/* Photos given to Steve in the app (agent/worker/app_photos.py): chosen
   with the paperclip, pasted, or dropped on the page. The app had no way in
   for a photo, though Signal and Telegram took them.

   The limits are the phones' (channels/photos.py), checked here so a
   refusal comes as the photo is added, not after the message; the worker
   checks again, by the bytes. Each photo goes up on its own request before
   the message: the socket through the supervisor takes a megabyte a
   message, and four photos are up to 80 MB. */

import { useEffect, useState } from "react";
import { request } from "./api";

export const MOST_PHOTOS = 4;
export const MOST_BYTES = 20_000_000;
export const FORMATS = ["image/jpeg", "image/png", "image/webp", "image/gif"];

/** A photo chosen for the next message, kept by an id of its own: two
 *  pastes of one screenshot are two photos. */
export interface Picked {
  id: number;
  file: File;
}

/** Of `files`, those that may join the `have` already chosen, and a line
 *  saying which were left out and why. */
export function choose(have: number, files: File[]): { add: File[]; left: string | null } {
  const add: File[] = [];
  const out: string[] = [];
  for (const file of files) {
    const name = file.name || "a file";
    if (!FORMATS.includes(file.type)) out.push(`${name}, not a JPEG, PNG, WebP or GIF`);
    else if (file.size > MOST_BYTES) out.push(`${name}, over ${MOST_BYTES / 1_000_000} MB`);
    else if (have + add.length >= MOST_PHOTOS) {
      out.push(`${name}, past the ${MOST_PHOTOS} Steve looks at in a message`);
    } else add.push(file);
  }
  return { add, left: out.length ? `Not added: ${out.join("; ")}.` : null };
}

/** The photo up to the worker: the name the message gives it, or why not. */
export async function upload(file: File): Promise<{ name?: string; error?: string }> {
  try {
    const response = await request("/control/photo", {
      method: "POST", body: file, headers: { "Content-Type": file.type },
    });
    const said = (await response.json()) as { ok: boolean; name?: string; error?: string };
    return said.ok && said.name ? { name: said.name } : { error: said.error ?? "That photo wasn't taken." };
  } catch (err) {
    return { error: `That photo wasn't taken: ${String(err)}` };
  }
}

/** A file as a URL an <img> can show, for as long as it is shown. */
export function useFileURL(file: File): string | null {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    const made = URL.createObjectURL(file);
    setUrl(made);
    return () => URL.revokeObjectURL(made);
  }, [file]);
  return url;
}

/** The `n`th photo of the user's message `message`, fetched with the
 *  backend's token, which an <img> can't send; "gone" once its day is over
 *  and the file with it. */
export function useSentPhoto(message: number, n: number): string | "gone" | null {
  const [url, setUrl] = useState<string | "gone" | null>(null);
  useEffect(() => {
    let made: string | null = null;
    let cancelled = false;
    void (async () => {
      try {
        const response = await request(`/control/photo?message=${message}&n=${n}`);
        if (!response.ok) {
          if (!cancelled && response.status === 404) setUrl("gone");
          return;
        }
        const blob = await response.blob();
        if (cancelled) return;
        made = URL.createObjectURL(blob);
        setUrl(made);
      } catch {
        // The backend away: an empty frame, until the page is opened again.
      }
    })();
    return () => {
      cancelled = true;
      if (made) URL.revokeObjectURL(made);
    };
  }, [message, n]);
  return url;
}
