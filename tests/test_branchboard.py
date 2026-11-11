"""对话树（Branchboard）后端测试：用假的 Claude Code 进程验证分支 / 上下文 / 权限 / 持久化。

真实模型的「暗号」上下文隔离测试默认跳过，设置环境变量 BRANCHBOARD_LIVE=1 后运行（会消耗套餐额度）：
    BRANCHBOARD_LIVE=1 python -m unittest tests.test_branchboard
"""
import json
import os
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path

from flask import Flask

import branchboard
from branchboard import BranchboardService, create_branchboard_blueprint, highlight_message, parse_summary


class FakeStdin:
    def __init__(self, proc):
        self.proc, self.closed = proc, False

    def write(self, text):
        for line in text.splitlines():
            msg = json.loads(line)
            self.proc.sent.append(msg)
            self.proc.inbox.put(msg)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProc:
    """按脚本吐出 stream-json 事件。脚本项为 dict（直接输出）或 ("wait", subtype)：等待前端写回对应消息。"""

    def __init__(self, args, script, cwd):
        self.args, self.script, self.cwd = args, script, cwd
        self.sent, self.inbox = [], queue.Queue()
        self.stdin = FakeStdin(self)
        self.stderr = iter(())
        self.returncode = None

    @property
    def stdout(self):
        for item in self.script(self):
            if isinstance(item, tuple):
                while True:
                    msg = self.inbox.get(timeout=5)
                    if msg.get("type") == item[1]:
                        break
                continue
            yield json.dumps(item, ensure_ascii=False) + "\n"
        self.returncode = 0

    def wait(self):
        return 0

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


