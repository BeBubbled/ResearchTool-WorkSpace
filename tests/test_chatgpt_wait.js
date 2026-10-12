/* Exercise the production wait loop with a clock that advances without wall time. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const script = fs.readFileSync(`${__dirname}/../browser_extension/prompt_bridge/chatgpt.js`, "utf8");

async function waitingCase(login) {
  let elapsed = 0, finish;
  const messages = [], statuses = [];
  const done = new Promise(resolve => {finish = resolve;});
  const target = {}; target.top = target;
  const document = {
    documentElement:{append() {}},
    createElement:() => ({style:{},setAttribute() {}}),
    querySelectorAll:selector => selector === "button" && login ? [{textContent:"登录",getClientRects:() => [{}]}] : [],
  };
  vm.runInNewContext(script, {
    window:target, document, Date:{now:() => elapsed},
    setTimeout(callback, milliseconds) {elapsed += milliseconds; queueMicrotask(callback);},
    chrome:{runtime:{async sendMessage(message) {
      messages.push(message);
      if (message.type === "GET_TASK") return {ok:true,task:{prompt:"测试",files:login ? [{}] : []}};
      if (message.type === "STATUS") {
        statuses.push(message);
        if (message.status === "error") finish();
      }
      return {ok:true};
    }}},
  });
  const watchdog = setTimeout(() => finish(), 1000);
  await done; clearTimeout(watchdog);
  assert.equal(statuses.at(-1)?.status, "error");
  assert.ok(!messages.some(message => ["CLAIM","FILE_CHUNK"].includes(message.type)));
  assert.ok(new Set(statuses.filter(message => message.status === "waiting").map(message => message.message)).size > 3);
  return {elapsed, error:statuses.at(-1).message};
}

(async () => {
  const absent = await waitingCase(false);
  assert.ok(absent.elapsed >= 30000 && absent.elapsed < 31000);
  assert.ok(absent.error.includes("30 秒"));
  const login = await waitingCase(true);
  assert.ok(login.elapsed >= 180000 && login.elapsed < 181000);
  assert.ok(login.error.includes("等待登录已超时"));
  console.log("ChatGPT waiting: visible progress, bounded composer/login waits, clear timeout errors and no premature claim/upload passed.");
})().catch(error => {console.error(error); process.exitCode = 1;});
