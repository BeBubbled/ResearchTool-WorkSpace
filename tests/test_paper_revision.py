"""Alignment, immutable LaTeX, version transactions and model-boundary tests; no real AI calls."""
import copy
import re
import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock
from pathlib import Path

from flask import Flask

import paper_revision
from paper_revision import (PaperRevision, Conflict, TOKEN, create_paper_revision_blueprint, DEFAULT_WORKFLOW,
                            parse_files, realign_units, scope_ids, source_files, validate_text, workflow_shape)
from paper_revision_import import read_local_project, bibliography_index
from google_translate import GoogleTranslateError
from claude_code_cli import ClaudeSessionMissing

ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"X-Paper-Revision": "1"}
SOURCE = r"""\documentclass{article}
\newcommand{\custom}[1]{#1}
\begin{document}
\section{Introduction}

Our method always improves accuracy by 12.5 percent. It uses $x_1 + y$ and \cite{prior}.

The second paragraph discusses the comparison. We preserve uncertainty.

Another paragraph provides context.

\section{Results}

The results remain preliminary.

\begin{table}
\caption{Protected caption.}
\begin{tabular}{cc} A & B \\ 1 & 2 \end{tabular}
\end{table}
\end{document}
"""


class FakeModel:
    def __init__(self):
        self.calls = []
        self.mode = "normal"
        self.release = threading.Event()
        self.release.set()
        self.entered = threading.Event()

    def __call__(self, messages, *args):
        data = json.loads(messages[-1]["content"])
        self.calls.append(data)
        self.entered.set()
        self.release.wait(5)
        if self.mode == "provider_failure":
            raise RuntimeError("secret-api-key must never appear in a response")
        if data["task"] == "context":
            if self.mode == "bad_context":
                return json.dumps({"claims": [{"source_id": "invented", "role": "result", "statement": "invented", "quote": "invented"}], "terms": []})
            return json.dumps({"claims": [{"source_id": s["id"], "role": "background", "statement": "当前原文的背景与论证。",
                "quote": s["text"][:100]} for s in data["sources"][:6]],
                "terms": [{"source_id": s["id"], "en": "accuracy", "zh": "准确率"} for s in data["sources"] if "accuracy" in s["text"]][:6]})
        if data["task"] == "audit":
            return json.dumps({"ok": self.mode != "audit_failure", "issues": ["修改与原文结果矛盾"] if self.mode == "audit_failure" else []})
        if data["task"] == "translate":
            if self.mode == "translation_failure":
                return '{"units":[]}'
            units = [{"id": u["id"], "zh": "中文🙂审阅文字。" + ("准确率" if "accuracy" in u["en"] else "") +
                      "".join(TOKEN.findall(u["en"]))} for u in data["units"]]
            glossary = [{"en": "accuracy", "zh": "准确率"}, {"en": "invented term", "zh": "虚构"}]
            return json.dumps({"units": units, "glossary": glossary})
        comments = data["comments"]
        bid = comments[0]["anchors"][0]["block_id"]
        if self.mode == "outside_scope":
            bid = data["blocks"][-1]["id"]
        block = next(b for b in data["blocks"] if b["id"] == bid)
        after = block["en"].replace("always improves", "may improve")
        if after == block["en"]:
            after = block["en"].replace("preliminary", "tentative")
        if self.mode == "corrupt_token":
            after = TOKEN.sub("", after, count=1)
        outcomes = [{"comment_id": c["id"], "status": "applied", "reason": "降低因果断言强度。"} for c in comments]
        if self.mode == "conflict":
            outcomes[0].update(status="unresolved", reason="两条批注的结论要求相互冲突。")
        if self.mode == "no_change":
            return json.dumps({"changes": [], "outcomes": [{**o, "status": "no_change"} for o in outcomes]})
        return json.dumps({"changes": [{"block_id": bid, "after": after,
            "comment_ids": [c["id"] for c in comments], "reason": "只调整选中句子，段落其余内容保留。"}], "outcomes": outcomes})


class FakeGoogle:
    """Mimics Google's format=html behaviour: keeps sentence spans, moves protected tokens."""
    SENTENCE = re.compile(r'<span id="(u\d+)">(.*?)</span>(?=<span id="u|$)', re.S)

    def __init__(self, key="configured-key"):
        self.key, self.requests, self.break_ids, self.fatal = key, [], set(), None
        self.checked, self.lock = [], threading.Lock()

    def configured(self):
        return bool(self.key)

    def status(self):
        return {"configured": self.configured()}

    def check(self, key):
        if key == "bad":
            raise GoogleTranslateError("Google 翻译 API Key 无效。")
        self.checked.append(key)

    def translate_html(self, segments):
        with self.lock:
            self.requests.append(list(segments))
        if self.fatal:
            raise GoogleTranslateError(self.fatal)
        out = []
        for segment in segments:
            parts = []
            for local_id, inner in self.SENTENCE.findall(segment):
                tokens = TOKEN.findall(inner)
                broken = any(marker in inner for marker in self.break_ids)
                kept = "" if broken else "".join(f'<span translate="no">{t}</span>' for t in reversed(tokens))
                parts.append(f'<span id="{local_id}">谷歌译文{kept}。</span>')
            out.append("".join(parts))
        return out


class ParserTests(unittest.TestCase):
    def test_round_trip_preserves_full_source_and_crlf_bom(self):
        source = SOURCE.replace("\n", "\r\n").replace("accuracy", "accuracy in m²")
        snap = parse_files([{"name": "main.tex", "text": source, "bom": True},
                            {"name": "sections/method.tex", "text": "Details with \\ref{eq:test}."}])
        self.assertEqual(source_files(snap)["main.tex"], "\ufeff" + source)
        self.assertEqual(source_files(snap)["sections/method.tex"], "Details with \\ref{eq:test}.")
        self.assertFalse(any("Protected caption" in b["masked"] for b in snap["blocks"]))
        self.assertEqual("".join(b["masked"] for b in snap["blocks"]).count("12.5"), 0)

    def test_unknown_macro_nested_groups_and_verbatim_stay_opaque(self):
        source = r"Prose \custom[opt]{hidden {nested} text} \verb|$not math$| \def\foo#1{Hidden words #1}."
        snap = parse_files([{"name": "a.tex", "text": source}])
        self.assertEqual(source_files(snap)["a.tex"], source)
        masked = snap["blocks"][0]["masked"]
        for hidden in ("hidden", "nested", "not math", "Hidden words"):
            self.assertNotIn(hidden, masked)

    def test_invalid_protected_syntax_and_new_numbers_are_rejected(self):
        for after in ("Changed ⟦L000001⟧", "Changed ⟦L000000⟧ $x$", "Changed ⟦L000000⟧ 999", "Changed\n\n⟦L000000⟧"):
            with self.assertRaises(ValueError):
                validate_text("Original ⟦L000000⟧", after)

    def test_percent_sign_allowed_only_in_display_chinese(self):
        validate_text("By ⟦L000000⟧ percent.", "提高⟦L000000⟧%。", display_only=True)
        with self.assertRaises(ValueError):
            validate_text("By ⟦L000000⟧ percent.", "By ⟦L000000⟧%.")
        with self.assertRaises(ValueError):
            validate_text("By ⟦L000000⟧ percent.", "提高⟦L000000⟧ 99%。", display_only=True)

    def test_alignment_reconstructs_source_and_abbreviations(self):
        snap = parse_files([{"name": "a.tex", "text": "We use Fig. A. It works, e.g. in practice. Final claim."}])
        units = snap["blocks"][0]["units"]
        self.assertEqual("".join(u["en"] for u in units), source_files(snap)["a.tex"])
        self.assertIn("e.g. in practice.", "".join(u["en"] for u in units))

    def test_bad_math_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_files([{"name": "a.tex", "text": "Prose $unclosed"}])

    def test_environment_options_stay_opaque(self):
        source = r"\begin{enumerate}[label=\alph*.] \item Editable text. \end{enumerate}"
        snap = parse_files([{"name": "a.tex", "text": source}])
        self.assertEqual(source_files(snap)["a.tex"], source)
        self.assertNotIn("label", snap["blocks"][0]["masked"])
        with self.assertRaises(ValueError):
            validate_text("Original ⟦L000000⟧", "Original ⟦L000000⟧[new option]")

    def test_comments_are_never_prose_sentences_or_model_text(self):
        source = ("\\section{Intro}\n% TODO rewrite the intro\nFirst sentence. Second one.\n"
                  "% A whole commented sentence here.\nThird sentence.\n")
        snap = parse_files([{"name": "a.tex", "text": source}])
        self.assertEqual(source_files(snap)["a.tex"], source)
        units = [u for b in snap["blocks"] for u in b["units"]]
        for unit in units:
            self.assertNotIn("TODO", unit["display_en"])
            self.assertNotIn("commented", unit["display_en"])
            self.assertNotIn("commented", paper_revision.model_source(unit["masked"], snap["tokens"]))
            self.assertRegex(re.sub(TOKEN, "", unit["masked"]), r"[A-Za-z]{2,}")
        comments = [v for v in snap["tokens"].values() if v.startswith("%")]
        self.assertEqual(len(comments), 2)
        self.assertTrue(all("TODO" not in paper_revision.describe_token(c, {}) for c in comments))

    def test_sections_follow_latex_headings_without_blank_lines(self):
        source = "\n".join([
            r"\documentclass{article}", r"% \section{Commented in preamble}", r"\begin{document}",
            r"\begin{abstract}", "Abstract words here.", r"\end{abstract}",
            r"\section{Introduction}\label{sec:intro}", "Intro text one.", "",
            "Intro text two.", r"% \section{Commented in body}", r"\section{Related Work}",
            "Related text.", r"\subsection*{Setup}", "Setup text.", r"\end{document}", ""])
        snap = parse_files([{"name": "main.tex", "text": source}])
        self.assertEqual(source_files(snap)["main.tex"], source)
        titles = snap["section_titles"]
        named = [(titles[b["section"]]["title"], b["units"][0]["en"].split("\n")[0]) for b in snap["blocks"]]
        self.assertEqual(named, [("Abstract", r"\begin{abstract}"),
                                 ("Introduction", r"\section{Introduction}\label{sec:intro}"),
                                 ("Introduction", "Intro text two."),
                                 ("Related Work", r"\section{Related Work}"),
                                 ("Setup", r"\subsection*{Setup}")])
        self.assertEqual([t["level"] for t in titles.values()], [0, 0, 0, 1])