def text_turn(session_id, text):
    """一个最简单的成功回合：init → 文本增量 → result。"""
    def script(proc):
        yield {"type": "system", "subtype": "init", "session_id": session_id}
        yield {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text"}}}
        for part in (text[: len(text) // 2], text[len(text) // 2:]):
            yield {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": part}}}
        yield {"type": "result", "subtype": "success", "result": text, "session_id": session_id}
    return script


class Harness:
    def __init__(self, test, summarize=None):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.scripts, self.procs = [], []
        self.service = self.make_service(summarize)

    def make_service(self, summarize=None):
        def popen(args, **kwargs):
            proc = FakeProc(args, self.scripts.pop(0), kwargs.get("cwd"))
            self.procs.append(proc)
            return proc
        return BranchboardService(self.root / "runtime", finder=lambda: "claude.exe", popen=popen, summarize=summarize)

    def wait_status(self, tree_id, turn_id, statuses=("done", "error", "interrupted"), timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            turn = self.service.snapshot(tree_id)["turns"][turn_id]
            if turn["answer"]["status"] in statuses:
                return turn
            time.sleep(0.02)
        raise AssertionError(f"轮次没有在 {timeout}s 内进入 {statuses}")


SID = ["11111111-1111-4111-8111-11111111111{}".format(i) for i in range(10)]


class BranchboardTest(unittest.TestCase):
    def test_root_follow_and_highlight_fork_from_parent_session(self):
        h = Harness(self)
        tree = h.service.create_tree("", str(h.cwd))
        h.scripts.append(text_turn(SID[0], "根回答：柏拉图表征假说说的是……"))
        root = h.service.create_turn(tree["id"], {"question": "第一个问题"})
        root = h.wait_status(tree["id"], root["id"])
        self.assertEqual(root["answer"]["status"], "done")
        self.assertEqual(root["sessionId"], SID[0])
        self.assertEqual(root["answer"]["markdown"], "根回答：柏拉图表征假说说的是……")
        self.assertNotIn("--resume", h.procs[0].args)
        user = [m for m in h.procs[0].sent if m["type"] == "user"][0]
        self.assertEqual(user["message"]["content"], "第一个问题")
        self.assertEqual(h.procs[0].sent[0]["request"]["subtype"], "initialize")

        # follow：从父会话分叉
        h.scripts.append(text_turn(SID[1], "继续"))
        follow = h.service.create_turn(tree["id"], {"parentId": root["id"], "kind": "follow", "question": "然后呢"})
        h.wait_status(tree["id"], follow["id"])
        args = h.procs[1].args
        self.assertEqual(args[args.index("--resume") + 1], SID[0])
        self.assertIn("--fork-session", args)

        # 两次划词：同一父会话分叉，方向 下、上 交替并持久化
        dirs = []
        for i, sid in enumerate(SID[2:4]):
            h.scripts.append(text_turn(sid, "分支"))
            hl = h.service.create_turn(tree["id"], {"parentId": root["id"], "kind": "highlight", "question": f"这是什么{i}",
                                                    "quote": {"text": "柏拉图表征假说", "source": "full", "start": 3, "end": 10}})
            h.wait_status(tree["id"], hl["id"])
            dirs.append(hl["layout"]["dir"])
            args = h.procs[-1].args
            self.assertEqual(args[args.index("--resume") + 1], SID[0])
            user = [m for m in h.procs[-1].sent if m["type"] == "user"][0]
            self.assertEqual(user["message"]["content"], highlight_message("柏拉图表征假说", f"这是什么{i}"))
        self.assertEqual(dirs, [1, -1])
        self.assertTrue(user["message"]["content"].startswith("[针对上一条回答中的这段内容提问]\n> 柏拉图表征假说\n\n"))

        # 持久化在工作目录，且新进程能恢复
        path = h.cwd / ".branchboard" / f"{tree['id']}.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["order"]), 4)
        self.assertEqual(saved["turns"][hl["id"]]["quote"], {"text": "柏拉图表征假说", "source": "full", "start": 3, "end": 10})
        reloaded = h.make_service().snapshot(tree["id"])
        self.assertEqual(reloaded["turns"][root["id"]]["sessionId"], SID[0])
        self.assertEqual(reloaded["title"], "第一个问题")

    def test_parent_must_be_finished(self):
        h = Harness(self)
        tree = h.service.create_tree("t", str(h.cwd))
        gate = threading.Event()

        def slow(proc):
            yield {"type": "system", "subtype": "init", "session_id": SID[0]}
            gate.wait(5)
            yield {"type": "result", "subtype": "success", "result": "ok", "session_id": SID[0]}
        h.scripts.append(slow)
        root = h.service.create_turn(tree["id"], {"question": "q"})
        h.wait_status(tree["id"], root["id"], ("streaming",))
        with self.assertRaises(ValueError):
            h.service.create_turn(tree["id"], {"parentId": root["id"], "kind": "follow", "question": "太早了"})
        with self.assertRaises(ValueError):
            h.service.create_turn(tree["id"], {"question": "第二个根"})
        gate.set()
        h.wait_status(tree["id"], root["id"])

    def test_permission_round_trip_and_files_changed(self):
        h = Harness(self)
        tree = h.service.create_tree("t", str(h.cwd))
        target = str(h.cwd / "notes" / "a.md")

        def script(proc):
            yield {"type": "system", "subtype": "init", "session_id": SID[0]}
            yield {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tu1", "name": "Write",
                                                                 "input": {"file_path": target, "content": "x"}}]}}
            yield {"type": "control_request", "request_id": "perm-1", "request": {
                "subtype": "can_use_tool", "tool_name": "Write", "input": {"file_path": target, "content": "x"}, "tool_use_id": "tu1"}}
            yield ("wait", "control_response")
            yield {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "ok"}]}}
            yield {"type": "result", "subtype": "success", "result": "写好了", "session_id": SID[0]}
        h.scripts.append(script)
        turn = h.service.create_turn(tree["id"], {"question": "写个文件"})
        deadline = time.monotonic() + 5
        while not h.service.snapshot(tree["id"])["turns"][turn["id"]].get("permissions"):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        pending = h.service.snapshot(tree["id"])["turns"][turn["id"]]["permissions"][0]
        self.assertEqual(pending["tool"], "Write")
        h.service.reply_permission(tree["id"], turn["id"], "perm-1", True)
        done = h.wait_status(tree["id"], turn["id"])
        response = [m for m in h.procs[0].sent if m["type"] == "control_response"][0]["response"]
        self.assertEqual(response["request_id"], "perm-1")
        self.assertEqual(response["response"]["behavior"], "allow")
        self.assertEqual(done["answer"]["filesChanged"], ["notes/a.md"])
        self.assertTrue(done["answer"]["toolEvents"][0]["approved"])
        self.assertEqual(done["answer"]["markdown"], "写好了")  # 没有流式文本时用 result 兜底
        self.assertEqual(done.get("permissions"), [])

    def test_deny_permission_and_error_result(self):
        h = Harness(self)
        tree = h.service.create_tree("t", str(h.cwd))

        def script(proc):
            yield {"type": "system", "subtype": "init", "session_id": SID[0]}
            yield {"type": "control_request", "request_id": "p", "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "rm -rf x"}}}
            yield ("wait", "control_response")
            yield {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "usage limit reached"}
        h.scripts.append(script)
        turn = h.service.create_turn(tree["id"], {"question": "删"})
        deadline = time.monotonic() + 5
        while not h.service.snapshot(tree["id"])["turns"][turn["id"]].get("permissions"):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        h.service.reply_permission(tree["id"], turn["id"], "p", False)
        done = h.wait_status(tree["id"], turn["id"])
        response = [m for m in h.procs[0].sent if m["type"] == "control_response"][0]["response"]["response"]
        self.assertEqual(response["behavior"], "deny")
        self.assertEqual(done["answer"]["status"], "error")
        self.assertIn("额度", done["answer"]["error"])

    def test_interrupted_turns_on_reload_and_summary(self):
        calls = []

        def summarize(markdown):
            calls.append(markdown)
            return ["要点一", "要点二", "要点三"]
        h = Harness(self, summarize=summarize)
        tree = h.service.create_tree("t", str(h.cwd))
        long_text = "很长的回答。" * 100
        h.scripts.append(text_turn(SID[0], long_text))
        turn = h.service.create_turn(tree["id"], {"question": "q"})
        h.wait_status(tree["id"], turn["id"])
        deadline = time.monotonic() + 5
        while not h.service.snapshot(tree["id"])["turns"][turn["id"]]["answer"].get("summary"):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.assertEqual(calls, [long_text])
        events = [kind for _seq, kind, _data in h.service.hub.since(tree["id"], 0)]
        self.assertIn("turn.summary", events)

        # 上次运行中被打断的轮次，重开后标记为 interrupted
        path = h.cwd / ".branchboard" / f"{tree['id']}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["turns"][turn["id"]]["answer"]["status"] = "streaming"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(h.make_service().snapshot(tree["id"])["turns"][turn["id"]]["answer"]["status"], "interrupted")

    def test_settings_map_to_cli_flags(self):
        h = Harness(self)
        tree = h.service.create_tree("t", str(h.cwd), {"model": "sonnet", "effort": "low", "permissionMode": "acceptEdits"})
        h.scripts.append(text_turn(SID[0], "ok"))
        turn = h.service.create_turn(tree["id"], {"question": "q"})
        h.wait_status(tree["id"], turn["id"])
        args = h.procs[0].args
        for flag, value in (("--model", "sonnet"), ("--effort", "low"), ("--permission-mode", "acceptEdits"),
                            ("--permission-prompt-tool", "stdio"), ("--input-format", "stream-json")):
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertEqual(branchboard.clean_settings({"model": "gpt", "permissionMode": "bypassPermissions"}),
                         {"model": "", "effort": "", "permissionMode": "default"})

    def test_blueprint_and_validation(self):
        h = Harness(self)
        app = Flask(__name__)
        app.register_blueprint(create_branchboard_blueprint(h.service))
        client = app.test_client()
        self.assertEqual(client.post("/api/branchboard/trees", json={"cwd": str(h.root / "nope")}).status_code, 400)
        created = client.post("/api/branchboard/trees", json={"title": "我的树", "cwd": str(h.cwd)}).get_json()["tree"]
        listed = client.get("/api/branchboard/trees").get_json()["trees"]
        self.assertEqual([t["title"] for t in listed], ["我的树"])
        self.assertEqual(client.get("/api/branchboard/trees/" + "0" * 32).status_code, 404)
        self.assertEqual(client.post(f"/api/branchboard/trees/{created['id']}/turns", json={"question": " "}).status_code, 400)
        self.assertEqual(client.patch(f"/api/branchboard/trees/{created['id']}", json={"title": "改名"}).get_json()["tree"]["title"], "改名")
        self.assertEqual(client.delete(f"/api/branchboard/trees/{created['id']}").status_code, 200)
        self.assertFalse((h.cwd / ".branchboard" / f"{created['id']}.json").exists())

    def test_parse_summary(self):
        self.assertEqual(parse_summary('```json\n["a", "b"]\n```'), ["a", "b"])
        self.assertEqual(parse_summary("- 一\n- 二"), ["一", "二"])


@unittest.skipUnless(os.getenv("BRANCHBOARD_LIVE") == "1", "需要真实的 Claude Code 登录；设置 BRANCHBOARD_LIVE=1 运行")
class LiveContextIsolationTest(unittest.TestCase):
    """§8 验收：在分支 A 告诉模型一个暗号，主线和分支 B 里问它，它不应该知道。"""

    def test_secret_stays_in_branch(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cwd = Path(tmp.name) / "work"
        cwd.mkdir()
        service = BranchboardService(Path(tmp.name) / "runtime")
        tree = service.create_tree("live", str(cwd), {"model": "sonnet", "effort": "low"})
        tid = tree["id"]

        def ask(data):
            turn = service.create_turn(tid, data)
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                snap = service.snapshot(tid)["turns"][turn["id"]]
                if snap["answer"]["status"] not in ("pending", "streaming"):
                    self.assertEqual(snap["answer"]["status"], "done", snap["answer"].get("error"))
                    return snap
                time.sleep(0.5)
            self.fail("真实调用超时")

        root = ask({"question": "我们来测试对话分支。请只回答一句：好的，我准备好了。不要使用任何工具。"})
        quote = root["answer"]["markdown"].strip()[:10] or "好的"
        secret = "紫色长颈鹿-7319"
        branch_a = ask({"parentId": root["id"], "kind": "highlight", "quote": {"text": quote, "source": "full"},
                        "question": f"请记住这个暗号：{secret}。只回答：记住了。不要使用任何工具。"})
        ask_secret = "我之前有没有告诉过你一个暗号？如果有，请原样说出；如果没有，只回答：没有。不要使用任何工具，不要读取文件。"
        in_a = ask({"parentId": branch_a["id"], "kind": "follow", "question": ask_secret})
        main = ask({"parentId": root["id"], "kind": "follow", "question": ask_secret})
        branch_b = ask({"parentId": root["id"], "kind": "highlight", "quote": {"text": quote, "source": "full"}, "question": ask_secret})
        self.assertIn("7319", in_a["answer"]["markdown"])
        self.assertNotIn("7319", main["answer"]["markdown"])
        self.assertNotIn("7319", branch_b["answer"]["markdown"])


if __name__ == "__main__":
    unittest.main()
