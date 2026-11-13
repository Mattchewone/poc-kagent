#!/usr/bin/env node
// Verify supervisor. Runs in a fresh OpenShell sandbox with no network and no credentials, one per
// revision; the worker deletes the sandbox once it has the verdict. (PR previews run on kargo-nonprod.)
//
//   POST /__poc/verify  {base_tar_b64, diff}  -> apply diff to a clean copy of base, restore base
//                                                tests, run `node --test` twice, return the verdict
//   GET  /__poc/status                         -> {state, verify}
//   GET  /readyz                               -> 200 (the worker's readiness probe)
//
// Two test runs, both must pass:
//   1. the agent's tree as written, so the tests it added count;
//   2. a copy with every base file under tests/ restored, so it cannot weaken existing tests.
// tests_touched flags removed/changed lines in test files that exist on base (informational).
import { createServer } from "node:http";
import { spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { join, relative } from "node:path";

const PORT = Number(process.argv[2] ?? process.env.PORT ?? 8080);
const WORK = process.env.WORK_DIR ?? "/work";
const BASE = join(WORK, "base");
const APP = join(WORK, "app");
const GRADED = join(WORK, "graded");

let state = "idle"; // idle | verifying | passed | failed
let lastVerify = null;

const log = (...a) => console.error("[supervisor]", ...a);

function run(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, { encoding: "utf8", timeout: 180_000, ...opts });
  return { code: r.status ?? -1, out: ((r.stdout ?? "") + (r.stderr ?? "")).slice(-6000) };
}

function walk(dir) {
  if (!existsSync(dir)) return [];
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}

function touchedPaths(diff) {
  return [...diff.matchAll(/^\+\+\+ b\/(.+)$/gm), ...diff.matchAll(/^--- a\/(.+)$/gm)].map((m) => m[1].trim());
}

// Base test files the diff removes or changes lines in (pure additions don't count).
function weakenedTests(diff) {
  const out = new Set();
  let file = null;
  for (const line of diff.split("\n")) {
    if (line.startsWith("--- ")) file = line.startsWith("--- a/") ? line.slice(6).trim() : null;
    else if (file?.startsWith("tests/") && line.startsWith("-") && !line.startsWith("---")) out.add(file);
  }
  return [...out];
}

const counts = (out) => ({
  pass: Number(out.match(/^ℹ pass (\d+)/m)?.[1] ?? 0),
  fail: Number(out.match(/^ℹ fail (\d+)/m)?.[1] ?? 0),
});

async function verify({ base_tar_b64, diff = "" }) {
  if (!base_tar_b64) throw new Error("base_tar_b64 is required");
  state = "verifying";
  // Empty WORK rather than removing it: the sandbox user owns its contents, not the directory's parent.
  mkdirSync(WORK, { recursive: true });
  for (const n of readdirSync(WORK)) rmSync(join(WORK, n), { recursive: true, force: true });
  mkdirSync(BASE, { recursive: true });
  const tar = join(WORK, "base.tar");
  writeFileSync(tar, Buffer.from(base_tar_b64, "base64"));
  let r = run("tar", ["-xf", tar, "-C", BASE]);
  if (r.code !== 0) throw new Error(`untar failed: ${r.out}`);
  cpSync(BASE, APP, { recursive: true });

  const touched = [...new Set(touchedPaths(diff))].filter((p) => p !== "/dev/null");
  const weakened = weakenedTests(diff);
  const result = { passed: false, output: "", tests_touched: weakened.length > 0, weakened, touched };

  if (diff.trim()) {
    writeFileSync(join(WORK, "change.patch"), diff);
    r = run("git", ["apply", "--whitespace=nowarn", join(WORK, "change.patch")], { cwd: APP });
    if (r.code !== 0) {
      Object.assign(result, { output: `git apply failed:\n${r.out}` });
      state = "failed";
      return (lastVerify = result);
    }
  }
  const env = { ...process.env, NODE_ENV: "test" };
  const agent = run("node", ["--test", "--test-reporter=spec"], { cwd: APP, env });
  cpSync(APP, GRADED, { recursive: true });
  for (const f of walk(join(BASE, "tests"))) cpSync(f, join(GRADED, relative(BASE, f)));
  const graded = run("node", ["--test", "--test-reporter=spec"], { cwd: GRADED, env });
  Object.assign(result, {
    passed: agent.code === 0 && graded.code === 0,
    agent_tests: { ...counts(agent.out), ok: agent.code === 0 },
    base_tests: { ...counts(graded.out), ok: graded.code === 0 },
    output: `# agent's tests\n${agent.out.slice(-3000)}\n# base tests restored\n${graded.out.slice(-3000)}`,
  });
  state = result.passed ? "passed" : "failed";
  return (lastVerify = result);
}

function json(res, code, body) {
  const data = JSON.stringify(body);
  res.writeHead(code, { "content-type": "application/json", "content-length": Buffer.byteLength(data) });
  res.end(data);
}

createServer(async (req, res) => {
  const path = req.url.split("?")[0];
  try {
    if (path === "/readyz") return json(res, 200, { ok: true });
    if (path === "/__poc/status") return json(res, 200, { state, verify: lastVerify });
    if (path === "/__poc/verify" && req.method === "POST") {
      let raw = "";
      for await (const c of req) raw += c;
      return json(res, 200, await verify(JSON.parse(raw || "{}")));
    }
    return json(res, 404, { error: "not found" });
  } catch (e) {
    log("error", e);
    state = state === "verifying" ? "failed" : state;
    return json(res, 500, { error: String(e?.message ?? e) });
  }
}).listen(PORT, "0.0.0.0", () => log(`listening on :${PORT}`));
