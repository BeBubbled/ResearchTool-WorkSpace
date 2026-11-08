/* Browser file entries are content handles; absolute paths require the explicit path input. */
(function(root) {
  'use strict';
  const accepted = name => /\.(tex|bib)$/i.test(name);
  // Dot-prefixed files or folders (.git, .backup.tex, macOS ._x.tex) are hidden; ignore them so their text never enters a project.
  const hidden = name => name.split(/[\\/]/).some(part => part.startsWith('.'));
  function validate(files) {
    if (files.length > 100 || files.reduce((n, item) => n + item.file.size, 0) > 2000000) {
      throw new Error('最多导入 100 份 .tex / .bib，总大小不超过 2 MB。');
    }
    const seen = new Set();
    for (const item of files) {
      if (seen.has(item.name.toLowerCase())) throw new Error(`重复文件路径：${item.name}`);
      seen.add(item.name.toLowerCase());
    }
    return files.sort((a, b) => a.name.localeCompare(b.name));
  }
  function fromFiles(files, directory = false) {
    return validate(Array.from(files).map(file => ({file,
      name: directory && file.webkitRelativePath ? file.webkitRelativePath.split('/').slice(1).join('/') : file.name}))
      .filter(item => accepted(item.name) && !hidden(item.name)));
  }
  async function fromDrop(transfer) {
    // Capture handles before the browser invalidates DataTransfer after the drop event.
    const entries = Array.from(transfer.items || []).filter(i => i.kind === 'file').map(i => i.webkitGetAsEntry?.());
    const fallback = Array.from(transfer.files || []);
    if (!entries.length || entries.some(e => !e)) return fromFiles(fallback);
    const files = [];
    let visited = 0;
    async function walk(entry, prefix, depth) {
      if (++visited > 10000 || depth > 30) throw new Error('目录过大或层级过深，请仅拖入论文工程。');
      const name = prefix + entry.name;
      if (entry.name.startsWith('.')) return;
      if (entry.isFile) {
        if (accepted(name)) {
          const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
          files.push({file, name});
          validate(files);
        }
      } else if (entry.isDirectory) {
        if (['node_modules', '__pycache__'].includes(entry.name)) return;
        const reader = entry.createReader();
        while (true) {
          const children = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
          if (!children.length) break;
          for (const child of children) await walk(child, name + '/', depth + 1);
        }
      }
    }
    for (const entry of entries) await walk(entry, '', 0);
    if (entries.length === 1 && entries[0].isDirectory) {
      const prefix = entries[0].name + '/';
      files.forEach(item => { item.name = item.name.slice(prefix.length); });
    }
    return validate(files);
  }
  const api = {fromFiles, fromDrop, validate, hidden};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.PaperRevisionImport = api;
})(typeof window === 'undefined' ? globalThis : window);
