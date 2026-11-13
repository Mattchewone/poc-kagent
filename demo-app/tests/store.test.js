import { test } from "node:test";
import assert from "node:assert/strict";
import { createStore, ValidationError } from "../src/store.js";

test("add trims titles and assigns ids", () => {
  const s = createStore([]);
  const a = s.add("  buy milk  ");
  const b = s.add("walk dog");
  assert.equal(a.title, "buy milk");
  assert.equal(a.done, false);
  assert.notEqual(a.id, b.id);
  assert.equal(s.list().length, 2);
});

test("add rejects empty titles", () => {
  const s = createStore([]);
  assert.throws(() => s.add("   "), ValidationError);
  assert.throws(() => s.add(undefined), ValidationError);
});

test("update toggles done and renames", () => {
  const s = createStore([]);
  const t = s.add("a");
  s.update(t.id, { done: true, title: "b" });
  assert.deepEqual({ done: s.get(t.id).done, title: s.get(t.id).title }, { done: true, title: "b" });
  assert.equal(s.update(999, { done: true }), undefined);
});

test("remove deletes by id", () => {
  const s = createStore([]);
  const t = s.add("a");
  assert.equal(s.remove(t.id), true);
  assert.equal(s.remove(t.id), false);
  assert.equal(s.list().length, 0);
});

test("stats reports total", () => {
  const s = createStore([]);
  s.add("a");
  s.add("b");
  assert.equal(s.stats().total, 2);
});
