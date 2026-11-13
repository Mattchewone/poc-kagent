// In-memory todo store. Each server process starts with the same seed data.

const SEED = [
  { title: "Try the agent pipeline", done: true },
  { title: "Ask for a feature in chat", done: false },
  { title: "Review the preview and approve", done: false },
];

export function createStore(seed = SEED) {
  let nextId = 1;
  const todos = new Map();

  function add(title) {
    const clean = String(title ?? "").trim();
    if (!clean) throw new ValidationError("title is required");
    if (clean.length > 200) throw new ValidationError("title must be 200 characters or fewer");
    const todo = { id: nextId++, title: clean, done: false, createdAt: new Date().toISOString() };
    todos.set(todo.id, todo);
    return todo;
  }

  function list() {
    return [...todos.values()];
  }

  function get(id) {
    return todos.get(Number(id));
  }

  function update(id, patch) {
    const todo = get(id);
    if (!todo) return undefined;
    if ("title" in patch) {
      const clean = String(patch.title ?? "").trim();
      if (!clean) throw new ValidationError("title is required");
      todo.title = clean;
    }
    if ("done" in patch) todo.done = Boolean(patch.done);
    return todo;
  }

  function remove(id) {
    return todos.delete(Number(id));
  }

  function stats() {
    const all = list();
    // BUG (planted for the demo): "left" should count todos that are not done.
    const left = all.filter((t) => t.done).length;
    return { total: all.length, left };
  }

  for (const s of seed) {
    const t = add(s.title);
    t.done = Boolean(s.done);
  }

  return { add, list, get, update, remove, stats };
}

export class ValidationError extends Error {}
