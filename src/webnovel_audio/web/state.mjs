// The player's listening rules, free of any DOM so they can be tested with node.
//
// A series has `chapters` (in order, each {id, n, title, dur}) and, per person, a
// position {chapter, t, furthest, furthest_t}: where they are now, and the
// furthest chapter they have reached. Chapters are matched by id, never by
// number, because a site can insert a chapter at the front and renumber.

export const FURTHEST_AFTER_S = 30;   // this long in a later chapter moves "furthest"
export const END_SLACK_S = 8;         // within this of the end counts as finished

export const indexOfChapter = (chapters, id) => chapters.findIndex((c) => c.id === id);

/** How many chapters lie beyond `furthestId` (all of them if there is none). */
export function newCount(chapters, furthestId) {
  return chapters.length - (indexOfChapter(chapters, furthestId) + 1);
}

export function nextChapter(chapters, id) {
  const i = indexOfChapter(chapters, id);
  return i >= 0 && i + 1 < chapters.length ? chapters[i + 1] : null;
}

export function prevChapter(chapters, id) {
  const i = indexOfChapter(chapters, id);
  return i > 0 ? chapters[i - 1] : null;
}

/**
 * The furthest chapter after listening `listened` seconds to `playingId`.
 * It only ever moves forward, and only once you have really listened (or the
 * chapter finished), so scrubbing through a book does not mark it read.
 */
export function advanceFurthest(chapters, furthestId, playingId, listened, finished = false) {
  const p = indexOfChapter(chapters, playingId);
  if (p < 0) return furthestId ?? null;
  const f = indexOfChapter(chapters, furthestId);
  return p > f && (finished || listened >= FURTHEST_AFTER_S) ? playingId : furthestId ?? null;
}

/** Where "Continue" should start, or null if the series has nothing playable. */
export function resumePoint(chapters, pos) {
  if (!chapters.length) return null;
  const cur = chapters.find((c) => c.id === pos?.chapter);
  if (!cur) return { chapter: chapters[0], t: 0 };
  const next = nextChapter(chapters, cur.id);
  if (next && cur.dur && pos.t >= cur.dur - END_SLACK_S) return { chapter: next, t: 0 };
  return { chapter: cur, t: pos.t || 0 };
}

/** Where "Back to furthest" goes, or null when you are already there (or have none). */
export function furthestPoint(chapters, pos) {
  const f = chapters.find((c) => c.id === pos?.furthest);
  if (!f || f.id === pos.chapter) return null;
  return { chapter: f, t: pos.furthest_t || 0 };
}

/** "listened" | "current" | "new": how the chapter list should mark a chapter. */
export function chapterState(chapters, pos, id) {
  const i = indexOfChapter(chapters, id);
  if (id === pos?.chapter) return "current";
  return i <= indexOfChapter(chapters, pos?.furthest) ? "listened" : "new";
}

export function formatTime(seconds) {
  const s = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return (h ? `${h}:` : "") + `${mm}:${String(r).padStart(2, "0")}`;
}

/** A CSS `url("...")` for an address from the page's own data, which could hold a
 *  quote, backslash or newline meant to break out of the declaration. */
export function cssUrl(address) {
  return `url("${String(address).replace(/["\\\n\r()]/g, (ch) => "%" + ch.charCodeAt(0).toString(16).padStart(2, "0"))}")`;
}