class LineageTests(unittest.TestCase):
    ENTRY = {"revision": 2, "from_revision": 1, "comment_ids": ["a", "b"], "reason": "合并两条批注"}

    def units(self, text, zh=True):
        units = parse_files([{"name": "a.tex", "text": text}])["blocks"][0]["units"]
        for i, unit in enumerate(units):
            unit["zh"] = f"旧句{'一二三四五'[i]}。" if zh else ""
        return units

    def test_unchanged_sentences_keep_chinese_and_rewritten_keep_exact_old_text(self):
        old = self.units("Alpha is true. Beta holds. Gamma ends.")
        new = realign_units(old, self.units("Alpha is true. Beta may hold. Gamma ends.", zh=False), self.ENTRY)
        self.assertEqual([u["zh"] for u in new], ["旧句一。", "", "旧句三。"])
        self.assertNotIn("lineage", new[0])
        entry = new[1]["lineage"][0]
        self.assertEqual((entry["en"], entry["zh"], entry["kind"]), ("Beta holds. ", "旧句二。", "replace"))
        self.assertEqual(entry["comment_ids"], ["a", "b"])

    def test_split_merge_insert_and_delete_are_recorded(self):
        old = self.units("Alpha is true. Beta holds. Gamma ends.")
        split = realign_units(old, self.units("Alpha is true. Beta holds now. Indeed it does. Gamma ends.", zh=False), self.ENTRY)
        self.assertEqual({u["lineage"][0]["kind"] for u in split[1:3]}, {"split"})
        self.assertEqual(split[1]["lineage"][0]["group"], split[2]["lineage"][0]["group"])
        self.assertEqual(split[2]["lineage"][0]["en"], "Beta holds. ")
        merged = realign_units(self.units("Alpha is true. Beta holds. Gamma ends."),
                               self.units("Alpha and beta hold. Gamma ends.", zh=False), self.ENTRY)
        self.assertEqual(merged[0]["lineage"][0]["kind"], "merge")
        self.assertEqual(merged[0]["lineage"][0]["zh"], "旧句一。旧句二。")
        deleted = realign_units(self.units("Alpha is true. Beta holds. Gamma ends."),
                                self.units("Alpha is true. Gamma ends.", zh=False), self.ENTRY)
        self.assertEqual(deleted[0]["removed_after"][0]["en"], "Beta holds. ")
        self.assertEqual(deleted[1]["zh"], "旧句三。")
        inserted = realign_units(self.units("Alpha is true. Gamma ends."),
                                 self.units("Alpha is true. New idea. Gamma ends.", zh=False), self.ENTRY)
        self.assertEqual((inserted[1]["lineage"][0]["kind"], inserted[1]["lineage"][0]["en"]), ("insert", ""))

    def test_rounds_stack_newest_first(self):
        first = realign_units(self.units("Alpha is true. Beta holds."),
                              self.units("Alpha is true. Beta may hold.", zh=False), self.ENTRY)
        first[1]["zh"] = "第二版。"
        second = realign_units(first, self.units("Alpha is true. Beta might hold.", zh=False),
                               {**self.ENTRY, "revision": 3, "from_revision": 2, "comment_ids": ["c"]})
        self.assertEqual([e["revision"] for e in second[1]["lineage"]], [3, 2])
        self.assertEqual([e["zh"] for e in second[1]["lineage"]], ["第二版。", "旧句二。"])
        self.assertEqual(second[0]["zh"], "旧句一。")


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.model = FakeModel()
        self.service = PaperRevision(Path(self.temp.name), codex_request=self.model,
            codex_status=lambda: {"chatgptAuthenticated": True, "defaultModel": "test-model", "defaultReasoningEffort": "high"},
            llm_request=lambda preset, messages: self.model(messages), llm_presets=lambda: [{"id": "test", "model": "api-model"}])
        app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))
        app.config["TESTING"] = True
        app.register_blueprint(create_paper_revision_blueprint(self.service))
        self.app, self.client = app, app.test_client()
        self.project = self.service.create("Test", [{"name": "main.tex", "text": SOURCE}])
        self.pid = self.project["id"]

    def tearDown(self):
        self.model.release.set()
        self.service.executor.shutdown(wait=True)
        self.temp.cleanup()

    def wait(self):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            p = self.service.read(self.pid)
            if p["job"]["status"] not in {"queued", "running"}:
                return p
            time.sleep(.01)
        self.fail("job timed out")

    def submit(self):
        p = self.service.read(self.pid)
        self.service.submit(self.pid, {"revision": p["revision"], "comment_ids": [c["id"] for c in p["comments"]], "confirm_usage": True})
        return self.wait()

    def translated(self):
        p = self.submit()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        return p

    def comment(self, scope="auto"):
        p = self.service.read(self.pid)
        snap = self.service.snapshot(self.pid)
        block = next(b for b in snap["blocks"] if "always improves" in b["masked"])
        unit = block["units"][0]
        return self.service.add_comment(self.pid, {"revision": p["revision"], "text": "将绝对断言改为审慎描述", "scope": scope,
            "anchors": [{"unit_id": unit["id"], "block_id": block["id"], "start": 2, "end": 5, "quote": unit["zh"][2:5]}]})

    def test_remove_project_hides_it_but_keeps_paper_files(self):
        paper = Path(self.temp.name) / "paper"
        paper.mkdir()
        (paper / "main.tex").write_text(SOURCE, encoding="utf-8")
        local = self.service.create("Local", [{"name": "main.tex", "text": SOURCE}],
                                    origin={"kind": "local", "path": str(paper), "main": "main.tex"})
        response = self.client.post(f"/api/paper-revision/projects/{local['id']}/remove", json={}, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertNotIn(local["id"], [p["id"] for p in self.service.list_projects()])
        self.assertIn(self.pid, [p["id"] for p in self.service.list_projects()])
        self.assertEqual((paper / "main.tex").read_text(encoding="utf-8"), SOURCE)
        self.assertEqual(len(list((Path(self.temp.name) / "_removed").glob(f"{local['id']}-*/project.json"))), 1)
        with self.assertRaises(KeyError):
            self.service.read(local["id"])
        self.assertEqual(self.client.post(f"/api/paper-revision/projects/{local['id']}/remove", json={}, headers=HEADERS).status_code, 404)

    def test_reparse_fixes_old_sections_and_keeps_chinese_and_comments(self):
        self.translated()
        self.comment()
        p = self.service.read(self.pid)
        # Simulate a project parsed by an older parser: every block lumped into one section.
        path = self.service.folder(self.pid) / f"r{p['revision']}.json"
        stale = json.loads(path.read_text(encoding="utf-8"))
        for block in stale["blocks"]:
            block["section"] = "f0:preamble"
        stale["section_titles"] = {}
        path.write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")
        result = self.service.reparse(self.pid, {"revision": p["revision"]})
        self.assertTrue(result["changed"])
        self.assertEqual(result["unplaced"], [])
        p = self.service.read(self.pid)
        self.assertEqual(p["history"][-1]["kind"], "reparse")
        self.assertEqual(len(p["comments"]), 1)
        snap = self.service.snapshot(self.pid)
        self.assertEqual(source_files(snap)["main.tex"], SOURCE)
        self.assertEqual(sorted(t["title"] for t in snap["section_titles"].values()), ["Introduction", "Results"])
        self.assertTrue(all(u["zh"] for b in snap["blocks"] for u in b["units"]))
        anchor = p["comments"][0]["anchors"][0]
        unit = next(u for b in snap["blocks"] for u in b["units"] if u["id"] == anchor["unit_id"])
        self.assertIn("always improves", unit["en"])
        again = self.service.reparse(self.pid, {"revision": p["revision"]})
        self.assertFalse(again["changed"])

    def test_reparse_folds_old_comment_only_sentences_and_keeps_chinese(self):
        source = "\\section{Intro}\nFirst sentence here.\n% An old note line.\nSecond sentence here.\n"
        pid = self.service.create("C", [{"name": "main.tex", "text": source}])["id"]
        path = self.service.folder(pid) / "r0.json"
        snap = json.loads(path.read_text(encoding="utf-8"))
        block = snap["blocks"][0]
        # Old parser output: the comment line was its own sentence.
        first, rest = block["units"][0], block["units"][1]
        token = TOKEN.match(rest["masked"])
        note, second = rest["masked"][:token.end()], rest["masked"][token.end():]
        self.assertTrue(snap["tokens"][note].startswith("% An old note"))
        block["units"] = [{**first, "zh": "第一句。"},
                          {**rest, "id": block["id"] + ":s2", "masked": note, "en": snap["tokens"][note], "display_en": "", "zh": "注释译文"},
                          {**rest, "id": block["id"] + ":s3", "masked": second, "en": second, "display_en": second, "zh": "第二句。"}]
        path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        result = self.service.reparse(pid, {"revision": 0})
        self.assertTrue(result["changed"])
        units = self.service.snapshot(pid)["blocks"][0]["units"]
        self.assertEqual([u["zh"] for u in units], ["第一句。", "第二句。"])
        self.assertEqual(source_files(self.service.snapshot(pid))["main.tex"], source)

    def test_full_multiround_workflow_preserves_latex_and_old_versions(self):
        self.translated()
        before = self.service.snapshot(self.pid)
        self.comment()
        p = self.submit()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        self.assertEqual(p["revision"], 2)
        self.assertFalse(p["comments"])
        current = self.service.snapshot(self.pid)
        self.assertEqual(source_files(current)["main.tex"], SOURCE.replace("always improves", "may improve"))
        self.assertEqual(source_files(self.service.snapshot(self.pid, 0))["main.tex"], SOURCE)
        changed = p["history"][-1]["changes"][0]["block_id"]
        self.assertEqual([b for b in before["blocks"] if b["id"] != changed], [b for b in current["blocks"] if b["id"] != changed])
        self.assertEqual(p["history"][-1]["comments"][0]["revision"], 1)
        unit = next(b for b in current["blocks"] if b["id"] == changed)["units"][0]
        self.service.add_comment(self.pid, {"revision": 2, "text": "继续检查", "scope": "paragraph", "anchors": [
            {"block_id": changed, "unit_id": unit["id"], "start": 0, "end": 2, "quote": unit["zh"][:2]}]})
        self.model.mode = "no_change"
        self.assertEqual(self.submit()["revision"], 3)

    def test_stale_and_forged_anchors_rejected(self):
        self.translated()
        p = self.comment()
        comment = p["comments"][0]
        for payload in ({**comment, "revision": 0}, {**comment, "anchors": [{**comment["anchors"][0], "quote": "错误"}]}):
            with self.assertRaises(Conflict):
                self.service.add_comment(self.pid, payload)

    def test_scope_enforced_and_neighbors_do_not_cross_section(self):
        self.translated()
        p = self.comment("paragraph")
        self.model.mode = "outside_scope"
        result = self.submit()
        self.assertEqual(result["job"]["status"], "failed")
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["comments"], p["comments"])
        snap = self.service.snapshot(self.pid)
        auto = {**p["comments"][0], "scope": "auto"}
        allowed = scope_ids(auto, snap["blocks"])
        self.assertNotIn(snap["blocks"][-1]["id"], allowed)
        self.assertEqual(scope_ids({**auto, "scope": "document"}, snap["blocks"]), {b["id"] for b in snap["blocks"]})

    def test_bad_translation_and_conflict_are_atomic(self):
        self.translated()
        self.comment()
        before = self.service.snapshot(self.pid)
        for mode in ("conflict", "corrupt_token", "provider_failure", "audit_failure"):
            self.model.mode = mode
            p = self.submit()
            self.assertEqual(p["job"]["status"], "failed", mode)
            self.assertEqual(p["revision"], 1)
            self.assertEqual(self.service.snapshot(self.pid), before)
            self.assertEqual(len(p["comments"]), 1)
            self.assertNotIn("secret-api-key", p["job"]["error"])
        # A translation failure no longer discards an audited English revision: the English is
        # saved, the rewritten sentence simply has no Chinese yet, and the user is told to retry.
        self.model.mode = "translation_failure"
        p = self.submit()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        self.assertEqual(p["revision"], 2)
        self.assertIn("补译", p["job"]["warning"])
        self.assertNotIn("preview", p["job"])
        snap = self.service.snapshot(self.pid)
        self.assertIn("may improve", source_files(snap)["main.tex"])
        self.assertEqual(sum(1 for b in snap["blocks"] for u in b["units"] if not u["zh"]), 1)

    def test_translation_keeps_valid_sentences_retries_rejects_and_previews(self):
        original = self.service.codex_request
        attempts = {}
        previews = []
        real_preview = self.service.preview
        def preview(pid, jid, done):
            previews.append(dict(done))
            return real_preview(pid, jid, done)
        self.service.preview = preview
        def flaky(messages, *args):
            data = json.loads(messages[-1]["content"])
            if data["task"] != "translate":
                return original(messages, *args)
            self.model.calls.append(data)
            units = []
            for u in data["units"]:
                attempts[u["id"]] = attempts.get(u["id"], 0) + 1
                tokens = TOKEN.findall(u["en"])
                zh = "译文" + "".join(reversed(tokens))  # reordered tokens are fine in Chinese
                if u["id"] == "p0002:s2":
                    zh = "译文"  # always drops its tokens: rejected twice, stays untranslated
                if u["id"] == "p0003:s1" and attempts[u["id"]] == 1:
                    zh = "译文 $x$"  # rejected once, fixed on retry
                units.append({"id": u["id"], "zh": zh})
            return json.dumps({"units": units})
        self.service.codex_request = flaky
        p = self.submit()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        self.assertIn("1 句暂无中文", p["job"]["warning"])
        snap = self.service.snapshot(self.pid)
        missing = [u["id"] for b in snap["blocks"] for u in b["units"] if not u["zh"]]
        self.assertEqual(missing, ["p0002:s2"])
        self.assertEqual((attempts["p0002:s2"], attempts["p0003:s1"], attempts["p0001:s1"]), (2, 2, 1))
        self.assertTrue(previews and "p0001:s1" in previews[0])
        retry = [c for c in self.model.calls if c["task"] == "translate" and "previous_errors" in c]
        self.assertEqual(set(retry[0]["previous_errors"]), {"p0002:s2", "p0003:s1"})
        # The next round translates only the remaining sentence.
        self.service.codex_request = original
        p = self.submit()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        self.assertEqual([u["id"] for u in self.model.calls[-1]["units"]], ["p0002:s2"])
        self.assertTrue(self.service.snapshot(self.pid)["translated"])

    def test_translation_batches_follow_sections(self):
        text = "\n\n".join([r"\section{One}", "Alpha text here. Beta text.", r"\section{Two}",
                            "Long sentence about the method. " * 300])
        snap = parse_files([{"name": "a.tex", "text": text}])
        batches = PaperRevision.translation_batches(snap, limit=2000)
        self.assertEqual(batches[0]["label"], "「One」")
        first_ids = {u["id"].split(":")[0] for u in batches[0]["units"]}
        self.assertTrue(all(b["section"] == snap["blocks"][0]["section"] for b in snap["blocks"] if b["id"] in first_ids))
        self.assertGreater(len(batches), 2)
        self.assertTrue(all(b["label"] == "「Two」" for b in batches[1:]))
        small = PaperRevision.translation_batches(parse_files([{"name": "b.tex", "text":
            "\n\n".join([r"\section{A}", "Short one.", r"\section{B}", "Short two."])}]))
        self.assertEqual(len(small), 1)
        self.assertEqual(small[0]["label"], "「A」 等 2 节")

    def test_google_translation_needs_no_model_and_falls_back_for_misaligned_sentences(self):
        google = FakeGoogle()
        self.service.google = google
        snap = self.service.snapshot(self.pid)
        tricky = next(u for b in snap["blocks"] for u in b["units"] if TOKEN.search(u["masked"]) and "It uses" in u["masked"])
        google.break_ids = {"It uses"}  # Google drops this sentence's protected tokens every time
        self.service.submit(self.pid, {"revision": 0, "confirm_usage": True, "translator": "google"})
        p = self.wait()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        self.assertEqual(p["job"]["config"]["stages"]["translate"], {"engine": "google"})
        self.assertFalse([c for c in self.model.calls if c["task"] == "context"], "Google rounds skip model reading")
        model_units = [u["id"] for c in self.model.calls if c["task"] == "translate" for u in c["units"]]
        self.assertEqual(set(model_units), {tricky["id"]})
        snap = self.service.snapshot(self.pid)
        self.assertTrue(snap["translated"])
        zh = {u["id"]: u["zh"] for b in snap["blocks"] for u in b["units"]}
        self.assertTrue(zh[tricky["id"]].startswith("中文"))
        self.assertTrue(all(v.startswith("谷歌译文") for k, v in zh.items() if k != tricky["id"]))
        # first round: paragraph segments; second round: the failed sentence alone
        self.assertEqual(len(google.requests), 2)
        self.assertEqual(google.requests[1][0].count('<span id="u'), 1)

    def test_google_requires_global_key_and_fatal_error_keeps_version(self):
        google = FakeGoogle(key="")
        self.service.google = google
        with self.assertRaisesRegex(ValueError, "全局设置"):
            self.service.submit(self.pid, {"revision": 0, "confirm_usage": True, "translator": "google"})
        google.key = "good-key"
        listing = self.client.get("/api/paper-revision/projects", headers=HEADERS).get_json()
        self.assertEqual(listing["google"], {"configured": True})
        google.fatal = "Google 翻译配额已用尽（检查 Cloud 控制台的配额与结算）。"
        self.service.submit(self.pid, {"revision": 0, "confirm_usage": True, "translator": "google"})
        p = self.wait()
        self.assertEqual(p["job"]["status"], "failed")
        self.assertIn("配额", p["job"]["error"])
        self.assertEqual(p["revision"], 0)

    def test_first_translation_is_one_request_and_large_papers_go_sequentially(self):
        calls = self.claude_backend()
        p = self.submit_claude()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        translate = [c for c in calls if c[0] == "translate"]
        self.assertEqual(len(translate), 1)
        self.assertFalse([c for c in calls if c[0] == "context"])
        snap = self.service.snapshot(self.pid)
        self.assertEqual(len(self.model.calls[-1]["units"]), sum(len(b["units"]) for b in snap["blocks"]))
        # A paper above the one-request limit: whole sections, one request after another.
        text = "\n\n".join(f"\\section{{Part {chr(65 + i)}}}\n\n" + "The method preserves uncertainty. " * 700 for i in range(4))
        self.pid = self.service.create("Large", [{"name": "main.tex", "text": text}])["id"]
        active, peak, lock = [0], [0], threading.Lock()
        inner = self.service.claude_request
        def slow(messages, model, effort):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            try:
                return inner(messages, model, effort)
            finally:
                with lock:
                    active[0] -= 1
        self.service.claude_request = slow
        calls.clear()
        p = self.submit_claude()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        parts = [c for c in self.model.calls if c["task"] == "translate"][-len([c for c in calls if c[0] == "translate"]):]
        self.assertEqual(len(parts), 2)  # 4 sections of ~24k chars: two whole sections per request
        self.assertEqual(peak[0], 1)
        self.assertTrue([c for c in calls if c[0] == "context"], "multi-part translation reads the paper first")
        for part in parts:  # whole sections only: heading + body paragraph of two sections
            self.assertEqual(len({u["id"].split(":")[0] for u in part["units"]}), 4)
        self.assertTrue(self.service.snapshot(self.pid)["translated"])

    def submit_claude(self):
        self.service.submit(self.pid, {"revision": self.service.read(self.pid)["revision"], "backend": "claude",
                                       "confirm_usage": True, "comment_ids": [c["id"] for c in self.service.read(self.pid)["comments"]]})
        return self.wait()

    def test_each_workflow_step_uses_its_own_engine(self):
        routed = []
        def recorder(engine):
            def call(*args):
                messages = args[0] if engine != "api" else args[1]
                routed.append((json.loads(messages[-1]["content"])["task"], engine, args[1:] if engine != "api" else (args[0],)))
                return self.model(messages)
            return call
        self.service.codex_request = recorder("codex")
        self.service.llm_request = recorder("api")
        self.claude_backend()
        self.service.claude_request = recorder("claude")
        google = FakeGoogle()
        google.break_ids = {"It uses"}
        self.service.google = google
        workflow = {"translate": {"engine": "google"},
                    "translate_fallback": {"engine": "api", "preset": "test"},
                    "context": {"engine": "codex"},
                    "revise": {"engine": "claude", "model": "opus", "effort": "max"},
                    "audit": {"engine": "codex", "model": "test-model", "effort": "low"}}
        # A Google translation round only checks the steps it runs: no context, revise or audit.
        self.service.codex_status = lambda: {}
        self.service.submit(self.pid, {"revision": 0, "confirm_usage": True, "workflow": workflow})
        p = self.wait()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        self.assertEqual({(t, e) for t, e, _ in routed}, {("translate", "api")})
        self.service.codex_status = lambda: {"chatgptAuthenticated": True, "defaultModel": "test-model",
                                             "defaultReasoningEffort": "high", "models": [{"id": "test-model"}]}
        routed.clear()
        self.comment()
        p = self.service.read(self.pid)
        self.service.submit(self.pid, {"revision": 1, "confirm_usage": True, "workflow": workflow,
                                       "comment_ids": [c["id"] for c in p["comments"]]})
        p = self.wait()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        engines = {}
        for task, engine, detail in routed:
            engines.setdefault(task, set()).add((engine, detail))
        self.assertEqual(engines["context"], {("codex", ("test-model", "high"))})
        self.assertEqual(engines["revise"], {("claude", ("opus", "max"))})
        self.assertEqual(engines["audit"], {("codex", ("test-model", "low"))})
        self.assertEqual(p["job"]["config"]["stages"]["context"], {"engine": "codex", "model": "test-model", "effort": "high"})
        self.assertEqual(p["job"]["config"]["stages"]["translate_fallback"]["model"], "api-model")

    def test_workflow_validation_and_global_defaults(self):
        for bad in ({"translate": {"engine": "nope"}}, {"revise": {"engine": "google"}},
                    {"revise": {"engine": "claude", "model": "haiku", "effort": "high"}},
                    {"audit": {"engine": "api"}}, "not a dict"):
            with self.assertRaises(ValueError):
                workflow_shape(bad)
        self.assertEqual(workflow_shape({}), DEFAULT_WORKFLOW)
        with self.assertRaisesRegex(ValueError, "Codex"):
            self.service.codex_status = lambda: {}
            self.service.submit(self.pid, {"revision": 0, "confirm_usage": True,
                                           "workflow": {k: {"engine": "codex"} for k in DEFAULT_WORKFLOW}})
        with self.assertRaisesRegex(ValueError, "API 预设不存在"):
            self.service.submit(self.pid, {"revision": 0, "confirm_usage": True,
                                           "workflow": {k: {"engine": "api", "preset": "missing"} for k in DEFAULT_WORKFLOW}})
        saved = []
        self.service.workflow_saver = saved.append
        self.service.workflow_defaults = lambda: saved[-1] if saved else DEFAULT_WORKFLOW
        mixed = {**DEFAULT_WORKFLOW, "translate": {"engine": "google"}, "context": {"engine": "codex"}}
        response = self.client.put("/api/paper-revision/workflow", headers=HEADERS, json={"workflow": mixed})
        self.assertEqual(response.status_code, 200, response.get_json())
        options = response.get_json()["options"]
        self.assertEqual(options["defaults"]["translate"], {"engine": "google"})
        self.assertEqual(options["defaults"]["context"], {"engine": "codex", "model": "", "effort": ""})
        self.assertEqual([s["id"] for s in options["stages"]], list(DEFAULT_WORKFLOW))
        self.assertEqual([{k: p[k] for k in ("id", "name", "model")} for p in options["api"]], [{"id": "test", "name": "test", "model": "api-model"}])
        bad = self.client.put("/api/paper-revision/workflow", headers=HEADERS, json={"workflow": {"revise": {"engine": "google"}}})
        self.assertEqual(bad.status_code, 400)
        listing = self.client.get("/api/paper-revision/projects", headers=HEADERS).get_json()
        self.assertEqual(listing["options"]["defaults"]["translate"], {"engine": "google"})

    def test_user_macros_are_displayed_as_values_but_sources_stay_untouched(self):
        main = "\\input{numbers}\n\\begin{document}\nThe gain is \\previewWithMin -- \\previewWithMax \\, dB on \\nImages{} images.\n\\end{document}\n"
        numbers = "% generated\n\\newcommand{\\previewWithMin}{31.2}\n\\newcommand{\\previewWithMax}{33.5}\n\\newcommand{\\nImages}{1,610}\n"
        self.pid = self.service.create("Macros", [{"name": "main.tex", "text": main},
                                                  {"name": "numbers.tex", "text": numbers}])["id"]
        snap = self.service.snapshot(self.pid)
        unit = next(u for b in snap["blocks"] for u in b["units"] if "gain" in u["en"])
        self.assertIn("31.2–33.5\u2009dB on 1,610 images", unit["display_en"])
        self.assertIn("\\previewWithMin", unit["en"])  # the LaTeX source view keeps the macro
        detail = self.client.get(f"/api/paper-revision/projects/{self.pid}", headers=HEADERS).get_json()
        self.assertIn("31.2–33.5", json.dumps(detail["snapshot"], ensure_ascii=False))
        p = self.translated()
        start = unit["display_en"].index("31.2")
        p = self.service.add_comment(self.pid, {"revision": 1, "text": "核对数字", "scope": "paragraph", "anchors": [
            {"side": "en", "unit_id": unit["id"], "block_id": unit["id"].split(":")[0], "start": start,
             "end": start + len("31.2–33.5"), "quote": "31.2–33.5"}]})
        self.assertEqual(p["comments"][0]["anchors"][0]["quote"], "31.2–33.5")
        translate = next(c for c in self.model.calls if c["task"] == "translate")
        self.assertTrue(any("⇒ 31.2" in v for v in translate["protected"].values()))
        zh = next(u for b in self.service.snapshot(self.pid)["blocks"] for u in b["units"] if u["id"] == unit["id"])["zh"]
        self.assertIn("31.2", zh)
        self.assertNotIn("\\previewWithMin", zh)
        exported = source_files(self.service.snapshot(self.pid))
        self.assertEqual((exported["main.tex"], exported["numbers.tex"]), (main, numbers))
        self.assertEqual(p["revision"], 1)

    def session_backend(self, folder):
        self.claude_backend()
        turns = []
        def session_request(messages, model, effort, *, session_id, resume, cwd, name):
            body = json.loads(messages[-1]["content"].split("\n\n", 1)[1])
            turns.append({"id": session_id, "resume": resume, "cwd": cwd, "name": name, "model": (model, effort),
                          "system": messages[0]["content"], "body": body})
            if self.session_missing and resume:
                self.session_missing = False
                raise ClaudeSessionMissing("Claude Code 会话已不存在。")
            blocks = {b["id"]: b for b in body.get("paper", []) + body.get("updated_blocks", [])}
            comment = body["comments"][0]
            bid = comment["anchors"][0]["block_id"]
            block = blocks.get(bid) or {"en": self.known[bid]}
            after = block["en"].replace("always improves", "may improve").replace("preliminary", "tentative")
            reply = {"changes": [{"block_id": bid, "after": after, "comment_ids": [comment["id"]], "reason": "审慎表述。"}],
                     "outcomes": [{"comment_id": comment["id"], "status": "applied", "reason": "已处理。"}]}
            return "好的，修改如下：\n" + json.dumps(reply, ensure_ascii=False)  # prose around JSON is tolerated
        self.session_missing = False
        self.service.claude_session_request = session_request
        self.known = {b["id"]: b["masked"] for b in self.service.snapshot(self.pid)["blocks"]}
        return turns

    def test_revision_rounds_continue_one_saved_claude_session_in_the_project_folder(self):
        folder = Path(self.temp.name) / "paper-project"
        (folder / "paper").mkdir(parents=True)
        (folder / "CLAUDE.md").write_text("project notes", encoding="utf-8")
        (folder / "paper" / "main.tex").write_text(SOURCE, encoding="utf-8")
        files, origin = read_local_project(str(folder / "paper" / "main.tex"))
        self.pid = self.service.create("会话论文", files, origin)["id"]
        self.assertEqual(self.service.read(self.pid)["claude_project"], {"path": str(folder.resolve()), "reason": "CLAUDE.md"})
        turns = self.session_backend(folder)
        self.translated()
        self.comment()
        p = self.submit_claude()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        first = turns[-1]
        self.assertEqual((first["resume"], first["cwd"], first["name"]), (False, str(folder.resolve()), "双语改稿 · 会话论文"))
        self.assertEqual(first["body"]["round"], "start")
        self.assertEqual(len(first["body"]["paper"]), len(self.service.snapshot(self.pid)["blocks"]))
        target = first["body"]["comments"][0]["anchors"][0]
        commented = next(b for b in self.service.snapshot(self.pid)["blocks"] if b["id"] == target["block_id"])
        self.assertEqual(target["sentence_id"], commented["units"][0]["id"])
        self.assertIn("always improves", target["english_sentence"])  # the English that was translated…
        self.assertIn(target["selected_chinese"], target["chinese_sentence"])  # …where the Chinese was selected
        self.assertNotIn("start", target)
        self.assertIn("Read/Grep/Glob", first["system"])
        self.assertEqual(first["model"], ("opus", "high"))
        self.assertEqual(p["history"][-1]["context_mode"], "session")
        state = self.service.read(self.pid)["claude_session"]
        self.assertEqual((state["id"], state["rounds"], state["last_error"]), (first["id"], 1, ""))
        # Minutes later: a new comment continues the same session and resends nothing unchanged.
        unit = next(b for b in self.service.snapshot(self.pid)["blocks"] if "preliminary" in b["masked"])["units"][0]
        self.service.add_comment(self.pid, {"revision": 2, "text": "更审慎", "scope": "paragraph", "anchors": [
            {"unit_id": unit["id"], "block_id": unit["id"].split(":")[0], "start": 0, "end": 2, "quote": unit["zh"][:2]}]})
        p = self.submit_claude()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        second = turns[-1]
        self.assertEqual((second["id"], second["resume"], second["body"]["round"]), (first["id"], True, "continue"))
        self.assertEqual(second["body"]["updated_blocks"], [])
        self.assertEqual(self.service.read(self.pid)["claude_session"]["rounds"], 2)

    def test_rejected_round_resyncs_and_deleted_session_restarts(self):
        turns = self.session_backend(None)
        self.translated()
        self.comment()
        self.model.mode = "audit_failure"  # the session proposes a change, the audit rejects the round
        failed = self.submit_claude()
        self.assertEqual(failed["job"]["status"], "failed")
        state = self.service.read(self.pid)["claude_session"]
        self.assertIn("一致性检查", state["last_error"])
        self.assertTrue(state["cwd"].endswith("claude-workspace"))  # uploads use a per-paper workspace
        self.model.mode = "normal"
        p = self.submit_claude()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        resent = turns[-1]["body"]
        self.assertTrue(turns[-1]["resume"])
        self.assertIn("一致性检查", resent["previous_round"])
        # only the block the rejected round proposed to change is resent (with its real, unchanged text)
        target = next(b for b in self.service.snapshot(self.pid)["blocks"] if "may improve" in b["masked"])["id"]
        self.assertEqual([b["id"] for b in resent["updated_blocks"]], [target])
        # The session was deleted in Claude Code: the next round transparently starts a new one.
        unit = next(b for b in self.service.snapshot(self.pid)["blocks"] if "preliminary" in b["masked"])["units"][0]
        self.service.add_comment(self.pid, {"revision": 2, "text": "再改", "scope": "paragraph", "anchors": [
            {"unit_id": unit["id"], "block_id": unit["id"].split(":")[0], "start": 0, "end": 2, "quote": unit["zh"][:2]}]})
        old_id = turns[-1]["id"]
        self.session_missing = True
        p = self.submit_claude()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        self.assertEqual((turns[-2]["id"], turns[-2]["resume"]), (old_id, True))
        self.assertEqual((turns[-1]["resume"], turns[-1]["body"]["round"]), (False, "start"))
        self.assertNotEqual(turns[-1]["id"], old_id)
        self.assertEqual(self.service.read(self.pid)["claude_session"]["id"], turns[-1]["id"])

    def test_project_facts_need_verified_evidence(self):
        folder = Path(self.temp.name) / "evidence-project"
        (folder / "paper").mkdir(parents=True)
        (folder / "CLAUDE.md").write_text("Notes: the accuracy gain was measured on 3 seeds only.\n", encoding="utf-8")
        (folder / "paper" / "main.tex").write_text(SOURCE, encoding="utf-8")
        (Path(self.temp.name) / "outside.md").write_text("measured on 3 seeds only", encoding="utf-8")
        files, origin = read_local_project(str(folder / "paper" / "main.tex"))
        self.pid = self.service.create("证据", files, origin)["id"]
        self.session_backend(folder)
        self.translated()
        reply = {}
        def session_request(messages, model, effort, **kwargs):
            body = json.loads(messages[-1]["content"].split("\n\n", 1)[1])
            comment = body["comments"][0]
            bid = comment["anchors"][0]["block_id"]
            block = next(b for b in body.get("paper", []) + body.get("updated_blocks", []) if b["id"] == bid) \
                if any(b["id"] == bid for b in body.get("paper", []) + body.get("updated_blocks", [])) else {"en": self.known[bid]}
            change = {"block_id": bid, "after": block["en"].replace("always improves", "may improve") + " It was measured on 3 seeds.",
                      "comment_ids": [comment["id"]], "reason": "依据项目笔记补充局限。", **reply}
            return json.dumps({"changes": [change], "outcomes": [{"comment_id": comment["id"], "status": "applied", "reason": "已补充。"}]})
        self.service.claude_session_request = session_request
        self.known = {b["id"]: b["masked"] for b in self.service.snapshot(self.pid)["blocks"]}
        cases = [({}, "原文和已核实证据中都没有的数字"),
                 ({"evidence": [{"path": "CLAUDE.md", "quote": "measured on 5 seeds"}]}, "找不到"),
                 ({"evidence": [{"path": "../outside.md", "quote": "measured on 3 seeds only"}]}, "不在论文所属项目目录"),
                 ({"evidence": "CLAUDE.md"}, "格式无效")]
        self.comment()
        for extra, message in cases:
            reply.clear(); reply.update(extra)
            failed = self.submit_claude()
            self.assertEqual(failed["job"]["status"], "failed", extra)
            self.assertIn(message, failed["job"]["error"])
        reply.clear(); reply.update({"evidence": [{"path": "CLAUDE.md", "quote": "the accuracy gain was measured on 3  seeds only"}]})
        p = self.submit_claude()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        change = p["history"][-1]["changes"][0]
        self.assertEqual(change["evidence"], [{"path": "CLAUDE.md", "quote": "the accuracy gain was measured on 3  seeds only"}])
        audit = [c for c in self.model.calls if c["task"] == "audit"][-1]
        self.assertEqual(audit["changes"][0]["evidence"][0]["path"], "CLAUDE.md")
        self.assertIn("3 seeds", source_files(self.service.snapshot(self.pid))["main.tex"])

    def test_session_settings_mode_reset_and_folder(self):
        turns = self.session_backend(None)
        self.translated()
        folder = Path(self.temp.name) / "elsewhere"
        folder.mkdir()
        with self.assertRaises(ValueError):
            self.service.set_claude_session(self.pid, {"revision": 1, "folder": "relative/path"})
        p = self.client.post(f"/api/paper-revision/projects/{self.pid}/claude-session", headers=HEADERS,
                             json={"revision": 1, "folder": str(folder)}).get_json()
        self.assertEqual(p["claude_project"], {"path": str(folder.resolve()), "reason": "manual"})
        self.service.set_claude_session(self.pid, {"revision": 1, "mode": False})
        self.comment()
        self.submit_claude()
        self.assertEqual(turns, [], "session mode off: stateless full-text revision")
        self.assertEqual(self.service.read(self.pid)["history"][-1]["context_mode"], "full_text")

    def local_paper(self, text=SOURCE):
        folder = Path(self.temp.name) / "disk-paper"
        folder.mkdir(exist_ok=True)
        (folder / "main.tex").write_bytes(text.encode("utf-8"))
        files, origin = read_local_project(str(folder / "main.tex"))
        self.pid = self.service.create("磁盘论文", files, origin)["id"]
        return folder / "main.tex"

    def open(self, **data):
        return self.client.post(f"/api/paper-revision/projects/{self.pid}/open", headers=HEADERS, json=data).get_json()

    def test_open_resyncs_from_disk_and_only_changed_sentences_need_translation(self):
        path = self.local_paper()
        self.translated()
        self.assertEqual(self.open()["status"], "in_sync")
        edited = SOURCE.replace("The second paragraph discusses the comparison.", "The second paragraph now compares two baselines.")
        path.write_bytes(edited.encode("utf-8"))
        opened = self.open()
        self.assertEqual(opened["status"], "synced")
        self.assertEqual((opened["sync"]["changed"], opened["sync"]["files_changed"]), (1, ["main.tex"]))
        snap = self.service.snapshot(self.pid)
        missing = [u["en"] for b in snap["blocks"] for u in b["units"] if not u["zh"]]
        self.assertEqual(missing, ["The second paragraph now compares two baselines. "])
        self.assertEqual(source_files(snap)["main.tex"], edited)
        calls_before = len(self.model.calls)
        p = self.submit()  # “补译缺失中文”
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        translated = [u["id"] for c in self.model.calls[calls_before:] if c["task"] == "translate" for u in c["units"]]
        self.assertEqual(len(translated), 1)
        self.assertEqual(self.open()["status"], "in_sync")

    def test_disk_and_tool_both_changed_asks_which_wins(self):
        path = self.local_paper()
        self.translated()
        self.comment()
        self.assertEqual(self.submit()["job"]["status"], "completed")  # a tool revision not on disk
        path.write_bytes(SOURCE.replace("Another paragraph provides context.", "Another paragraph gives context.").encode("utf-8"))
        opened = self.open()
        self.assertEqual(opened["status"], "conflict")
        self.assertEqual(opened["conflict"]["tool_revisions"], [2])
        revision = self.service.read(self.pid)["revision"]
        kept = self.open(resolution="tool")
        self.assertEqual((kept["status"], kept["project"]["revision"]), ("kept_tool", revision))
        self.assertIn("may improve", source_files(self.service.snapshot(self.pid))["main.tex"])
        self.assertEqual(self.open()["status"], "in_sync")  # that disk version is now acknowledged
        path.write_bytes(SOURCE.replace("Another paragraph provides context.", "A third version.").encode("utf-8"))
        self.assertEqual(self.open()["status"], "synced")  # no new tool edits since: disk wins silently
        self.assertIn("A third version.", source_files(self.service.snapshot(self.pid))["main.tex"])

    def test_each_visit_starts_without_old_comments_and_backup_can_be_restored(self):
        self.translated()
        self.comment()
        opened = self.open()
        self.assertEqual(opened["backup"]["count"], 1)
        self.assertEqual(opened["project"]["comments"], [])
        backup = opened["project"]["comment_backups"][-1]
        self.assertEqual(backup["comments"][0]["text"], "将绝对断言改为审慎描述")
        self.assertIsNone(self.open()["backup"], "nothing new to back up")
        restored = self.client.post(f"/api/paper-revision/projects/{self.pid}/comments/import", headers=HEADERS,
                                    json={"revision": 1, "backup_id": backup["id"]}).get_json()
        self.assertEqual((restored["imported"], restored["unplaced"]), (1, []))
        anchor = restored["project"]["comments"][0]["anchors"][0]
        unit = next(u for b in self.service.snapshot(self.pid)["blocks"] for u in b["units"] if u["id"] == anchor["unit_id"])
        self.assertEqual(unit["zh"][anchor["start"]:anchor["end"]], anchor["quote"])
        self.assertTrue(restored["project"]["comment_backups"][-1]["dismissed"])

    def test_exported_comments_reimport_on_an_edited_paper(self):
        path = self.local_paper()
        self.translated()
        self.comment()
        snap = self.service.snapshot(self.pid)
        other = next(b for b in snap["blocks"] if "preliminary" in b["masked"])
        self.service.add_comment(self.pid, {"revision": 1, "text": "这句要改", "scope": "paragraph", "anchors": [
            {"unit_id": other["units"][0]["id"], "block_id": other["id"], "side": "en", "start": 4, "end": 11, "quote": "results"}]})
        response = self.client.get(f"/api/paper-revision/projects/{self.pid}/comments/export", headers=HEADERS)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        bundle = response.get_json()
        self.assertEqual(len(bundle["comments"]), 2)
        self.assertEqual(self.service.read(self.pid)["comments_exported"], [c["id"] for c in self.service.read(self.pid)["comments"]])
        # Elsewhere the paper was edited: the commented results sentence was rewritten.
        path.write_bytes(SOURCE.replace("The results remain preliminary.", "The findings stay tentative.").encode("utf-8"))
        self.assertEqual(self.open()["status"], "synced")
        revision = self.service.read(self.pid)["revision"]
        result = self.client.post(f"/api/paper-revision/projects/{self.pid}/comments/import", headers=HEADERS,
                                  json={"revision": revision, "data": bundle}).get_json()
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["unplaced"], ["这句要改"])
        with self.assertRaises(ValueError):
            self.service.import_comments(self.pid, {"revision": revision, "data": {"format": "other"}})

    def test_retranslate_everything_clears_chinese_and_glossary(self):
        self.translated()
        p = self.client.post(f"/api/paper-revision/projects/{self.pid}/retranslate", headers=HEADERS, json={"revision": 1}).get_json()
        snap = self.service.snapshot(self.pid)
        self.assertEqual(p["history"][-1]["kind"], "clear_translation")
        self.assertFalse(any(u["zh"] for b in snap["blocks"] for u in b["units"]))
        self.assertNotIn("translation_glossary", snap)
        self.assertEqual(self.submit()["job"]["status"], "completed")
        self.assertTrue(self.service.snapshot(self.pid)["translated"])

    def test_streamed_translation_is_previewed_before_the_reply_ends(self):
        self.claude_backend()
        previews = []
        real_preview = self.service.preview
        self.service.preview = lambda pid, jid, done: previews.append(dict(done)) or real_preview(pid, jid, done)
        inner = self.service.claude_request
        def streaming(messages, model, effort, on_text=None):
            reply = inner(messages, model, effort)
            if on_text and json.loads(messages[-1]["content"])["task"] == "translate":
                for cut in range(0, len(reply), 7):
                    on_text(reply[:cut])
            return reply
        self.service.claude_request = streaming
        self.service.stream_flush_seconds = 0  # push every streamed sentence immediately
        p = self.submit_claude()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        streamed = [d for d in previews if len(d) < sum(len(b["units"]) for b in self.service.snapshot(self.pid)["blocks"])]
        self.assertTrue(streamed, "sentences were previewed while the reply was still streaming")
        self.assertTrue(all(v[1].startswith("中文") for d in streamed for v in d.values()))

    def make_multi_main_folder(self):
        folder = Path(self.temp.name) / "multi-main"
        (folder / "sections").mkdir(parents=True)
        (folder / "supp_tables").mkdir()
        (folder / "notes").mkdir()
        main ="\\documentclass{article}\n\\input{supp_tables/numbers}\n\\begin{document}\n\\input{sections/body}\n\\bibliography{refs}\n\\end{document}\n"
        files = {"main.tex": main,
                 "main_backup.tex": main.replace("sections/body", "sections/old"),
                 "supplementary.tex": "% \\documentclass{commented}\n\\documentclass{article}\n\\begin{document}\nSupplementary text here.\n\\end{document}\n",
                 "sections/body.tex": "Our method always improves accuracy. It is simple.\n",
                 "sections/old.tex": "An outdated draft paragraph.\n",
                 "supp_tables/numbers.tex": "\\newcommand{\\nImages}{1,610}\n",
                 "notes/snippet.tex": "% \\documentclass{article} only in a comment\nNot a main file.\n",
                 "refs.bib": "@article{prior,title={Prior work},year={2024}}\n"}
        for name, text in files.items():
            (folder / name).write_bytes(text.encode("utf-8"))
        return folder, files

    def test_folder_import_lets_the_user_choose_the_main_tex(self):
        folder, files = self.make_multi_main_folder()
        candidates = self.client.post("/api/paper-revision/local-candidates", headers=HEADERS, json={"path": str(folder)}).get_json()
        self.assertEqual(candidates["candidates"], ["main.tex", "supplementary.tex", "main_backup.tex"])
        with self.assertRaisesRegex(ValueError, "多个主 .tex 文件"):
            read_local_project(str(folder))
        imported, origin = read_local_project(str(folder), "main.tex")
        self.assertEqual(sorted(f["name"] for f in imported), ["main.tex", "refs.bib", "sections/body.tex", "supp_tables/numbers.tex"])
        self.assertEqual((origin["path"], origin["main"]), (str(folder.resolve()), "main.tex"))
        with self.assertRaises(ValueError):
            read_local_project(str(folder), "../outside.tex")
        response = self.client.post("/api/paper-revision/projects", headers=HEADERS,
                                    data={"local_path": str(folder), "main": "main.tex", "title": "主文件"})
        self.assertEqual(response.status_code, 201, response.get_json())
        self.pid = response.get_json()["id"]
        snap = self.service.snapshot(self.pid)
        self.assertEqual(sorted(f["name"] for f in snap["files"]), ["main.tex", "sections/body.tex", "supp_tables/numbers.tex"])
        # Re-reading on open follows the same main file (the backup is never pulled in).
        self.translated()
        (folder / "sections" / "body.tex").write_bytes(b"Our method may improve accuracy. It is simple.\n")
        opened = self.open()
        self.assertEqual((opened["status"], opened["sync"]["changed"]), ("synced", 1))
        self.assertNotIn("main_backup.tex", source_files(self.service.snapshot(self.pid)))

    def test_uploaded_folder_with_several_mains_requires_a_choice(self):
        _, files = self.make_multi_main_folder()
        def upload(extra):
            data = {"files": [(io.BytesIO(text.encode("utf-8")), name) for name, text in files.items()], **extra}
            return self.client.post("/api/paper-revision/projects", headers=HEADERS, data=data, content_type="multipart/form-data")
        refused = upload({})
        self.assertEqual(refused.status_code, 400)
        self.assertIn("多个主 .tex 文件", refused.get_json()["error"])
        created = upload({"main": "main.tex"})
        self.assertEqual(created.status_code, 201, created.get_json())
        snap = self.service.snapshot(created.get_json()["id"])
        self.assertEqual(sorted(f["name"] for f in snap["files"]), ["main.tex", "sections/body.tex", "supp_tables/numbers.tex"])
        self.assertEqual([b["name"] for b in snap["bibliography"]], ["refs.bib"])
        self.assertEqual(upload({"main": "missing.tex"}).status_code, 400)

    def test_existing_project_can_be_renamed_and_repointed_to_a_main_tex(self):
        folder, files = self.make_multi_main_folder()
        # An uploaded project (no local path) with the same body text, already translated and commented.
        upload = [{"name": "main.tex", "text": files["main.tex"]}, {"name": "sections/body.tex", "text": files["sections/body.tex"]},
                  {"name": "supp_tables/numbers.tex", "text": files["supp_tables/numbers.tex"]},
                  {"name": "refs.bib", "text": files["refs.bib"]}]
        self.pid = self.service.create("旧项目", upload)["id"]
        self.translated()
        self.comment()
        url = f"/api/paper-revision/projects/{self.pid}/source"
        renamed = self.client.post(url, headers=HEADERS, json={"revision": 1, "title": "  论文 · 正式版  "}).get_json()
        self.assertEqual((renamed["project"]["title"], renamed["sync"]), ("论文 · 正式版", None))
        self.assertEqual(self.client.post(url, headers=HEADERS, json={"revision": 1, "title": ""}).status_code, 400)
        ambiguous = self.client.post(url, headers=HEADERS, json={"revision": 1, "path": str(folder)})
        self.assertEqual(ambiguous.status_code, 400)
        self.assertIn("多个主 .tex 文件", ambiguous.get_json()["error"])
        # Same content: the source is attached without a new version, comments stay.
        same = self.client.post(url, headers=HEADERS, json={"revision": 1, "path": str(folder), "main": "main.tex"}).get_json()
        self.assertEqual((same["sync"], same["project"]["revision"], len(same["project"]["comments"])), (None, 1, 1))
        self.assertEqual(same["project"]["origin"]["main"], "main.tex")
        self.assertEqual(self.open()["status"], "in_sync")  # opening it now re-reads from disk
        # Switching to another main file re-reads; the comment goes to a backup first.
        self.comment()
        switched = self.client.post(url, headers=HEADERS, json={"revision": 1, "path": str(folder), "main": "main_backup.tex"}).get_json()
        self.assertEqual(switched["sync"]["files_changed"], ["main_backup.tex", "sections/old.tex", "main.tex", "sections/body.tex"])
        self.assertEqual(switched["project"]["comments"], [])
        self.assertEqual(switched["project"]["comment_backups"][-1]["reason"], "更换论文来源前的批注")
        snap = self.service.snapshot(self.pid)
        self.assertEqual(sorted(f["name"] for f in snap["files"]), ["main_backup.tex", "sections/old.tex", "supp_tables/numbers.tex"])
        self.assertEqual(self.open()["status"], "in_sync")

    def test_cancellation_discards_delayed_result_and_prevents_edit_race(self):
        self.model.release.clear()
        self.service.submit(self.pid, {"revision": 0, "confirm_usage": True})
        self.assertTrue(self.model.entered.wait(2))
        with self.assertRaises(Conflict):
            self.service.submit(self.pid, {"revision": 0, "confirm_usage": True})
        self.service.cancel(self.pid)
        self.model.release.set()
        self.service.executor.shutdown(wait=True)
        p = self.service.read(self.pid)
        self.assertEqual(p["revision"], 0)
        self.assertEqual(p["job"]["status"], "cancelled")

    def test_comments_during_translation_carry_into_the_translated_version(self):
        self.model.release.clear()
        job = self.service.submit(self.pid, {"revision": 0, "confirm_usage": True})["job"]
        self.assertTrue(self.model.entered.wait(2))
        snap = self.service.snapshot(self.pid)
        units = [(b, u) for b in snap["blocks"] for u in b["units"]]
        (b1, u1), (b2, u2) = units[0], units[1]
        # Two sentences already previewed: the first matches the final translation, the second won't.
        self.service.preview(self.pid, job["id"], {u1["id"]: [u1["masked"], "中文预览"], u2["id"]: [u2["masked"], "完全不同的预览"]})
        add = lambda side, b, u, start, end, quote: self.service.add_comment(self.pid, {
            "revision": 0, "text": "意见", "scope": "auto",
            "anchors": [{"side": side, "unit_id": u["id"], "block_id": b["id"], "start": start, "end": end, "quote": quote}]})
        add("zh", b1, u1, 0, 2, "中文")
        add("zh", b2, u2, 2, 4, "不同")
        add("en", b1, u1, 0, 3, u1["display_en"][:3])
        with self.assertRaises(Conflict):  # not yet shown in Chinese
            add("zh", *units[2], 0, 1, "中")
        removable = self.service.read(self.pid)["comments"][-1]["id"]
        self.service.delete_comment(self.pid, removable, 0)
        add("en", b1, u1, 0, 3, u1["display_en"][:3])
        self.model.release.set()
        p = self.wait()
        self.assertEqual((p["job"]["status"], p["revision"]), ("completed", 1), p["job"])
        self.assertEqual([(c["anchors"][0]["side"], c["anchors"][0]["quote"]) for c in p["comments"]],
                         [("zh", "中文"), ("en", u1["display_en"][:3])])
        self.assertEqual([a["quote"] for c in p["comment_backups"][-1]["comments"] for a in c["anchors"]], ["不同"])

    def test_cancelled_translation_backs_up_comments_on_unsaved_chinese(self):
        self.model.release.clear()
        job = self.service.submit(self.pid, {"revision": 0, "confirm_usage": True})["job"]
        self.assertTrue(self.model.entered.wait(2))
        block = self.service.snapshot(self.pid)["blocks"][0]
        unit = block["units"][0]
        self.service.preview(self.pid, job["id"], {unit["id"]: [unit["masked"], "中文预览"]})
        for side, quote in (("zh", "中文"), ("en", unit["display_en"][:2])):
            self.service.add_comment(self.pid, {"revision": 0, "text": "意见", "scope": "auto", "anchors": [
                {"side": side, "unit_id": unit["id"], "block_id": block["id"], "start": 0, "end": 2, "quote": quote}]})
        p = self.service.cancel(self.pid)
        self.model.release.set()
        self.service.executor.shutdown(wait=True)
        p = self.service.read(self.pid)
        self.assertEqual([c["anchors"][0]["side"] for c in p["comments"]], ["en"])
        self.assertEqual(p["comment_backups"][-1]["comments"][0]["anchors"][0]["quote"], "中文")

    def test_restore_creates_version_and_export_preserves_paths(self):
        self.translated()
        p = self.service.restore(self.pid, {"revision": 1, "target": 0})
        self.assertEqual(p["revision"], 2)
        self.assertFalse(self.service.snapshot(self.pid)["translated"])
        response = self.client.get(f"/api/paper-revision/projects/{self.pid}/export?revision=0")
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(archive.read("source/main.tex").decode(), SOURCE)
            self.assertIn("review/changes.patch", archive.namelist())

    def test_restart_marks_active_job_interrupted(self):
        p = self.service.read(self.pid)
        p["job"] = {"status": "running"}
        self.service.save(p)
        restarted = PaperRevision(self.service.root, codex_request=self.model, codex_status=lambda: {},
            llm_request=lambda *a: "", llm_presets=lambda: [])
        try:
            self.assertEqual(restarted.read(self.pid)["job"]["status"], "interrupted")
        finally:
            restarted.executor.shutdown()

    def test_http_guards_upload_and_json_contract(self):
        self.assertEqual(self.client.get('/paper-revision').status_code, 200)
        self.assertEqual(self.client.post('/api/paper-revision/projects', data={"source": "Hello."}).status_code, 403)
        self.assertEqual(self.client.get('/api/paper-revision/projects', headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.get('/api/paper-revision/projects', headers={"Host": "evil.example"}).status_code, 403)
        response = self.client.post('/api/paper-revision/projects', data={"files": (io.BytesIO(b"English text."), "sections/a.tex")}, headers=HEADERS)
        self.assertEqual(response.status_code, 201)
        response = self.client.post(f'/api/paper-revision/projects/{self.pid}/comments', json=[], headers=HEADERS)
        self.assertEqual(response.status_code, 400)
        response = self.client.post('/api/paper-revision/projects', data={"files": (io.BytesIO(b"English text."), "../a.tex")}, headers=HEADERS)
        self.assertEqual(response.status_code, 400)

    def test_api_backend_and_explicit_usage_gate(self):
        with self.assertRaises(ValueError):
            self.service.submit(self.pid, {"revision": 0})
        self.service.submit(self.pid, {"revision": 0, "backend": "api", "preset_id": "test", "confirm_usage": True})
        self.assertEqual(self.wait()["job"]["status"], "completed")

    def claude_backend(self):
        models = [{"id": "opus", "label": "Opus"}, {"id": "sonnet", "label": "Sonnet"}]
        calls = []
        def claude(messages, model, effort):
            calls.append((json.loads(messages[-1]["content"])["task"], model, effort))
            return self.model(messages)
        self.service.claude_request = claude
        self.service.claude_status = lambda **_: {"plan": True, "models": models,
            "efforts": ["low", "medium", "high", "xhigh", "max"],
            "defaults": {"translate": {"model": "sonnet", "effort": "medium"}, "revise": {"model": "opus", "effort": "high"}}}
        return calls

    def test_claude_code_backend_uses_plan_login_only(self):
        calls = self.claude_backend()
        status = self.service.claude_status
        self.service.claude_status = lambda **_: {**status(), "plan": False, "error": "Claude Code 当前不是 Claude 账号登录"}
        with self.assertRaisesRegex(ValueError, "不是 Claude 账号"):
            self.service.submit(self.pid, {"revision": 0, "backend": "claude", "confirm_usage": True})
        self.service.claude_status = status
        for bad in ({"claude_revise_model": "haiku-evil"}, {"claude_translate_effort": "ultra"}):
            with self.assertRaises(ValueError):
                self.service.submit(self.pid, {"revision": 0, "backend": "claude", "confirm_usage": True, **bad})
        # Defaults: Sonnet translates and reads the paper; Opus revises and audits.
        self.service.submit(self.pid, {"revision": 0, "backend": "claude", "confirm_usage": True})
        p = self.wait()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        stages = p["job"]["config"]["stages"]
        self.assertEqual(stages["translate"], {"engine": "claude", "model": "sonnet", "effort": "medium"})
        self.assertEqual(stages["revise"], {"engine": "claude", "model": "opus", "effort": "high"})
        self.assertTrue(calls and all(c[1:] == ("sonnet", "medium") for c in calls))
        calls.clear()
        self.comment()
        p = self.service.read(self.pid)
        self.service.submit(self.pid, {"revision": 1, "backend": "claude", "confirm_usage": True,
            "comment_ids": [c["id"] for c in p["comments"]], "claude_revise_effort": "max", "claude_translate_model": "opus"})
        self.assertEqual(self.wait()["job"]["status"], "completed")
        by_task = {}
        for task, model, effort in calls:
            by_task.setdefault(task, set()).add((model, effort))
        self.assertEqual(by_task["revise"], {("opus", "max")})
        self.assertEqual(by_task["audit"], {("opus", "max")})
        self.assertEqual(by_task["translate"], {("opus", "medium")})
        self.assertEqual(by_task["context"], {("opus", "medium")})
        listing = self.client.get("/api/paper-revision/projects", headers=HEADERS).get_json()
        self.assertTrue(listing["claude"]["plan"])

    def test_clipboard_chinese_text_is_annotate_only_and_exports_comments(self):
        text = "我们提出了一种新方法（PCM），效果提升 5%！\n\n第二段说明“限制。”还有待验证吗？"
        response = self.client.post("/api/paper-revision/projects", headers=HEADERS,
                                    data={"title": "剪贴板", "source": text, "translate": "1"})
        self.assertEqual(response.status_code, 201, response.get_json())
        self.pid = response.get_json()["id"]
        project = self.service.read(self.pid)
        self.assertEqual(project["language"], "zh")
        self.assertFalse(project["translate"])
        snap = self.service.snapshot(self.pid)
        self.assertEqual([u["display_en"] for b in snap["blocks"] for u in b["units"]],
                         ["我们提出了一种新方法（PCM），效果提升 5%！", "第二段说明“限制。”", "还有待验证吗？"])
        self.assertEqual(source_files(snap)["paper.tex"], text.replace("5%", "5\\%"))
        unit = snap["blocks"][0]["units"][0]
        start = unit["display_en"].index("新方法")
        self.service.add_comment(self.pid, {"revision": 0, "text": "说明新在哪里", "scope": "paragraph", "anchors": [
            {"side": "en", "unit_id": unit["id"], "block_id": snap["blocks"][0]["id"], "start": start, "end": start + 3, "quote": "新方法"}]})
        with self.assertRaisesRegex(ValueError, "中文原文"):
            self.service.submit(self.pid, {"revision": 0, "confirm_usage": True, "comment_ids": []})
        with self.assertRaisesRegex(ValueError, "不需要翻译"):
            self.service.set_translate(self.pid, {"revision": 0, "translate": True})
        exported = self.client.get(f"/api/paper-revision/projects/{self.pid}/comments/export", headers=HEADERS)
        disposition = exported.headers["Content-Disposition"]
        disposition.encode("latin-1")  # a real server writes headers as Latin-1; a Chinese title must not break it
        self.assertIn("filename*=UTF-8''%E5%89%AA%E8%B4%B4%E6%9D%BF-comments-v0.json", disposition)
        bundle = exported.get_json()
        self.assertEqual(bundle["comments"][0]["anchors"][0]["english_sentence"], unit["display_en"])
        # Re-parsing keeps the Chinese sentence split.
        self.assertFalse(self.service.reparse(self.pid, {"revision": 0})["changed"])

    def test_clipboard_plain_english_escapes_specials_but_keeps_latex(self):
        self.assertEqual(paper_revision.plain_to_latex("Costs $5 at 10% for A_b & C#."),
                         r"Costs \$5 at 10\% for A\_b \& C\#.")
        self.assertEqual(paper_revision.plain_to_latex(r"Gain 10% by \cite{x}."), r"Gain 10% by \cite{x}.")
        self.assertEqual(paper_revision.plain_to_latex("Inline $x$ math."), "Inline $x$ math.")
        self.assertEqual(paper_revision.detect_language(SOURCE), "en")
        response = self.client.post("/api/paper-revision/projects", headers=HEADERS,
                                    data={"source": "We gain 10% on this task. It costs $5."})
        project = self.service.read(response.get_json()["id"])
        self.assertEqual((project["language"], project["translate"]), ("en", True))
        snap = self.service.snapshot(project["id"])
        self.assertEqual([u["display_en"] for b in snap["blocks"] for u in b["units"]],
                         ["We gain 10% on this task. ", "It costs $5."])

    def test_untranslated_project_comments_on_english_and_translates_gaps_later(self):
        response = self.client.post("/api/paper-revision/projects", headers=HEADERS,
                                    data={"title": "No zh", "source": SOURCE, "translate": "0"})
        self.pid = response.get_json()["id"]
        self.assertFalse(self.service.read(self.pid)["translate"])
        with self.assertRaisesRegex(ValueError, "批注"):
            self.service.submit(self.pid, {"revision": 0, "confirm_usage": True, "comment_ids": []})
        snap = self.service.snapshot(self.pid)
        block = next(b for b in snap["blocks"] if "always improves" in b["masked"])
        unit = block["units"][0]
        start = unit["display_en"].index("always")
        with self.assertRaises(Conflict):
            self.service.add_comment(self.pid, {"revision": 0, "text": "x", "scope": "auto", "anchors": [
                {"side": "en", "unit_id": unit["id"], "block_id": block["id"], "start": start, "end": start + 6, "quote": "wrong!"}]})
        p = self.service.add_comment(self.pid, {"revision": 0, "text": "改为审慎表述", "scope": "auto", "anchors": [
            {"side": "en", "unit_id": unit["id"], "block_id": block["id"], "start": start, "end": start + 6, "quote": "always"}]})
        self.assertEqual(p["comments"][0]["anchors"][0]["side"], "en")
        p = self.submit()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        self.assertIn("未翻译", p["job"]["progress"])
        self.assertFalse([c for c in self.model.calls if c["task"] == "translate"])
        revise = next(c for c in self.model.calls if c["task"] == "revise")
        target = revise["comments"][0]["anchors"][0]
        self.assertEqual(target["selected_english"], "always")
        self.assertIn("always improves", target["english_sentence"])
        self.assertNotIn("selected_chinese", target)
        snap = self.service.snapshot(self.pid)
        self.assertFalse(snap["translated"])
        changed = next(b for b in snap["blocks"] if b["id"] == block["id"])["units"][0]
        self.assertIn("may improve", changed["en"])
        self.assertEqual(changed["lineage"][0]["en"], unit["display_en"])
        # Switching translation on later fills every missing sentence exactly once.
        self.client.post(f"/api/paper-revision/projects/{self.pid}/settings", headers=HEADERS,
                         json={"revision": 1, "translate": True})
        p = self.submit()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        translated = [u["id"] for c in self.model.calls if c["task"] == "translate" for u in c["units"]]
        self.assertEqual(len(translated), len(set(translated)))
        snap = self.service.snapshot(self.pid)
        self.assertTrue(snap["translated"])
        self.assertEqual(len(translated), sum(len(b["units"]) for b in snap["blocks"]))
        # Turning it off again keeps existing Chinese and skips translation of revised sentences.
        self.service.set_translate(self.pid, {"revision": 2, "translate": False})
        with self.assertRaises(ValueError):
            self.service.set_translate(self.pid, {"revision": 2, "translate": "no"})
        target = next(b for b in snap["blocks"] if "preliminary" in b["masked"])
        zh_unit = target["units"][0]
        self.service.add_comment(self.pid, {"revision": 2, "text": "更审慎", "scope": "paragraph", "anchors": [
            {"unit_id": zh_unit["id"], "block_id": target["id"], "start": 0, "end": 2, "quote": zh_unit["zh"][:2]}]})
        before = len([c for c in self.model.calls if c["task"] == "translate"])
        p = self.submit()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        self.assertEqual(len([c for c in self.model.calls if c["task"] == "translate"]), before)
        snap = self.service.snapshot(self.pid)
        self.assertTrue(any(u["zh"] for b in snap["blocks"] for u in b["units"]))
        revised = next(b for b in snap["blocks"] if b["id"] == target["id"])["units"][0]
        self.assertEqual(revised["zh"], "")
        self.assertEqual(revised["lineage"][0]["zh"], zh_unit["zh"])

    def test_multiple_overlapping_comments_and_draft_concurrency(self):
        self.translated()
        self.comment()
        self.comment()
        with self.assertRaises(Conflict):
            self.service.submit(self.pid, {"revision": 1, "confirm_usage": True, "comment_ids": []})
        self.assertEqual(self.submit()["revision"], 2)

    def test_cross_paragraph_selection_and_restore_with_pending_comments(self):
        self.translated()
        blocks = self.service.snapshot(self.pid)["blocks"][1:3]
        anchors = [{"block_id": b["id"], "unit_id": u["id"], "start": 0,
                    "end": len(u["zh"]), "quote": u["zh"]} for b in blocks for u in b["units"]]
        p = self.service.add_comment(self.pid, {"revision": 1, "text": "调整这两段的衔接", "scope": "paragraph", "anchors": anchors})
        self.assertEqual(scope_ids(p["comments"][0], blocks), {b["id"] for b in blocks})
        with self.assertRaises(Conflict):
            self.service.restore(self.pid, {"revision": 1, "target": 0})
        self.assertEqual(self.submit()["revision"], 2)

    def test_navigation_shell_is_public_but_cross_site_data_stays_blocked(self):
        headers = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "cross-site"}
        self.assertEqual(self.client.get('/paper-revision', headers=headers).status_code, 200)
        self.assertEqual(self.client.get('/api/paper-revision/projects', headers=headers).status_code, 403)
        self.assertEqual(self.client.get('/paper-revision', headers={**headers, "Host": "evil.example"}).status_code, 403)

    def test_translation_is_batched_and_only_changed_blocks_are_retranslated(self):
        self.translated()
        calls = [c for c in self.model.calls if c["task"] == "translate"]
        self.assertEqual(len(calls), 1)
        self.comment()
        self.submit()
        last = [c for c in self.model.calls if c["task"] == "translate"][-1]
        self.assertTrue(all(u["id"].startswith("p0002:") for u in last["units"]))

    def test_incremental_translation_lineage_and_overlapping_comments(self):
        self.translated()
        old = next(b for b in self.service.snapshot(self.pid)["blocks"] if "always improves" in b["masked"])
        first_comment = self.comment()["comments"][0]["id"]
        second_comment = self.comment()["comments"][1]["id"]
        p = self.submit()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        translate = [c for c in self.model.calls if c["task"] == "translate"][-1]
        self.assertEqual([u["id"] for u in translate["units"]], [old["units"][0]["id"]])
        self.assertEqual([u["id"] for u in translate["kept_translations"]], [old["units"][1]["id"]])
        block = next(b for b in self.service.snapshot(self.pid)["blocks"] if b["id"] == old["id"])
        changed, kept = block["units"]
        self.assertEqual(kept["zh"], old["units"][1]["zh"])
        self.assertNotIn("lineage", kept)
        entry = changed["lineage"][0]
        self.assertEqual((entry["en"], entry["zh"]), (old["units"][0]["display_en"], old["units"][0]["zh"]))
        self.assertEqual((entry["revision"], entry["from_revision"]), (2, 1))
        self.assertEqual(entry["comment_ids"], [first_comment, second_comment])

        original = self.service.codex_request
        def again(messages, *args):
            data = json.loads(messages[-1]["content"])
            if data["task"] != "revise":
                return original(messages, *args)
            self.model.calls.append(data)
            cid = data["comments"][0]["id"]
            target = next(b for b in data["blocks"] if b["id"] == old["id"])
            return json.dumps({"changes": [{"block_id": old["id"], "after": target["en"].replace("may improve", "can improve"),
                "comment_ids": [cid], "reason": "再次调整语气。"}], "outcomes": [{"comment_id": cid, "status": "applied", "reason": "已调整。"}]})
        self.service.codex_request = again
        self.service.add_comment(self.pid, {"revision": 2, "text": "再改", "scope": "paragraph", "anchors": [
            {"unit_id": changed["id"], "block_id": old["id"], "start": 0, "end": 2, "quote": changed["zh"][:2]}]})
        p = self.submit()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        revise = [c for c in self.model.calls if c["task"] == "revise"][-1]
        self.assertNotIn("lineage", json.dumps(revise["blocks"], ensure_ascii=False))
        stacked = next(b for b in self.service.snapshot(self.pid)["blocks"] if b["id"] == old["id"])["units"][0]["lineage"]
        self.assertEqual([e["revision"] for e in stacked], [3, 2])
        self.assertIn("may improve", stacked[0]["en"])
        self.assertIn("always improves", stacked[1]["en"])

    def test_local_main_follows_inputs_bibliography_and_preserves_originals(self):
        root = Path(self.temp.name) / "input"
        (root / "sections").mkdir(parents=True)
        main = r"\input{sections/intro}\bibliography{references}"
        bib = '@article{prior, title={A {Nested} Study}, author="Doe, Jane", year={2024}}\r\n'
        (root / "main.tex").write_text(main, encoding="utf-8")
        (root / "sections/intro.tex").write_text("English source.\\cite{prior}", encoding="utf-8")
        (root / "references.bib").write_bytes(b"\xef\xbb\xbf" + bib.encode())
        files, origin = read_local_project(str(root / "main.tex"))
        imported = self.service.create("Path", files, origin)
        snap = self.service.snapshot(imported["id"])
        self.assertEqual(snap["references"]["prior"]["file"], "references.bib")
        self.assertFalse(snap["import_warnings"])
        self.assertEqual(origin["kind"], "local_path")
        self.assertEqual((root / "main.tex").read_text(), main)
        response = self.client.get(f'/api/paper-revision/projects/{imported["id"]}/export')
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(archive.read("source/references.bib"), (root / "references.bib").read_bytes())
            self.assertIn("source/sections/intro.tex", archive.namelist())

    def test_local_paths_reject_missing_escape_and_non_source(self):
        root = Path(self.temp.name) / "input"
        root.mkdir()
        (root / "main.tex").write_text(r"\input{missing}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "缺少依赖"):
            read_local_project(str(root / "main.tex"))
        (root.parent / "outside.tex").write_text("Outside text.", encoding="utf-8")
        (root / "main.tex").write_text(r"\input{../outside}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "超出"):
            read_local_project(str(root / "main.tex"))
        with self.assertRaises(ValueError):
            read_local_project("relative/main.tex")
        with self.assertRaises(ValueError):
            read_local_project(r"\\server\paper\main.tex")

    def test_local_path_endpoint_and_missing_upload_dependencies(self):
        path = Path(self.temp.name) / "main.tex"
        path.write_text("Standalone English source.", encoding="utf-8")
        response = self.client.post('/api/paper-revision/projects', data={"local_path": str(path)}, headers=HEADERS)
        self.assertEqual(response.status_code, 201, response.json)
        response = self.client.post('/api/paper-revision/projects', data={"local_path": str(path), "source": "Other"}, headers=HEADERS)
        self.assertEqual(response.status_code, 400)
        imported = self.service.create("Missing", [{"name": "main.tex", "text": "English.\\input{missing}"}])
        self.assertTrue(self.service.snapshot(imported["id"])["import_warnings"])

    def test_directory_import_skips_hidden_and_unrelated_files(self):
        root = Path(self.temp.name) / "paper"
        (root / ".git").mkdir(parents=True)
        (root / "sub").mkdir()
        for name in ("main.tex", "sub/part.tex", ".git/ignored.tex", "picture.png", ".backup.tex", "sub/._part.tex", ".refs.bib"):
            (root / name).write_text("English text.", encoding="utf-8")
        files, _ = read_local_project(str(root))
        self.assertEqual({f["name"] for f in files}, {"main.tex", "sub/part.tex"})

    def test_upload_ignores_hidden_files(self):
        from io import BytesIO
        data = {"files": [(BytesIO(b"English text."), "main.tex"), (BytesIO(b"Hidden draft."), ".draft.tex"),
                          (BytesIO(b"Cache."), ".cache/x.tex")]}
        response = self.client.post('/api/paper-revision/projects', data=data, headers=HEADERS,
                                    content_type="multipart/form-data")
        self.assertEqual(response.status_code, 201, response.json)
        names = [f["name"] for f in self.service.snapshot(response.json["id"])["files"]]
        self.assertEqual(names, ["main.tex"])

    def test_bibtex_duplicates_nested_and_parentheses(self):
        text = '@article{a, title={Nested {Braces} Work}}\n@book(b, title="Work (one)")\n@article{a, title={duplicate}}'
        entries, warnings = bibliography_index([{"name": "r.bib", "text": text}])
        self.assertEqual(set(entries), {"a", "b"})
        self.assertIn('Work (one)', entries["b"]["raw"])
        self.assertTrue(warnings)

    def test_context_is_grounded_shared_cached_and_refreshed(self):
        self.translated()
        self.assertFalse([c for c in self.model.calls if c["task"] == "context"], "one-request translation skips reading")
        translated = next(c for c in self.model.calls if c["task"] == "translate")
        self.assertFalse(translated["paragraph_context"], "the whole paper is already in the request")
        # the glossary the translation reported is kept only where it is verifiable
        self.assertEqual(self.service.snapshot(self.pid)["translation_glossary"], {"accuracy": "准确率"})
        self.comment()
        p = self.submit()
        first = len([c for c in self.model.calls if c["task"] == "context"])
        self.assertGreater(first, 0)
        old_context = self.service.snapshot(self.pid)["context"]
        revision = next(c for c in self.model.calls if c["task"] == "revise")
        self.assertTrue(revision["paper_context"]["claims"])
        self.assertIn({"en": "accuracy", "zh": "准确率"}, revision["paper_context"]["terms"])
        self.assertEqual(len(revision["blocks"]), len(self.service.snapshot(self.pid)["blocks"]))
        self.assertTrue(p["history"][-1]["audit"])
        unit = next(b for b in self.service.snapshot(self.pid)["blocks"] if "preliminary" in b["masked"])["units"][0]
        self.service.add_comment(self.pid, {"revision": 2, "text": "再改", "scope": "paragraph", "anchors": [
            {"unit_id": unit["id"], "block_id": unit["id"].split(":")[0], "start": 0, "end": 2, "quote": unit["zh"][:2]}]})
        self.submit()
        context_calls = len([c for c in self.model.calls if c["task"] == "context"])
        self.assertEqual(context_calls, first + 1)  # unchanged chunks were reused, the edited one re-read
        self.assertNotEqual(old_context["fingerprint"], self.service.snapshot(self.pid)["context"]["fingerprint"])

    def test_fabricated_context_evidence_aborts_before_revision(self):
        self.translated()
        self.comment()
        self.model.mode = "bad_context"
        p = self.submit()
        self.assertEqual(p["revision"], 1)
        self.assertEqual(p["job"]["status"], "failed")
        self.assertFalse(any(c["task"] == "revise" for c in self.model.calls))

    def test_references_are_metadata_and_shared_with_context(self):
        p = self.service.create("Refs", [{"name": "main.tex", "text": SOURCE},
            {"name": "refs.bib", "text": '@article{prior,title={An earlier method},year={2024}}'}])
        self.pid = p["id"]
        self.translated()
        translated = next(c for c in self.model.calls if c["task"] == "translate")
        self.assertEqual(translated["references"]["entries"][0]["key"], "prior")
        self.comment()
        self.submit()
        contextual = next(c for c in self.model.calls if c["task"] == "context" and c["references"]["entries"])
        self.assertEqual(contextual["references"]["entries"][0]["key"], "prior")
        self.assertIn("not_paper_fulltext", contextual["references"]["kind"])

    def test_long_paper_uses_full_context_with_bounded_revision_groups(self):
        self.enterContext(mock.patch.dict(paper_revision.FULL_TEXT_CHARS, {"codex": 50_000}))  # force the batch fallback
        text = "\n\n".join("Our method always improves accuracy. " + "The discussion preserves scientific uncertainty. " * 30 for _ in range(65))
        self.pid = self.service.create("Long", [{"name": "main.tex", "text": text}])["id"]
        self.translated()
        snap = self.service.snapshot(self.pid)
        unit = snap["blocks"][30]["units"][0]
        self.service.add_comment(self.pid, {"revision": 1, "text": "审慎修改", "scope": "paragraph", "anchors": [
            {"unit_id": unit["id"], "block_id": snap["blocks"][30]["id"], "start": 0, "end": 2, "quote": unit["zh"][:2]}]})
        self.model.mode = "outside_scope"
        failed = self.submit()
        self.assertEqual(failed["job"]["status"], "failed")
        self.assertEqual(failed["revision"], 1)
        self.model.mode = "normal"
        p = self.submit()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        self.assertEqual(p["history"][-1]["context_mode"], "hierarchical")
        revision = next(c for c in self.model.calls if c["task"] == "revise")
        self.assertEqual(len(revision["blocks"]), 3)
        self.assertGreater(revision["paper_context"]["covered_sources"], 60)
        self.assertEqual(len(revision["outline"]), 65)
        self.assertTrue(revision["outline"][64]["opening"].startswith("Our method"))
        self.assertEqual(len(p["history"][-1]["changes"]), 1)

    def test_global_long_revision_aggregates_multiple_batches_without_scope_leak(self):
        self.enterContext(mock.patch.dict(paper_revision.FULL_TEXT_CHARS, {"codex": 50_000}))  # force the batch fallback
        source = "\n\n".join("Our method always improves accuracy. " + "The background matters. " * 70 for _ in range(40))
        self.pid = self.service.create("Global", [{"name": "main.tex", "text": source}])["id"]
        self.translated()
        self.comment("document")
        original_runner = self.service.codex_request
        def grouped(messages, *args):
            data = json.loads(messages[-1]["content"])
            if data["task"] != "revise":
                return original_runner(messages, *args)
            self.model.calls.append(data)
            cid = data["comments"][0]["id"]
            bid = data["allowed_blocks"][cid][0]
            block = next(b for b in data["blocks"] if b["id"] == bid)
            return json.dumps({"changes": [{"block_id": bid, "after": block["en"].replace("always improves", "may improve"),
                "comment_ids": [cid], "reason": "保持全文论述审慎。"}],
                "outcomes": [{"comment_id": cid, "status": "applied", "reason": "已处理本批段落。"}]})
        self.service.codex_request = grouped
        p = self.submit()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        groups = [c for c in self.model.calls if c["task"] == "revise"]
        self.assertGreater(len(groups), 1)
        ids = [bid for g in groups for v in g["allowed_blocks"].values() for bid in v]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(set(ids)), 40)
        self.assertEqual(len(p["history"][-1]["changes"]), len(groups))
        self.assertEqual(len(p["history"][-1]["outcomes"]), 1)

    def test_glossary_keeps_previous_translation_after_context_refresh(self):
        self.translated()
        self.comment()
        original = self.service.codex_request
        def changed_term(messages, *args):
            result = json.loads(original(messages, *args))
            if json.loads(messages[-1]["content"])["task"] == "context":
                for term in result["terms"]:
                    term["zh"] = "精确度"
            return json.dumps(result)
        self.service.codex_request = changed_term
        self.assertEqual(self.submit()["job"]["status"], "completed")
        context = self.service.snapshot(self.pid)["context"]
        self.assertEqual(next(t for t in context["terms"] if t["en"] == "accuracy")["zh"], "准确率")
        self.assertTrue(any(c["alternative"] == "精确度" for c in context["term_conflicts"]))


if __name__ == "__main__":
    unittest.main()
