/* Run both sides of the page/content-script handshake, including lost first probes. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { randomUUID } = require("node:crypto");
const { PromptBridge } = require("../static/prompts.js");
const script = fs.readFileSync(`${__dirname}/../browser_extension/prompt_bridge/workbench.js`, "utf8");
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function page() {
  const listeners = [], sent = [];
  const target = {
    location:{origin:"http://127.0.0.1:8765",pathname:"/prompts"},
    crypto:{randomUUID}, setTimeout, clearTimeout, setInterval, clearInterval,
    addEventListener(type, listener) { if (type === "message") listeners.push(listener); },
    postMessage(data, origin) {
      sent.push(data);
      queueMicrotask(() => target.dispatch({data, origin, source:target}));
    },
    dispatch(event) { listeners.forEach(listener => listener(event)); },
  };
  target.top = target;
  return {target, sent};
}
function inject(target, sendMessage = async () => ({ok:true})) {
  vm.runInNewContext(script, {window:target, location:target.location,
    chrome:{runtime:{getManifest:() => ({version:"1.0.1"}), sendMessage}}});
}

(async () => {
  // A document_start content script may announce before the page has its listener.
  const early = page();
  inject(early.target);
  await delay(0);
  const earlyBridge = new PromptBridge(early.target);
  assert.equal((await earlyBridge.request("PING", {}, 700)).version, "1.0.1");
  assert.equal(earlyBridge.pending.size, 0);

  // Legacy document_idle timing: the initial probe is lost, so resend until injected.
  const late = page();
  let announced = 0, forwarded = 0;
  const lateBridge = new PromptBridge(late.target, () => announced++);
  const connecting = lateBridge.request("PING", {}, 1200);
  await delay(350);
  assert.equal(late.sent.filter(row => row.type === "PING").length, 2);
  inject(late.target, async () => { forwarded++; return {ok:true}; });
  assert.equal((await connecting).ok, true);
  assert.equal(announced, 1);
  assert.equal(forwarded, 1);
  const count = late.sent.length;
  await delay(300);
  assert.equal(late.sent.length, count); // Successful probes leave no retry timer.

  // An absent extension times out and does not keep posting in the background.
  const absent = page(), absentBridge = new PromptBridge(absent.target);
  await assert.rejects(absentBridge.request("PING", {}, 550), {code:"BRIDGE_TIMEOUT"});
  const expired = absent.sent.length;
  await delay(300);
  assert.equal(absent.sent.length, expired);
  assert.equal(absentBridge.pending.size, 0);
  // A submission is never retried, even when no acknowledgement arrives.
  await assert.rejects(absentBridge.request("OPEN_CHATGPT", {transfer:{id:"test"}}, 550), {code:"BRIDGE_TIMEOUT"});
  assert.equal(absent.sent.filter(row => row.type === "OPEN_CHATGPT").length, 1);

  const reloaded = page(), reloadedBridge = new PromptBridge(reloaded.target);
  inject(reloaded.target, async () => { throw new Error("Extension context invalidated."); });
  await assert.rejects(reloadedBridge.request("PING", {}, 700), error => error.code === "BRIDGE_RELOAD" && error.message.includes("刷新工作台"));
  const broken = page(), brokenBridge = new PromptBridge(broken.target);
  inject(broken.target, async () => undefined);
  await assert.rejects(brokenBridge.request("PING", {}, 700), error => error.code === "BRIDGE_RUNTIME" && error.message.includes("扩展后台"));

  // Neither side accepts a foreign frame or origin claiming to be the other side.
  const restricted = page(), restrictedBridge = new PromptBridge(restricted.target);
  const probe = restrictedBridge.request("PING", {}, 700);
  const id = restricted.sent[0].id;
  for (const [source, origin] of [[{}, restricted.target.location.origin], [restricted.target, "https://evil.example"]]) {
    restricted.target.dispatch({source, origin, data:{source:"research-prompt-bridge", id, ok:true}});
  }
  assert.equal(restrictedBridge.pending.size, 1);
  let accepted = 0;
  inject(restricted.target, async () => {accepted++; return {ok:true};});
  for (const [source, origin] of [[{}, restricted.target.location.origin], [restricted.target, "https://evil.example"]]) {
    restricted.target.dispatch({source, origin, data:{source:"research-prompt-workbench", id, type:"OPEN_CHATGPT"}});
  }
  assert.equal((await probe).ok, true);
  assert.equal(accepted, 1);
  console.log("Prompt connection: early/late injection, retry cleanup, timeout, no submission replay, reload diagnostics and origin restrictions passed.");
})().catch(error => {console.error(error); process.exitCode = 1;});
