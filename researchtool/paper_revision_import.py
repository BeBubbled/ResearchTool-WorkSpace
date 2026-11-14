"""Bounded local imports and a conservative bibliography index; never execute TeX."""
from pathlib import Path, PurePosixPath
import os
import re
from urllib.parse import urlsplit, unquote

SUFFIXES = {".tex", ".bib"}
MAX_IMPORT_BYTES = 2_000_000
SKIP_DIRS = {".git", ".venv", ".runtime", "node_modules", "__pycache__"}


def is_hidden(name):
    """Dot-prefixed files or folders (.git, .backup.tex, macOS ._x.tex) are never imported in bulk."""
    return any(part.startswith(".") for part in str(name).replace("\\", "/").split("/") if part)


def uncomment(text):
    return re.sub(r"(?m)(?<!\\)%[^\n]*", "", text)


def dependencies(text):
    pattern = r"\\(input|include|bibliography|addbibresource)(?:\[[^]]*\])?\s*\{([^{}]+)\}"
    for match in re.finditer(pattern, uncomment(text)):
        suffix = ".tex" if match[1] in {"input", "include"} else ".bib"
        for value in match[2].split(","):
            value = value.strip()
            if value and not any(c in value for c in "\\#$"):
                yield value if PurePosixPath(value).suffix else value + suffix


def is_main_tex(text):
    """A main LaTeX file declares the document class (outside comments)."""
    return bool(re.search(r"\\documentclass\s*(?:\[[^\]]*\])?\s*\{", uncomment(text)))


def rank_candidates(names):
    """main.tex first, then shorter / less nested names; backups and drafts last."""
    def key(name):
        stem = PurePosixPath(name).stem.lower()
        penalty = any(word in stem for word in ("backup", "old", "copy", "draft", "bak", "tmp"))
        return (stem != "main", penalty, name.count("/"), len(name), name.lower())
    return sorted(names, key=key)


def main_candidates(files):
    return rank_candidates([f["name"] for f in files if f["name"].lower().endswith(".tex") and is_main_tex(f["text"])])


def select_main(files, main):
    """Keep the chosen main file and only what it pulls in (\\input / \\include / bibliography),
    resolved relative to the project root or to the including file. Missing files are left to the
    dependency warnings."""
    by_name = {f["name"]: f for f in files}
    if main not in by_name or not main.lower().endswith(".tex"):
        raise ValueError("所选主文件不在导入的 .tex 文件中。")
    kept, queue = [], [main]
    while queue:
        name = queue.pop(0)
        if name in {k["name"] for k in kept} or name not in by_name:
            continue
        item = by_name[name]
        kept.append(item)
        if not name.lower().endswith(".tex"):
            continue
        parent = PurePosixPath(name).parent
        for dependency in dependencies(item["text"]):
            for option in (PurePosixPath(dependency), parent / dependency):
                normalized = option.as_posix().lstrip("./")
                if normalized in by_name:
                    queue.append(normalized)
                    break
    return kept


def scan_main_candidates(value):
    """Main-file candidates in a local directory, reading only the head of each .tex file."""
    path = Path(str(value or "").strip().strip('"'))
    if not path.is_absolute() or not path.is_dir():
        return []
    names, scanned = [], 0
    for current, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        scanned += len(files) + len(dirs)
        if scanned > 10_000:
            break
        for name in files:
            if name.startswith(".") or not name.lower().endswith(".tex"):
                continue
            target = Path(current) / name
            try:
                with target.open("rb") as stream:
                    head = stream.read(65_536).decode("utf-8-sig", errors="replace")
            except OSError:
                continue
            if is_main_tex(head):
                names.append(target.relative_to(path).as_posix())
    return rank_candidates(names)


def decode_file(raw, name):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(f"{name} 不是 UTF-8 编码，请转换后导入。") from None
    return {"name": name, "text": text, "bom": raw.startswith(b"\xef\xbb\xbf")}


