const assert = require("node:assert/strict");
const handoff = require("../static/prompt_handoff.js");

class MemoryStorage {
  constructor() { this.items = new Map(); }
  getItem(key) { return this.items.has(key) ? this.items.get(key) : null; }
  setItem(key, value) { this.items.set(key, String(value)); }
  removeItem(key) { this.items.delete(key); }
}

// send → take is one-shot and keeps text, source and same-origin API attachments only.
const storage = new MemoryStorage();
const opened = [];
handoff.send({
  text: "【选区】\nx_t = α x_0",
  source: { label: "文献精读", title: "Paper" },
  attachments: [
    { name: "paper.pdf", url: "/api/reader/documents/abc/source" },
    { name: "evil", url: "https://evil.example/steal" },
    { name: "protocol-relative", url: "//evil.example/x" },
    { name: "page", url: "/prompts" },
  ],
}, storage, url => { opened.push(url); return true; });
assert.deepEqual(opened, ["/prompts?handoff=1"]);
const item = handoff.take(storage);
assert.equal(item.text, "【选区】\nx_t = α x_0");
assert.deepEqual(item.source, { label: "文献精读", title: "Paper" });
assert.deepEqual(item.attachments, [{ name: "paper.pdf", url: "/api/reader/documents/abc/source" }]);
assert.equal(handoff.take(storage), null, "a handoff is consumed once");

// Stale, empty or malformed handoffs are ignored.
storage.setItem(handoff.KEY, JSON.stringify({ text: "old", created: Date.now() - handoff.MAX_AGE_MS - 1 }));
assert.equal(handoff.take(storage), null);
storage.setItem(handoff.KEY, "{not json");
assert.equal(handoff.take(storage), null);
assert.throws(() => handoff.build({ text: "   " }), /没有可发送的文字/);
assert.equal(handoff.build({ text: "x".repeat(200000) }).text.length, 100000);

console.log("prompt handoff ok");
