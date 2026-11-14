"""Local browser fixture using production routes/UI and a fake model; never consumes quota."""
import sys
import tempfile
import json
import os
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask, render_template
from test_paper_revision import FakeGoogle, FakeModel, SOURCE
from researchtool.paper_revision import PaperRevision, create_paper_revision_blueprint

ROOT = Path(__file__).resolve().parents[1]
app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))
storage = tempfile.TemporaryDirectory(prefix="paper-revision-browser-")
class DemoModel(FakeModel):
    def __call__(self, messages, *args):
        data = json.loads(messages[-1]["content"])
        if data["task"] != "translate":
            return super().__call__(messages, *args)
        time.sleep(float(os.getenv("HARNESS_TRANSLATE_DELAY", "0.4")))  # make live preview visible
        replacements = {
            "Introduction": "引言", "Results": "结果",
            "Our method always improves accuracy by ": "我们的方法始终将准确率提高 ",
            "Our method may improve accuracy by ": "我们的方法可能将准确率提高 ",
            " percent.": " 个百分点。", "It uses ": "该方法使用 ", " and ": " 和 ",
            "The second paragraph discusses the comparison.": "第二段讨论对照方法。",
            "We preserve uncertainty.": "我们保留结论的不确定性。",
            "Another paragraph provides context.": "另一段提供背景信息。",
            "The results remain preliminary.": "目前的结果仍是初步的。",
        }
        translated = []
        for unit in data["units"]:
            text = unit["en"]
            for en, zh in replacements.items():
                text = text.replace(en, zh)
            translated.append({"id": unit["id"], "zh": text})
        return json.dumps({"units": translated})

model = DemoModel()
session_blocks = {}


def demo_session(messages, model_name, effort, *, session_id, resume, cwd, name):
    """Simulated saved Claude Code session: remembers the paper across rounds like the real one."""
    body = json.loads(messages[-1]["content"].split("\n\n", 1)[1])
    for block in body.get("paper", []) + body.get("updated_blocks", []):
        session_blocks.setdefault(session_id, {})[block["id"]] = block
    request = {**body, "blocks": list(session_blocks.get(session_id, {}).values())}
    return model([{"role": "user", "content": json.dumps(request, ensure_ascii=False)}])


service = PaperRevision(storage.name, codex_request=model,
    codex_status=lambda: {"chatgptAuthenticated": True, "defaultModel": "LOCAL-FAKE-MODEL", "defaultReasoningEffort": "high"},
    llm_request=lambda preset, messages: model(messages), llm_presets=lambda: [{"id": "fake", "model": "LOCAL-FAKE-MODEL"}],
    google=FakeGoogle(key="harness-key"),
    claude_request=lambda messages, *args: model(messages),
    claude_session_request=demo_session,
    claude_status=lambda **_: {"available": True, "loggedIn": True, "plan": True, "subscriptionType": "fake",
                               "models": [{"id": "opus", "label": "Opus · 质量优先"}, {"id": "sonnet", "label": "Sonnet · 更快、更省额度"}],
                               "efforts": ["low", "medium", "high", "xhigh", "max"],
                               "defaults": {"translate": {"model": "sonnet", "effort": "medium"}, "revise": {"model": "opus", "effort": "high"}}})
app.register_blueprint(create_paper_revision_blueprint(service))
project = service.create("交互验收 · 模拟论文", [{"name": "main.tex", "text": SOURCE}])
fixture = Path(storage.name) / "input"
(fixture / "sections").mkdir(parents=True)
(fixture / "main.tex").write_text("\\documentclass{article}\n\\begin{document}\n\\input{sections/body}\n\\bibliography{references}\n\\end{document}\n", encoding="utf-8")
(fixture / "sections/body.tex").write_text(SOURCE.split(r"\begin{document}")[1].split(r"\end{document}")[0], encoding="utf-8")
(fixture / "references.bib").write_text('@article{prior,title={Earlier accuracy study},author={Doe, Jane},year={2024}}', encoding="utf-8")

@app.get('/paper-revision-drag-fixture')
def drag_fixture():
    fixture_json = json.dumps({"main.tex": SOURCE, "references.bib": (fixture / "references.bib").read_text(encoding="utf-8")}).replace('<', '\\u003c')
    widget = '<button id="testDrop" style="position:fixed;right:20px;bottom:20px;z-index:20">投递测试文件拖放事件（含 .bib）</button>'
    script = '<script>document.getElementById("testDrop").addEventListener("click", () => {const data=new DataTransfer(); for (const [name,text] of Object.entries(' + fixture_json + ')) data.items.add(new File([text], name, {type:"text/plain"})); document.getElementById("dropZone").dispatchEvent(new DragEvent("drop",{dataTransfer:data,bubbles:true,cancelable:true}));});</script>'
    return render_template('paper_revision.html').replace('</body>', widget + script + '</body>')
if __name__ == "__main__":
    try:
        print("IMPORT_FIXTURE=" + str(fixture / "main.tex"), flush=True)
        app.run(host="127.0.0.1", port=8793, threaded=True, use_reloader=False)
    finally:
        service.executor.shutdown(wait=True)
        storage.cleanup()
