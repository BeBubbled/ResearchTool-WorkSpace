const assert = require('node:assert/strict');
const {fromDrop, fromFiles} = require('../static/paper_revision_import.js');
const file = (name, size = 10) => ({name, size});
const entry = (name, size) => ({name, isFile: true, file: cb => cb(file(name, size))});
function directory(name, batches) {
  return {name, isDirectory: true, createReader: () => {
    let index = 0; return {readEntries: cb => cb(batches[index++] || [])};
  }};
}
function drop(entries) { return {items: entries.map(e => ({kind: 'file', webkitGetAsEntry: () => e})), files: []}; }
(async () => {
  const root = directory('paper', [[entry('main.tex')], [directory('sections', [[entry('intro.tex')]]), entry('refs.bib')], [entry('image.png')]]);
  const files = await fromDrop(drop([root]));
  assert.deepEqual(files.map(f => f.name), ['main.tex', 'refs.bib', 'sections/intro.tex']);
  assert.deepEqual(fromFiles([{...file('a.tex'), webkitRelativePath: 'paper/sub/a.tex'}], true).map(f => f.name), ['sub/a.tex']);
  assert.equal((await fromDrop({items: [], files: [file('a.tex'), file('a.pdf')]})).length, 1);
  await assert.rejects(fromDrop(drop([entry('a.tex'), entry('A.tex')])), /重复/);
  await assert.rejects(fromDrop(drop([entry('large.tex', 2000001)])), /2 MB/);
  const multi = await fromDrop(drop([directory('one', [[entry('a.tex')]]), directory('two', [[entry('a.tex')]])]));
  assert.deepEqual(multi.map(f => f.name), ['one/a.tex', 'two/a.tex']);
  const hidden = await fromDrop(drop([directory('paper', [[directory('.git', [[entry('private.tex')]]), entry('main.tex')]])]));
  assert.deepEqual(hidden.map(f => f.name), ['main.tex']);
  const dotFiles = await fromDrop(drop([directory('paper', [[entry('.backup.tex'), entry('._main.tex'), entry('main.tex'), entry('.refs.bib')]])]));
  assert.deepEqual(dotFiles.map(f => f.name), ['main.tex']);
  assert.deepEqual(fromFiles([file('.draft.tex'), file('b.tex')]).map(f => f.name), ['b.tex']);
  assert.deepEqual(fromFiles([{...file('a.tex'), webkitRelativePath: 'paper/.cache/a.tex'}, {...file('.x.bib'), webkitRelativePath: 'paper/.x.bib'},
    {...file('c.tex'), webkitRelativePath: 'paper/c.tex'}], true).map(f => f.name), ['c.tex']);
  console.log('Passed: directory pagination, nested paths, file fallback, duplicates, limits, multi-root and hidden file/directory handling.');
})().catch(error => { console.error(error); process.exitCode = 1; });
