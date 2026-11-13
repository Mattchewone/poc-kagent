// Relative URLs throughout so the app also works behind a path prefix (preview proxy).
const list = document.getElementById("list");
const left = document.getElementById("left");
const form = document.getElementById("new-todo");
const input = document.getElementById("title");
const error = document.getElementById("error");

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "content-type": "application/json" }, ...options });
  if (!res.ok && res.status !== 204) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error ?? `HTTP ${res.status}`);
  }
  return res.status === 204 ? null : res.json();
}

function showError(message) {
  error.textContent = message;
  error.hidden = !message;
}

async function render() {
  const [todos, stats] = await Promise.all([api("api/todos"), api("api/stats")]);
  list.replaceChildren(
    ...todos.map((t) => {
      const li = document.createElement("li");
      li.className = t.done ? "done" : "";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = t.done;
      box.addEventListener("change", () => act(() => api(`api/todos/${t.id}`, { method: "PATCH", body: JSON.stringify({ done: box.checked }) })));
      const label = document.createElement("span");
      label.textContent = t.title;
      const del = document.createElement("button");
      del.textContent = "✕";
      del.title = "Delete";
      del.addEventListener("click", () => act(() => api(`api/todos/${t.id}`, { method: "DELETE" })));
      li.append(box, label, del);
      return li;
    }),
  );
  left.textContent = `${stats.left} item${stats.left === 1 ? "" : "s"} left`;
}

async function act(fn) {
  try {
    showError("");
    await fn();
  } catch (e) {
    showError(e.message);
  }
  await render();
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  act(async () => {
    await api("api/todos", { method: "POST", body: JSON.stringify({ title: input.value }) });
    input.value = "";
  });
});

render().catch((e) => showError(e.message));
