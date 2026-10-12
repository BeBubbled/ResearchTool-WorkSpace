"""Disposable local browser fixture; never sends data to the real ChatGPT.

Run: python tests/prompt_browser_harness.py
Open /fixture?case=textarea, contenteditable, paste, existing, filename,
current-composer, composer-fallback, or hidden-editor.
The fixture runs the production ChatGPT content script against real DOM/File APIs.
"""
import sys
import tempfile
from pathlib import Path

from flask import Flask, send_file

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prompt_workbench import PromptWorkbench, create_prompt_blueprint

app = Flask(__name__, static_folder=str(ROOT / "static"), template_folder=str(ROOT / "templates"))
temporary = tempfile.TemporaryDirectory(prefix="prompt-browser-test-")
app.register_blueprint(create_prompt_blueprint(PromptWorkbench(Path(temporary.name), ROOT / "browser_extension/prompt_bridge")))


@app.get("/fixture")
def fixture():
    return send_file(ROOT / "tests/fixtures/prompt_bridge.html")


@app.get("/fixture/chatgpt.js")
def content_script():
    return send_file(ROOT / "browser_extension/prompt_bridge/chatgpt.js")


if __name__ == "__main__":
    try:
        app.run(host="127.0.0.1", port=8792, use_reloader=False)
    finally:
        temporary.cleanup()
