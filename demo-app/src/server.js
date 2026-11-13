// Zero-dependency HTTP server: JSON API under /api, static files from public/.
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";
import { createStore, ValidationError } from "./store.js";

const PUBLIC_DIR = fileURLToPath(new URL("../public/", import.meta.url));
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };

function send(res, status, body) {
  const data = body === undefined ? "" : JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(data) });
  res.end(data);
}

async function readJson(req) {
  let raw = "";
  for await (const chunk of req) {
    raw += chunk;
    if (raw.length > 1e6) throw new ValidationError("body too large");
  }
  if (!raw) return {};
  try {
    return JSON.parse(raw);
  } catch {
    throw new ValidationError("invalid JSON");
  }
}

export function createApp(store = createStore()) {
  return async function handle(req, res) {
    const url = new URL(req.url, "http://localhost");
    const path = url.pathname;
    try {
      if (path === "/healthz") return send(res, 200, { ok: true });

      if (path === "/api/todos" && req.method === "GET") return send(res, 200, store.list());
      if (path === "/api/todos" && req.method === "POST") {
        const body = await readJson(req);
        return send(res, 201, store.add(body.title));
      }
      if (path === "/api/stats" && req.method === "GET") return send(res, 200, store.stats());

      const m = path.match(/^\/api\/todos\/(\d+)$/);
      if (m) {
        if (req.method === "GET") {
          const t = store.get(m[1]);
          return t ? send(res, 200, t) : send(res, 404, { error: "not found" });
        }
        if (req.method === "PATCH") {
          const t = store.update(m[1], await readJson(req));
          return t ? send(res, 200, t) : send(res, 404, { error: "not found" });
        }
        if (req.method === "DELETE") {
          return store.remove(m[1]) ? send(res, 204) : send(res, 404, { error: "not found" });
        }
      }
      if (path.startsWith("/api/")) return send(res, 404, { error: "not found" });

      if (req.method !== "GET") return send(res, 405, { error: "method not allowed" });
      const rel = normalize(path === "/" ? "index.html" : path.slice(1));
      if (rel.startsWith("..")) return send(res, 404, { error: "not found" });
      const file = await readFile(join(PUBLIC_DIR, rel)).catch(() => null);
      if (!file) return send(res, 404, { error: "not found" });
      res.writeHead(200, { "content-type": TYPES[extname(rel)] ?? "application/octet-stream" });
      res.end(file);
    } catch (err) {
      if (err instanceof ValidationError) return send(res, 400, { error: err.message });
      console.error(err);
      send(res, 500, { error: "internal error" });
    }
  };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const port = Number(process.env.PORT ?? 3000);
  createServer(createApp()).listen(port, "0.0.0.0", () => console.log(`demo-todo listening on :${port}`));
}
