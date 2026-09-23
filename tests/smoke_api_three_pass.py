#!/usr/bin/env python3
"""Explicit real-API smoke test. Not discovered by the default test suite."""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import web_panel
from codex_three_pass import ApiThreePassAnalysisManager, DirectApiExecutor, build_chunk_plan, infer_api_protocol


def wait_for_ready(session, timeout: float = 3600) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session.status == "ready" and not session.busy:
            return
        if session.status in {"failed", "cancelled"}:
            raise RuntimeError(session.error or f"Smoke test ended with {session.status}.")
        time.sleep(1)
    raise TimeoutError("API Three-Pass smoke test exceeded one hour.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paper", type=Path)
    parser.add_argument("--preset-id", required=True)
    parser.add_argument("--model")
    parser.add_argument("--confirm-usage", action="store_true")
    parser.add_argument("--confirm-chunked", action="store_true")
    args = parser.parse_args()
    if not args.confirm_usage:
        parser.error("Add --confirm-usage to acknowledge that this sends the paper to the configured API and incurs charges.")
    provider = web_panel.direct_api_provider(args.preset_id)
    if not provider:
        parser.error("The requested saved LLM preset does not exist.")
    source = args.paper.read_text(encoding="utf-8")
    plan, _chunks = build_chunk_plan(source, provider.get("contextWindow"), 12_000)
    if plan["requiresConfirmation"] and not args.confirm_chunked:
        parser.error(
            f"This paper needs {plan['chunkCount']} chunks and about {plan['estimatedCalls']} calls. "
            "Add --confirm-chunked to continue."
        )
    protocol = infer_api_protocol(provider["baseUrl"], provider.get("protocol") or "auto")
    with tempfile.TemporaryDirectory(prefix="paper-lens-api-smoke-") as temporary:
        manager = ApiThreePassAnalysisManager(
            web_panel.PROJECT_ROOT,
            web_panel.THREE_PASS_SKILL_PATH,
            web_panel.direct_api_provider,
            DirectApiExecutor(slot_factory=web_panel.direct_api_slot),
        )
        session = manager.create(
            root=Path(temporary) / "analyses",
            document_id="smoke-api",
            document_cache_key="",
            document_title=args.paper.stem,
            provider=provider,
            protocol=protocol,
            model=args.model or provider["model"],
            effort="high",
            language="zh-CN",
            focuses=["方法", "实验", "复现性"],
            custom_focus="",
            input_markdown=source,
            options={"reasoningEffort": "high", "maxOutputTokens": 12_000, "temperature": None},
            chunk_plan=plan,
        )
        wait_for_ready(session)
        manager.ask(session, "请用一句话说明这篇论文最重要的复现风险。")
        wait_for_ready(session)
        print(f"OK: backend=api protocol={protocol} model={session.model} calls≈{plan['estimatedCalls']}")
        print(session.report_path.read_text(encoding="utf-8")[:500])
        manager.pool.shutdown(wait=True, cancel_futures=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
