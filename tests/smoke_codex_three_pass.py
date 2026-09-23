#!/usr/bin/env python3
"""Explicit, quota-consuming smoke test for the real Codex App Server.

This file is intentionally not named ``test_*.py`` so normal test discovery
never executes it.
"""

from __future__ import annotations

import argparse
import tempfile
import time
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from codex_three_pass import TERMINAL_STATUSES, ThreePassAnalysisManager  # noqa: E402


def wait_until(predicate, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.5)
    raise TimeoutError("Timed out waiting for the real Codex smoke test.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a real Three-Pass analysis and follow-up through Codex App Server.")
    parser.add_argument("paper", type=Path, help="A readable local .md or .mmd paper")
    parser.add_argument("--model", help="Codex model ID; defaults to the App Server default")
    parser.add_argument("--effort", default="high", help="Reasoning effort (default: high)")
    parser.add_argument("--output", type=Path, help="Output directory; defaults to a temporary directory")
    parser.add_argument(
        "--confirm-usage",
        action="store_true",
        help="Required acknowledgement that this runs five real turns and consumes ChatGPT/Codex usage",
    )
    args = parser.parse_args()
    if not args.confirm_usage:
        parser.error("Pass --confirm-usage to acknowledge that this smoke test consumes ChatGPT/Codex usage.")
    paper = args.paper.expanduser().resolve()
    if paper.suffix.lower() not in {".md", ".mmd"} or not paper.is_file():
        parser.error("paper must be an existing .md or .mmd file")
    paper_text = paper.read_text(encoding="utf-8", errors="replace")
    if len("".join(paper_text.split())) < 300:
        parser.error("paper does not contain enough readable text")

    skill = PROJECT_ROOT / ".agents" / "skills" / "three-pass-paper-reader" / "SKILL.md"
    manager = ThreePassAnalysisManager(PROJECT_ROOT, skill)
    try:
        status = manager.status()
        if not status.get("chatgptAuthenticated"):
            raise RuntimeError(status.get("error") or "Codex is not authenticated with ChatGPT.")
        model = args.model or status.get("defaultModel")
        models = {item.get("id"): item for item in status.get("models", [])}
        if model not in models:
            raise RuntimeError(f"Model is not available: {model}")
        supported = models[model].get("supportedEfforts") or []
        effort = args.effort if not supported or args.effort in supported else models[model].get("defaultEffort") or supported[0]
        output = args.output.expanduser().resolve() if args.output else Path(tempfile.mkdtemp(prefix="paper-lens-three-pass-smoke-"))
        output.mkdir(parents=True, exist_ok=True)
        session = manager.create(
            root=output / "analyses",
            document_id="smoke-test",
            document_cache_key="",
            document_title=paper.stem,
            model=str(model),
            effort=str(effort),
            language="zh-CN",
            focuses=["方法", "实验", "复现性"],
            custom_focus="验证 Skill 加载、证据引用和报告完整性。",
            input_markdown=f"# {paper.stem}\n\n{paper_text}",
        )
        wait_until(lambda: session.status in TERMINAL_STATUSES, 60 * 60)
        if session.status != "ready":
            raise RuntimeError(session.error or f"Analysis ended as {session.status}.")
        manager.ask(session, "请用三点概括这篇论文最主要的复现风险。")
        wait_until(lambda: session.status in {"ready", "failed", "cancelled"} and not session.busy, 30 * 60)
        if session.status != "ready" or not session.messages:
            raise RuntimeError(session.error or "Follow-up did not complete.")
        print(f"PASS: report={session.report_path}")
        print(f"PASS: thread={session.thread_id}")
        print(f"PASS: follow-up={session.messages[-1]['answer'][:160]}")
        return 0
    finally:
        manager.runtime.stop()


if __name__ == "__main__":
    raise SystemExit(main())
