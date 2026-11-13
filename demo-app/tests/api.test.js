import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { createApp } from "../src/server.js";
import { createStore } from "../src/store.js";

let server, base;

before(async () => {
  server = createServer(createApp(createStore([])));
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  base = `http://127.0.0.1:${server.address().port}`;
});
after(() => server.close());

const call = (path, method = "GET", body) =>
  fetch(base + path, { method, headers: { "content-type": "application/json" }, body: body && JSON.stringify(body) });

test("create, list, toggle, delete", async () => {
  const created = await call("/api/todos", "POST", { title: "write tests" });
  assert.equal(created.status, 201);
  const todo = await created.json();

  const listed = await (await call("/api/todos")).json();
  assert.ok(listed.some((t) => t.id === todo.id));

  const toggled = await (await call(`/api/todos/${todo.id}`, "PATCH", { done: true })).json();
  assert.equal(toggled.done, true);

  assert.equal((await call(`/api/todos/${todo.id}`, "DELETE")).status, 204);
  assert.equal((await call(`/api/todos/${todo.id}`)).status, 404);
});

test("validation errors are 400", async () => {
  const res = await call("/api/todos", "POST", { title: "" });
  assert.equal(res.status, 400);
  assert.match((await res.json()).error, /required/);
});

test("serves the UI", async () => {
  const res = await call("/");
  assert.equal(res.status, 200);
  assert.match(await res.text(), /<h1>Todos<\/h1>/);
});
