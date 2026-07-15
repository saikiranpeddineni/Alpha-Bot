import assert from "node:assert/strict";
import test from "node:test";

const storage = new Map();
const root = { dataset: {} };
let networkCalls = 0;
globalThis.fetch = async () => { networkCalls += 1; throw new Error("theme contract must not perform network I/O"); };
globalThis.window = { localStorage: { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) } };
globalThis.document = { documentElement: root };
const { applyTheme, persistTheme, readTheme } = await import("../.theme-test-dist/theme.js");

test("unknown or absent persisted values fail closed to default", () => { storage.clear(); assert.equal(readTheme(), "default"); storage.set("alphadesk.theme", "neon-unknown"); assert.equal(readTheme(), "default"); });
test("cockpit preference persists and applies only the root theme attribute", () => { storage.clear(); root.dataset = { unrelated: "preserved" }; networkCalls = 0; persistTheme("cockpit"); assert.equal(storage.get("alphadesk.theme"), "cockpit"); assert.equal(readTheme(), "cockpit"); assert.equal(root.dataset.theme, "cockpit"); assert.equal(root.dataset.unrelated, "preserved"); assert.equal(networkCalls, 0); });
test("default preference deterministically restores the classic root theme", () => { storage.set("alphadesk.theme", "cockpit"); root.dataset.theme = "cockpit"; networkCalls = 0; persistTheme("default"); assert.equal(storage.get("alphadesk.theme"), "default"); assert.equal(readTheme(), "default"); assert.equal(root.dataset.theme, "default"); assert.equal(networkCalls, 0); });
test("applyTheme changes presentation root without touching persistence or network", () => { storage.set("alphadesk.theme", "default"); networkCalls = 0; applyTheme("cockpit"); assert.equal(root.dataset.theme, "cockpit"); assert.equal(storage.get("alphadesk.theme"), "default"); assert.equal(networkCalls, 0); });
