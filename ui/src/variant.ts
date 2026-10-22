/* The variant an id picks, for what is drawn by hand: a mark's variant
   (HandMark.tsx), a note's tilt (needs.ts). The same id always picks the
   same, so nothing changes at a redraw. Not in marks.ts, which
   `python -m devtools.marks` writes whole. */

/** FNV-1a, so close ids still spread. */
export function variantOf(id: string, count: number): number {
  let hash = 0x811c9dc5;
  for (let i = 0; i < id.length; i++) {
    hash ^= id.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0) % count;
}

/** A number from lo to hi that an id picks, for a small difference of a
 *  drawn thing's own: a check's size, place and tilt (Form.tsx). */
export function between(id: string, lo: number, hi: number): number {
  return lo + (variantOf(id, 1001) / 1000) * (hi - lo);
}
