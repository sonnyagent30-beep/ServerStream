// Regression guard for the dashboard's api() helper.
//
// The bug: `!String(o.headers["Content-Type"] && "")` is inverted. With the
// header absent, `undefined && ""` is `undefined`, and `String(undefined)` is
// the non-empty string "undefined" - truthy - so the guard never fired and the
// Content-Type was never set. fetch() then sent text/plain and every write
// failed server-side with "Input should be a valid dictionary".
//
// This extracts the *shipped* api() function out of index.html and drives it
// with a stubbed fetch, so the real code is what is under test.
import { readFileSync } from "node:fs";
import vm from "node:vm";

const html = readFileSync(new URL("../manager/static/index.html", import.meta.url), "utf8");
const match = html.match(/async function api\(path, opts\)\{[\s\S]*?\n\}/);
if (!match) { console.error("  FAIL  could not locate api() in index.html"); process.exit(1); }

const calls = [];
const ctx = {
  TOKEN: "tok-123",
  location: { href: "" },
  Object, String, JSON,
  fetch: (path, o) => {
    calls.push({ path, headers: { ...o.headers }, body: o.body, method: o.method || "GET" });
    return { status: 200, ok: true, json: async () => ({}), text: async () => "" };
  },
};
vm.createContext(ctx);
vm.runInContext(`${match[0]}\nglobalThis.__api = api;`, ctx);

const api = ctx.__api;
let fails = 0;
const check = (name, cond, extra) => {
  console.log(`  ${cond ? "PASS" : "FAIL"}  ${name}${!cond && extra ? "  -> " + extra : ""}`);
  if (!cond) fails++;
};
const ct = (i) => {
  const h = calls[i].headers;
  const k = Object.keys(h).find(x => x.toLowerCase() === "content-type");
  return k ? h[k] : undefined;
};

console.log("\n== a JSON body is declared as JSON (the reported bug) ==");
await api("/api/platforms/1", { method: "PATCH", body: JSON.stringify({ enabled: true }) });
check("PATCH body sets Content-Type: application/json", ct(0) === "application/json", ct(0));
check("body is sent unchanged", calls[0].body === '{"enabled":true}', calls[0].body);

await api("/api/platforms", { method: "POST", body: JSON.stringify({ name: "x" }) });
check("POST body sets Content-Type: application/json", ct(1) === "application/json", ct(1));

await api("/api/standby", { method: "POST", body: JSON.stringify({ data_url: "data:x" }) });
check("standby upload sets Content-Type: application/json", ct(2) === "application/json", ct(2));

console.log("\n== an explicit Content-Type is respected, not clobbered ==");
await api("/up", { method: "POST", body: "raw", headers: { "Content-Type": "text/csv" } });
check("explicit header kept", ct(3) === "text/csv", ct(3));

await api("/up", { method: "POST", body: "raw", headers: { "content-type": "text/csv" } });
check("lower-case explicit header also detected", ct(4) === "text/csv", ct(4));

console.log("\n== requests without a body are left alone ==");
await api("/api/state");
check("no body -> no forced Content-Type", ct(5) === undefined, ct(5));

console.log("\n== auth header still attached ==");
check("X-SS-Token sent", calls[0].headers["X-SS-Token"] === "tok-123", JSON.stringify(calls[0].headers));

console.log("\n" + (fails === 0 ? "ALL PASS" : `${fails} FAILURES`));
process.exit(fails === 0 ? 0 : 1);
