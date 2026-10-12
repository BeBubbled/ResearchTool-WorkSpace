/* Exercise the real service worker protocol with a disposable browser API stub. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

(async () => {
  const stored = {}, requests = [], tabs = [], server = {status:"pending"};
  let onMessage, onRemoved;
  const chrome = {
    storage:{session:{
      async get(key) {return {[key]:stored[key] ? {...stored[key]} : undefined};},
      async set(items) {Object.assign(stored,items);},
      async remove(key) {delete stored[key];},
    }},
    tabs:{async create(options) {tabs.push(options); return {id:42};}, async update(id,options) {tabs.push({id,...options});}, onRemoved:{addListener(fn){onRemoved=fn;}}},
    runtime:{onMessage:{addListener(fn){onMessage=fn;}}},
  };
  const context = vm.createContext({chrome, URL, Date, Map, Number, AbortSignal, fetch:async (url, options) => {
    requests.push({url,options});
    if (url.endsWith("/status")) Object.assign(server,JSON.parse(options.body));
    return {ok:true, async json(){return {...server,prompt:"测试",files:[]};}};
  }});
  vm.runInContext(fs.readFileSync(path.join(__dirname,"../browser_extension/prompt_bridge/background.js"),"utf8"),context);
  const source = {tab:{id:1},frameId:0,url:"http://127.0.0.1:54321/prompts"};
  const target = {tab:{id:42},frameId:0,url:"https://chatgpt.com/"};
  const transfer = {id:"a".repeat(32),token:"b".repeat(43)};
  const send = (message,sender) => new Promise(resolve => onMessage(message,sender,resolve));
  assert.equal((await send({type:"PING"},source)).ok,true);
  assert.equal((await send({type:"PING"},{...source,url:"https://evil.example/prompts"})).ok,false);
  assert.equal((await send({type:"OPEN_CHATGPT",transfer},{...source,frameId:1})).ok,false);
  assert.equal((await send({type:"OPEN_CHATGPT",transfer:{...transfer,id:"../../secret"}},source)).ok,false);
  const results = await Promise.all([send({type:"OPEN_CHATGPT",transfer},source),send({type:"OPEN_CHATGPT",transfer},source)]);
  assert.ok(results.every(row=>row.ok));
  assert.equal(tabs.length,2); // One creation plus one navigation.
  assert.equal(tabs[1].url,"https://chatgpt.com/");
  assert.ok(requests.every(row=>row.url.startsWith("http://127.0.0.1:54321/api/prompts/transfers/")));
  assert.ok(requests.every(row=>row.options.headers.Authorization===`Bearer ${transfer.token}`));
  assert.equal((await send({type:"GET_TASK"},{...target,tab:{id:7}})).task,null);
  assert.equal((await send({type:"GET_TASK"},target)).task.prompt,"测试");
  assert.equal((await send({type:"FILE_CHUNK",index:0,offset:0},target)).ok,false);
  assert.equal((await send({type:"CLAIM"},target)).ok,true);
  assert.equal((await send({type:"CLAIM"},target)).ok,false);
  assert.equal((await send({type:"FILE_CHUNK",index:0,offset:-1},target)).ok,false);
  assert.equal((await send({type:"FILE_CHUNK",index:0,offset:0},target)).ok,true);
  assert.equal((await send({type:"STATUS",status:"completed",message:"已填入"},target)).ok,true);
  assert.equal(stored["prompt-tab-42"],undefined);
  assert.equal(server.status,"completed");
  server.status="pending";
  await send({type:"OPEN_CHATGPT",transfer},source);
  await send({type:"CLAIM"},target);
  assert.equal((await send({type:"GET_TASK"},target)).ok,false); // A refreshed page cannot replay uploads.
  assert.equal(server.status,"error");
  server.status="pending";
  await send({type:"OPEN_CHATGPT",transfer},source);
  onRemoved(42);
  await new Promise(resolve=>setTimeout(resolve,10));
  assert.equal(server.status,"cancelled");
  assert.equal(stored["prompt-tab-42"],undefined);
  console.log("Browser bridge: sender restrictions, single-tab handoff, auth, claim, chunk bounds, reload protection, completion and close cleanup passed.");
})().catch(error=>{console.error(error);process.exitCode=1;});
