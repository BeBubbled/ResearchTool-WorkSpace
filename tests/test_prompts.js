const assert = require("node:assert/strict");
const { PromptDraft } = require("../static/prompts.js");

const a = { id:"a",title:"A",content:"模板 A\n第二行" };
const b = { id:"b",title:"B",content:"模板 B" };
const draft = new PromptDraft();
assert.equal(draft.add(b),true);
assert.equal(draft.add(a),true);
assert.equal(draft.add(b),false);
assert.equal(draft.text,"模板 B\n\n模板 A\n第二行");
draft.updateText(draft.text + "\n我的补充");
assert.equal(draft.add(a),false);
assert.ok(draft.text.endsWith("我的补充"));
const restored = new PromptDraft(JSON.parse(JSON.stringify(draft.serialize())));
assert.deepEqual(restored.selected.map(row => row.id),["b","a"]);
assert.equal(restored.add(a),false);
const one = {name:"paper.pdf",size:10,lastModified:1};
restored.addFiles([one,one]); restored.addFiles([one]);
assert.equal(restored.files.length,1);
assert.throws(() => restored.addFiles([{name:"large",size:100*1024*1024,lastModified:0}]),/100 MB/);
assert.equal(restored.files.length,1);
assert.throws(() => restored.addFiles(Array.from({length:20},(_,i) => ({name:`${i}`,size:1,lastModified:1}))),/20/);
restored.clear(); assert.equal(restored.text,""); assert.equal(restored.selected.length,0);
assert.equal(restored.files.length,1); assert.equal(restored.add(a),true);
restored.updateText("x".repeat(100000));
assert.throws(() => restored.add(b),/100,000/); assert.equal(restored.selected.length,0);

// Removing just one template releases its button and re-adding appends it last.
const edited = new PromptDraft();
edited.add(a); edited.add(b);
assert.equal(edited.updateText(edited.text.replace(a.content, "")),true);
assert.deepEqual(edited.selected.map(row => row.id),["b"]);
assert.equal(edited.add(b),false);
assert.equal(edited.add(a),true);
assert.deepEqual(edited.selected.map(row => row.id),["b","a"]);
assert.ok(edited.text.endsWith(a.content));
assert.equal(edited.add(a),false);

// Selecting a whole block plus its separator must work even when the next block
// starts with the same words (a common pattern among the bundled Chinese prompts).
const sharedPrefix = new PromptDraft(); sharedPrefix.add(a); sharedPrefix.add(b);
sharedPrefix.updateText(b.content, {start:0,end:a.content.length+2});
assert.deepEqual(sharedPrefix.selected.map(row=>row.id),["b"]);
assert.equal(sharedPrefix.add(a),true);

// Editing within a template keeps its identity, including after saving and reloading.
edited.updateText(edited.text.replace("第二行", "我修改过的第二行"));
assert.equal(edited.add(a),false);
const reloaded = new PromptDraft(JSON.parse(JSON.stringify(edited.serialize())));
reloaded.restoreSelected([a,b]);
assert.equal(reloaded.add(a),false);
reloaded.updateText(reloaded.text.replace("模板 A\n我修改过的第二行", ""));
assert.equal(reloaded.add(a),true);

// Deleting the whole textarea works without the separate clear button, with or without whitespace.
for (const text of ["", "\n  \n"]) {
  edited.updateText(text);
  assert.equal(edited.selected.length,0);
  assert.equal(edited.add(a),true);
  assert.equal(edited.add(a),false);
}

// Restoring deleted text via undo must not insert another copy when the button is clicked.
const undoText = edited.text;
edited.updateText(""); edited.updateText(undoText);
assert.equal(edited.add(a),false);
assert.equal(edited.text,undoText);
assert.deepEqual(edited.selected.map(row => row.id),["a"]);

// Additions before/between templates shift their positions without releasing other buttons.
const shifted = new PromptDraft(); shifted.add(a); shifted.add(b);
shifted.updateText("前言\n" + shifted.text);
shifted.updateText(shifted.text.replace("\n\n", "\n\n补充内容\n\n"));
assert.equal(shifted.add(a),false); assert.equal(shifted.add(b),false);
shifted.updateText(shifted.text.replace(b.content,""));
assert.deepEqual(shifted.selected.map(row=>row.id),["a"]);
assert.equal(shifted.add(b),true);

// Upgrade old saved drafts, including stale selected IDs left by the original bug.
const legacy = new PromptDraft({text:b.content,selected:[{id:a.id,title:a.title},{id:b.id,title:b.title}]});
legacy.restoreSelected([a,b]);
assert.deepEqual(legacy.selected.map(row=>row.id),["b"]);
assert.equal(legacy.add(a),true);
assert.equal(new PromptDraft({text:"",selected:[{id:a.id,title:a.title}]}).selected.length,0);
const windowsLines = new PromptDraft(); windowsLines.add({...a,content:"first\r\nsecond"});
windowsLines.updateText("first\nsecond!");
assert.equal(windowsLines.add(a),false);
console.log("Prompt draft: ordered append, deduplication, manual deletion/re-add, partial edits, undo, legacy draft recovery, persistence, and attachment limits passed.");