def read_local_project(value, main=None):
    value = str(value or "").strip().strip('"')
    if value.lower().startswith("file:"):
        uri = urlsplit(value)
        if uri.netloc not in {"", "localhost"} or uri.query or uri.fragment:
            raise ValueError("仅支持本机绝对路径，不支持远程文件地址。")
        value = unquote(uri.path)
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", value):
            value = value[1:]
    if value.startswith(("\\\\", "//")):
        raise ValueError("仅支持本机路径，不读取网络共享。")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("请输入项目目录或主 .tex 文件的绝对路径。")
    path = path.resolve(strict=True)
    if not path.is_dir() and path.suffix.lower() != ".tex":
        raise ValueError("本地路径必须指向目录或主 .tex 文件。")
    root = path if path.is_dir() else path.parent
    entry = None
    if path.is_dir() and main:
        entry = (root / str(main)).resolve()
        if not entry.is_relative_to(root) or entry.suffix.lower() != ".tex" or not entry.is_file():
            raise ValueError("所选主文件不在该目录中。")
    elif path.is_dir():
        candidates = scan_main_candidates(root)
        if len(candidates) > 1:
            raise ValueError("该目录有多个主 .tex 文件：" + "、".join(candidates[:8]) + "。请选择要导入的主文件。")
        if candidates:
            entry = (root / candidates[0]).resolve()
    files, seen, total = [], set(), 0

    def add(candidate):
        nonlocal total
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root) or candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            raise ValueError("依赖文件或链接超出所选项目目录，请选择包含所有正文的共同父目录。")
        if resolved in seen:
            return None
        if resolved.suffix.lower() not in SUFFIXES or not resolved.is_file():
            raise ValueError("依赖必须是项目中的 .tex 或 .bib 普通文件。")
        if len(files) >= 100:
            raise ValueError("项目最多导入 100 份 .tex / .bib 文件。")
        with resolved.open("rb") as stream:
            raw = stream.read(MAX_IMPORT_BYTES - total + 1)
        total += len(raw)
        if total > MAX_IMPORT_BYTES:
            raise ValueError("项目源文件总大小超过 2 MB。")
        item = decode_file(raw, resolved.relative_to(root).as_posix())
        seen.add(resolved)
        files.append(item)
        return item

    if path.is_dir() and entry is None:  # no main file found: legacy whole-folder import
        scanned = 0
        def unreadable(error):
            raise error
        for current, dirs, names in os.walk(root, followlinks=False, onerror=unreadable):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".") and
                             not (Path(current) / d).is_symlink() and
                             not (hasattr(Path(current) / d, "is_junction") and (Path(current) / d).is_junction()))
            scanned += len(names) + len(dirs)
            if scanned > 10_000:
                raise ValueError("目录文件过多，请选择论文工程目录。")
            for name in sorted(names):
                if not name.startswith(".") and Path(name).suffix.lower() in SUFFIXES:
                    add(Path(current) / name)
    else:
        def visit(candidate):
            item = add(candidate)
            if not item or candidate.suffix.lower() != ".tex":
                return
            for dependency in dependencies(item["text"]):
                options = [root / dependency, candidate.parent / dependency]
                target = next((p for p in options if p.is_file()), None)
                if target is None:
                    raise ValueError(f"缺少依赖 {dependency}；请选择完整项目目录后导入。")
                visit(target)
        visit(entry or path)
    origin = {"kind": "local_path", "path": str(path), "root": str(root), "read_only": True}
    if entry is not None and path.is_dir():
        origin["main"] = entry.relative_to(root).as_posix()
    return files, origin


def dependency_warnings(files):
    names = {f["name"] for f in files}
    warnings = []
    for item in files:
        if not item["name"].lower().endswith(".tex"):
            continue
        for dep in dependencies(item["text"]):
            relative = str(PurePosixPath(item["name"]).parent / dep)
            # Collapse .. only for matching uploaded names; never use it for filesystem access.
            def normalized(value):
                parts = []
                for part in value.split("/"):
                    if part == ".." and parts:
                        parts.pop()
                    elif part not in {"", "."}:
                        parts.append(part)
                return "/".join(parts)
            if normalized(dep) not in names and normalized(relative) not in names:
                warnings.append(f"{item['name']} 引用的 {dep} 未导入。")
    return list(dict.fromkeys(warnings))


def bibliography_index(files):
    """Keep raw entries as metadata; preserve unknown BibTeX syntax in the source export."""
    entries, warnings = {}, []
    for item in files:
        text, cursor = item["text"], 0
        while match := re.search(r"@([A-Za-z]+)\s*([({])", text[cursor:]):
            start = cursor + match.start()
            body_start = cursor + match.end()
            opening = match[2]
            closing = "}" if opening == "{" else ")"
            depth, brace_depth, quoted, i = 1, 0, False, body_start
            while i < len(text) and depth:
                c = text[i]
                if c == "\\":
                    i += 2
                    continue
                if c == '"' and (depth == 1 if opening == "{" else brace_depth == 0):
                    quoted = not quoted
                if not quoted:
                    if opening == "{":
                        if c == "{": depth += 1
                        elif c == "}": depth -= 1
                    else:
                        if c == "{": brace_depth += 1
                        elif c == "}": brace_depth -= 1
                        elif not brace_depth and c == "(": depth += 1
                        elif not brace_depth and c == closing: depth -= 1
                i += 1
            if depth:
                warnings.append(f"{item['name']} 中有无法完整解析的 BibTeX 条目；原文件仍会保留。")
                break
            kind = match[1].lower()
            body = text[body_start:i - 1]
            key = body.split(",", 1)[0].strip()
            if kind not in {"comment", "preamble", "string"} and re.fullmatch(r"[^\s,{}()]+", key) and "," in body:
                if key in entries:
                    warnings.append(f"重复参考文献 key：{key}；模型上下文采用首次出现的条目。")
                else:
                    entries[key] = {"key": key, "file": item["name"], "raw": text[start:i]}
            cursor = i
    return entries, warnings


def cited_keys(text):
    keys = set()
    for match in re.finditer(r"\\(?:[A-Za-z]*cite[A-Za-z]*|nocite)\*?(?:\[[^]]*\])*\s*\{([^{}]*)\}", uncomment(text)):
        keys.update(k.strip() for k in match[1].split(",") if k.strip())
    return keys
