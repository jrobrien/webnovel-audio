import test from "node:test";
import assert from "node:assert/strict";
import {
  advanceFurthest, chapterState, cssUrl, formatTime, furthestPoint, newCount,
  nextChapter, prevChapter, resumePoint,
} from "../../src/webnovel_audio/web/state.mjs";

const ch = (...ids) => ids.map((id, i) => ({ id, n: i + 1, title: `T${id}`, dur: 600 }));
const book = ch("a", "b", "c", "d", "e");

test("new chapters are those beyond the furthest, and all of them with no history", () => {
  assert.equal(newCount(book, "c"), 2);
  assert.equal(newCount(book, "e"), 0);
  assert.equal(newCount(book, null), 5);
  assert.equal(newCount(book, "gone"), 5);                 // a stale id is "no history"
  assert.equal(newCount([...book, ...ch("f")], "e"), 1);   // a refresh found one more
});

test("next and previous follow the list, not the numbers", () => {
  assert.equal(nextChapter(book, "b").id, "c");
  assert.equal(nextChapter(book, "e"), null);
  assert.equal(prevChapter(book, "a"), null);
  assert.equal(prevChapter(book, "c").id, "b");
  const renumbered = [{ id: "z", n: 1 }, ...book];          // a chapter inserted at the front
  assert.equal(nextChapter(renumbered, "a").id, "b");
});

test("furthest moves forward only after 30s of listening or a finish", () => {
  assert.equal(advanceFurthest(book, "b", "c", 10), "b");
  assert.equal(advanceFurthest(book, "b", "c", 30), "c");
  assert.equal(advanceFurthest(book, "b", "c", 2, true), "c");
  assert.equal(advanceFurthest(book, "c", "a", 9999, true), "c");   // older chapter: never back
  assert.equal(advanceFurthest(book, "c", "c", 9999), "c");
  assert.equal(advanceFurthest(book, null, "a", 31), "a");          // first listen
  assert.equal(advanceFurthest(book, "c", "gone", 99), "c");
});

test("continue resumes the current spot, or the next chapter if it had finished", () => {
  assert.deepEqual(resumePoint(book, { chapter: "c", t: 120 }), { chapter: book[2], t: 120 });
  assert.deepEqual(resumePoint(book, { chapter: "c", t: 595 }), { chapter: book[3], t: 0 });
  assert.deepEqual(resumePoint(book, { chapter: "e", t: 599 }), { chapter: book[4], t: 599 });
  assert.deepEqual(resumePoint(book, null), { chapter: book[0], t: 0 });
  assert.deepEqual(resumePoint(book, { chapter: "gone", t: 5 }), { chapter: book[0], t: 0 });
  assert.equal(resumePoint([], null), null);
});

test("back-to-furthest returns to where you were in it, and only when elsewhere", () => {
  const pos = { chapter: "b", t: 5, furthest: "d", furthest_t: 321 };
  assert.deepEqual(furthestPoint(book, pos), { chapter: book[3], t: 321 });
  assert.equal(furthestPoint(book, { ...pos, chapter: "d" }), null);
  assert.equal(furthestPoint(book, { chapter: "b", t: 5 }), null);
});

test("the chapter list marks listened, current and new", () => {
  const pos = { chapter: "b", furthest: "d" };
  const marks = book.map((c) => chapterState(book, pos, c.id));
  assert.deepEqual(marks, ["listened", "current", "listened", "listened", "new"]);
  assert.equal(chapterState(book, null, "a"), "new");
});

test("times read as m:ss and h:mm:ss", () => {
  assert.equal(formatTime(0), "0:00");
  assert.equal(formatTime(65.9), "1:05");
  assert.equal(formatTime(3725), "1:02:05");
  assert.equal(formatTime(NaN), "0:00");
});

test("a hostile cover address cannot break out of the css url()", () => {
  assert.equal(cssUrl("/cover/a.jpg"), 'url("/cover/a.jpg")');
  const out = cssUrl('x"); background: url("//evil/x');
  assert.ok(!out.slice(5, -2).includes('"') && !out.includes("); "));
  assert.ok(!cssUrl("a\\b\nc").slice(5, -2).match(/[\\\n]/));
});
