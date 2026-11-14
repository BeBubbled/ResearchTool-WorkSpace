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

    def test_steps_record_thinking_and_tools_in_order(self):
        h = Harness(self)
        tree = h.service.create_tree("t", str(h.cwd))
        gate = threading.Event()

        def ev(inner):
            return {"type": "stream_event", "event": inner}

        def script(proc):
            yield {"type": "system", "subtype": "init", "session_id": SID[0]}
            yield ev({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}})
            yield ev({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "先看看"}})
            yield ev({"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "x"}})
            yield ev({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "文件。"}})
            gate.wait(5)  # 停在思考中：这时思考块还开着
            yield ev({"type": "content_block_stop", "index": 0})
            yield ev({"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tu1", "name": "Read", "input": {}}})
            yield ev({"type": "content_block_stop", "index": 1})
            yield {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tu1", "name": "Read",
                                                                 "input": {"file_path": str(h.cwd / "a.md")}}]}}
            yield {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "内容", "is_error": True}]}}
            yield ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}})
            yield ev({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "答案"}})
            yield {"type": "result", "subtype": "success", "result": "答案", "session_id": SID[0]}
        h.scripts.append(script)
        turn = h.service.create_turn(tree["id"], {"question": "q"})
        deadline = time.monotonic() + 5
        while True:
            answer = h.service.snapshot(tree["id"])["turns"][turn["id"]]["answer"]
            if answer.get("steps") and answer["steps"][0]["text"] == "先看看文件。":
                break
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.assertIsNone(answer["steps"][0]["end"])
        self.assertTrue(answer["startedAt"])
        gate.set()
        done = h.wait_status(tree["id"], turn["id"])["answer"]
        steps = done["steps"]
        self.assertEqual([s["kind"] for s in steps], ["thinking", "tool"])  # 流里先到的工具块和完整消息合成一项
        self.assertTrue(steps[0]["end"])
        self.assertEqual((steps[1]["name"], steps[1]["hint"], steps[1]["isError"]), ("Read", str(h.cwd / "a.md"), True))
        self.assertTrue(steps[1]["end"] and done["endedAt"])
        self.assertEqual(done["markdown"], "答案")
        saved = json.loads((h.cwd / ".branchboard" / f"{tree['id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["turns"][turn["id"]]["answer"]["steps"], steps)

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

    def test_new_chat_uses_default_cwd_and_auto_titles_on_first_and_third_question(self):
        """新聊天不用选目录；首问后自动命名，第三问时按前三个问题更新，手动改名后不再变。"""
        h = Harness(self)
        calls = []

        def titler(questions):
            calls.append(list(questions))
            return f"「标题 {len(calls)}」。\n多余的解释"
        h.service.titler = titler

        def wait_title(tid, title):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if h.service.snapshot(tid)["title"] == title:
                    return
                time.sleep(0.02)
            self.fail(f"标题没有变成 {title}：{h.service.snapshot(tid)['title']}")

        tree = h.service.create_tree("", "")
        tid = tree["id"]
        self.assertEqual(tree["title"], "新聊天")
        self.assertEqual(Path(tree["cwd"]), (h.root / "runtime" / "chats").resolve())
        self.assertTrue(Path(tree["cwd"]).is_dir())

        h.scripts.append(text_turn(SID[0], "答一"))
        root = h.service.create_turn(tid, {"question": "什么是表征学习\n补充说明"})
        h.wait_status(tid, root["id"])
        wait_title(tid, "标题 1")
        self.assertEqual(calls, [["什么是表征学习\n补充说明"]])

        h.scripts.append(text_turn(SID[1], "答二"))
        second = h.service.create_turn(tid, {"parentId": root["id"], "kind": "follow", "question": "第二问"})
        h.wait_status(tid, second["id"])
        self.assertEqual(len(calls), 1)  # 第二个问题不改名

        h.scripts.append(text_turn(SID[2], "答三"))
        third = h.service.create_turn(tid, {"parentId": second["id"], "kind": "follow", "question": "第三问"})
        h.wait_status(tid, third["id"])
        wait_title(tid, "标题 2")
        self.assertEqual(calls[1], ["什么是表征学习\n补充说明", "第二问", "第三问"])
        self.assertEqual(h.service.list_trees()[0]["title"], "标题 2")

        h.scripts.append(text_turn(SID[3], "答四"))
        fourth = h.service.create_turn(tid, {"parentId": third["id"], "kind": "follow", "question": "第四问"})
        h.wait_status(tid, fourth["id"])
        self.assertEqual(len(calls), 2)

        # 重新加载后仍记得是自动命名；手动改名之后不再自动改
        self.assertEqual(h.make_service().tree(tid)["titleSource"], "auto")
        self.assertEqual(h.service.update_tree(tid, {"title": "我起的名字"})["titleSource"], "manual")

        named = h.service.create_tree("手动标题", str(h.cwd))
        h.scripts.append(text_turn(SID[4], "答"))
        turn = h.service.create_turn(named["id"], {"question": "一个问题"})
        h.wait_status(named["id"], turn["id"])
        self.assertEqual(h.service.snapshot(named["id"])["title"], "手动标题")
        self.assertEqual(len(calls), 2)

    def test_title_falls_back_to_first_line_and_multi_question_crosses_third(self):
        h = Harness(self)
        tree = h.service.create_tree("", str(h.cwd))
        tid = tree["id"]
        h.scripts.append(text_turn(SID[0], "答一"))
        root = h.service.create_turn(tid, {"question": "第一个问题\n第二行"})
        h.wait_status(tid, root["id"])
        self.assertEqual(h.service.snapshot(tid)["title"], "第一个问题")  # 没有命名模型：用首问首行

        calls = []
        h.service.titler = lambda questions: calls.append(list(questions)) or "合并后的标题"
        h.scripts.append(text_turn(SID[1], "答二"))
        multi = h.service.create_turn(tid, {"questions": [
            {"sourceId": root["id"], "text": "问二"}, {"sourceId": root["id"], "text": "", "quote": {"text": "答一"}}]})
        h.wait_status(tid, multi["id"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and h.service.snapshot(tid)["title"] != "合并后的标题":
            time.sleep(0.02)
        self.assertEqual(h.service.snapshot(tid)["title"], "合并后的标题")  # 一次问两个：越过第三个问题
        self.assertEqual(calls, [["第一个问题\n第二行", "问二", "答一"]])
        self.assertEqual(branchboard.clean_title("**标题：“深度学习入门”**\n解释"), "深度学习入门")
        self.assertEqual(branchboard.clean_title("  \n"), "")
        self.assertEqual(branchboard.clean_title("一句话解释对比学习</br>"), "一句话解释对比学习")

    def test_multi_source_turn_forks_latest_source_and_injects_other_branches(self):
        """在多张卡片里划词、攒起来一次问出：从最近的来源分叉，其他分支的内容作为引用材料拼进消息。"""
        h = Harness(self)
        tree = h.service.create_tree("t", str(h.cwd))
        tid = tree["id"]

        def run(data, sid, text):
            h.scripts.append(text_turn(sid, text))
            turn = h.service.create_turn(tid, data)
            return h.wait_status(tid, turn["id"])
        root = run({"question": "根问题"}, SID[0], "根回答，提到甲和乙。")
        a = run({"questions": [{"sourceId": root["id"], "text": "甲是什么", "quote": {"text": "甲"}}]}, SID[1], "分支 A：甲的解释，暗号 7319。")
        b = run({"questions": [{"sourceId": root["id"], "text": "乙是什么", "quote": {"text": "乙"}}]}, SID[2], "分支 B：乙的解释。")
        self.assertEqual((a["kind"], a["layout"]["dir"]), ("highlight", 1))
        self.assertEqual((b["kind"], b["layout"]["dir"]), ("highlight", -1))

        multi = run({"questions": [
            {"sourceId": a["id"], "text": "这和乙有什么关系", "quote": {"text": "暗号 7319"}},
            {"sourceId": b["id"], "text": "", "quote": {"text": "乙的解释"}},
            {"sourceId": root["id"], "text": "顺便总结一下", "quote": {"text": "根回答", "source": "question"}},
        ]}, SID[3], "合并回答")
        self.assertEqual(multi["kind"], "multi")
        self.assertEqual(multi["parentId"], b["id"])  # 最近创建的来源
        self.assertEqual([q["sourceId"] for q in multi["questions"]], [a["id"], b["id"], root["id"]])
        self.assertEqual(multi["questions"][2]["quote"]["source"], "question")
        args = h.procs[-1].args
        self.assertEqual(args[args.index("--resume") + 1], SID[2])  # 从 B 的会话分叉
        prompt = [m for m in h.procs[-1].sent if m["type"] == "user"][0]["message"]["content"]
        # A 不在 B 的上下文里 → 它的提问和回答被拼进来；B 和根已在上下文里，不重复拼
        self.assertIn("[以下内容来自其他对话分支，此前的对话里没有它们]", prompt)
        self.assertIn("### 第 2 轮\n提问：甲是什么", prompt)
        self.assertIn("分支 A：甲的解释，暗号 7319。", prompt)
        self.assertNotIn("分支 B：乙的解释。\n", prompt.split("我有")[0])
        self.assertIn("我有 3 个相关的问题，请逐一回答", prompt)
        self.assertIn("## 问题 1\n[针对第 2 轮的回答中的这段内容提问]\n> 暗号 7319\n\n这和乙有什么关系", prompt)
        self.assertIn("## 问题 2\n[针对第 3 轮的回答中的这段内容提问]\n> 乙的解释\n\n（请结合上面这段内容展开说明。）", prompt)
        self.assertIn("## 问题 3\n[针对第 1 轮的提问中的这段内容提问]", prompt)

        # 之后在合并卡片上继续：A 已经拼进过上下文，不会再次注入
        follow = run({"questions": [{"sourceId": multi["id"], "text": "再说说", }]}, SID[4], "好")
        self.assertEqual(follow["kind"], "follow")
        self.assertNotIn("layout", follow)  # 第一个普通追问和父卡片同一行
        prompt = [m for m in h.procs[-1].sent if m["type"] == "user"][0]["message"]["content"]
        self.assertEqual(prompt, "再说说")
        self.assertEqual(set(branchboard.context_ids(h.service.tree(tid), follow["id"])),
                         {follow["id"], multi["id"], a["id"], root["id"], b["id"]})

        # 同一张卡片上加两个问题小框：两个普通问题 → 一张多问题卡片，单独开一行
        two = run({"questions": [{"sourceId": root["id"], "text": "问一"}, {"sourceId": root["id"], "text": "问二"}]}, SID[5], "两个回答")
        self.assertEqual(two["kind"], "multi")
        self.assertIn("layout", two)
        prompt = [m for m in h.procs[-1].sent if m["type"] == "user"][0]["message"]["content"]
        self.assertTrue(prompt.startswith("我有 2 个相关的问题"))
        self.assertNotIn("其他对话分支", prompt)

    def test_question_validation(self):
        h = Harness(self)
        tree = h.service.create_tree("t", str(h.cwd))
        h.scripts.append(text_turn(SID[0], "根"))
        root = h.service.create_turn(tree["id"], {"question": "根"})
        h.wait_status(tree["id"], root["id"])
        bad = [
            {"questions": [{"sourceId": root["id"], "text": "", "quote": {"text": "根"}}]},  # 全部留空
            {"questions": []},
            {"questions": [{"sourceId": root["id"], "text": "x"}, {"sourceId": None, "text": "y"}]},
            {"questions": [{"sourceId": "nope", "text": "x"}]},
        ]
        for data in bad:
            with self.assertRaises((ValueError, KeyError), msg=data):
                h.service.create_turn(tree["id"], data)

    def test_full_version_classify_and_rewrite(self):
        calls = []

        def complete(messages, model, effort):
            calls.append((messages, model, effort))
            if "JSON" in messages[0]["content"]:
                return '```json\n{"%s": "correct", "bogus": "extend", "%s": "weird"}\n```' % (keys[0], keys[1])
            return "```markdown\n语言模型只学到粗粒度的关系结构。\n```"
        h = Harness(self)
        h.service.complete = complete
        tree = h.service.create_tree("t", str(h.cwd))
        tid = tree["id"]

        def run(data, sid, text):
            h.scripts.append(text_turn(sid, text))
            turn = h.service.create_turn(tid, data)
            return h.wait_status(tid, turn["id"])
        root = run({"question": "介绍"}, SID[0], "第一段。\n\n语言模型里有视觉知识，规模越大越强。")
        a = run({"questions": [{"sourceId": root["id"], "text": "准确吗", "quote": {"text": "语言模型里有视觉知识"}}]}, SID[1], "说得偏强了：只有粗粒度结构。")
        b = run({"questions": [{"sourceId": root["id"], "text": "第一段指什么", "quote": {"text": "第一段"}}]}, SID[2], "指开头。")
        keys = [f"{a['id']}:0", f"{b['id']}:0"]
        refs = [{"key": keys[0], "childId": a["id"], "qi": 0}, {"key": keys[1], "childId": b["id"], "qi": 0}]

        # 分类：一次调用；只接受已知的键和类别
        self.assertEqual(h.service.full_version_classify(tid, root["id"], refs), {keys[0]: "correct"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:], ("sonnet", "low"))
        self.assertIn("被划中的原文：语言模型里有视觉知识", calls[0][0][1]["content"])

        # 改写：每段一次调用，去掉 ``` 包裹；同样的内容第二次走缓存
        group = {"block": 1, "paragraph": "语言模型里有视觉知识，规模越大越强。", "items": [refs[0]]}
        result = h.service.full_version_rewrite(tid, root["id"], [group])
        self.assertEqual(result, {"rewrites": [{"nodeId": root["id"], "block": 1, "text": "语言模型只学到粗粒度的关系结构。"}],
                                  "called": 1, "cached": 0})
        self.assertIn("## 修正信息（第 2 轮）", calls[1][0][1]["content"])
        self.assertIn("说得偏强了：只有粗粒度结构。", calls[1][0][1]["content"])
        again = h.service.full_version_rewrite(tid, root["id"], [group])
        self.assertEqual((again["called"], again["cached"]), (0, 1))
        self.assertEqual(len(calls), 2)
        # 缓存存在树文件里（重启后仍有效），但不随快照发给前端
        saved = json.loads((h.cwd / ".branchboard" / f"{tid}.json").read_text(encoding="utf-8"))
        self.assertEqual(list(saved["fullVersionCache"].values()), ["语言模型只学到粗粒度的关系结构。"])
        self.assertNotIn("fullVersionCache", h.service.snapshot(tid))
        self.assertEqual(h.make_service().full_version_rewrite.__self__.tree(tid)["fullVersionCache"], saved["fullVersionCache"])

        # 校验：段落必须出自原回答；分支必须划在这一轮上
        with self.assertRaises(ValueError):
            h.service.full_version_rewrite(tid, root["id"], [{**group, "paragraph": "伪造的段落"}])
        with self.assertRaises(ValueError):
            h.service.full_version_classify(tid, a["id"], refs)
        with self.assertRaises(KeyError):
            h.service.full_version_classify(tid, root["id"], [{"childId": "nope", "qi": 0}])

        # 没有模型时：接口返回 503
        h.service.complete = None
        app = Flask(__name__)
        app.register_blueprint(create_branchboard_blueprint(h.service))
        resp = app.test_client().post(f"/api/branchboard/trees/{tid}/turns/{root['id']}/full-version/classify", json={"items": refs})
        self.assertEqual(resp.status_code, 503)

    def test_merge_subtree_updates_card_and_context(self):
        h = Harness(self)
        tree = h.service.create_tree("t", str(h.cwd))
        tid = tree["id"]

        def run(data, sid, text):
            h.scripts.append(text_turn(sid, text))
            turn = h.service.create_turn(tid, data)
            return h.wait_status(tid, turn["id"])
        root = run({"question": "根"}, SID[0], "视觉知识随规模增强。")
        a = run({"questions": [{"sourceId": root["id"], "text": "准确吗", "quote": {"text": "视觉知识"}}]}, SID[1], "只有粗粒度结构。")
        g = run({"questions": [{"sourceId": a["id"], "text": "可靠吗", "quote": {"text": "粗粒度"}}]}, SID[2], "也不可靠。")
        main = run({"questions": [{"sourceId": root["id"], "text": "下一个问题"}]}, SID[3], "主线")
        self.assertEqual(branchboard.subtree_members(h.service.tree(tid), root["id"]), [a["id"], g["id"]])

        # 只能合并自己子树里的卡片
        with self.assertRaises(ValueError):
            h.service.merge_subtree(tid, root["id"], {"markdown": "x", "absorbed": [main["id"]]})
        with self.assertRaises(ValueError):
            h.service.merge_subtree(tid, root["id"], {"markdown": " ", "absorbed": [a["id"]]})

        # 先合并中间的 a（收起 g），再合并根（收起 a、g）：嵌套的合并被并进外层
        h.service.merge_subtree(tid, a["id"], {"markdown": "只有粗粒度结构。\n\n> 后来的修正：也不可靠。", "absorbed": [g["id"]]})
        merged = h.service.merge_subtree(tid, root["id"], {"markdown": "视觉知识只在粗粒度上随规模增强，而且并不可靠。",
                                                           "absorbed": [g["id"], a["id"]], "stats": {"correct": 2, "bogus": "x"}})
        self.assertEqual(merged["merged"]["absorbed"], [a["id"], g["id"]])  # 按创建顺序
        self.assertEqual(merged["merged"]["stats"], {"correct": 2})
        snap = h.service.snapshot(tid)
        self.assertNotIn("merged", snap["turns"][a["id"]])
        saved = json.loads((h.cwd / ".branchboard" / f"{tid}.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["turns"][root["id"]]["merged"]["markdown"], "视觉知识只在粗粒度上随规模增强，而且并不可靠。")

        # 收起的卡片不能再被提问；被合并的卡片自己不能再并入别处
        with self.assertRaises(ValueError):
            h.service.create_turn(tid, {"questions": [{"sourceId": g["id"], "text": "再问", "quote": {"text": "可靠"}}]})
        with self.assertRaises(ValueError):
            h.service.merge_subtree(tid, a["id"], {"markdown": "x", "absorbed": [g["id"]]})

        # 之后从根卡片提问：会话仍从根的原始会话分叉，合并版作为「以此为准」的材料放在最前面
        follow = run({"questions": [{"sourceId": root["id"], "text": "所以结论是？", "quote": {"text": "视觉知识"}}]}, SID[4], "好")
        args = h.procs[-1].args
        self.assertEqual(args[args.index("--resume") + 1], SID[0])
        prompt = [m for m in h.procs[-1].sent if m["type"] == "user"][0]["message"]["content"]
        self.assertTrue(prompt.startswith("[第 1 轮的回答之后整合了 2 个划词分支的结论，下面是整合后的版本；它和你此前的回答不一致的地方，以它为准]\n\n"
                                          "视觉知识只在粗粒度上随规模增强，而且并不可靠。"))
        self.assertTrue(prompt.endswith("[针对上一条回答中的这段内容提问]\n> 视觉知识\n\n所以结论是？"))
        # 跨分支引用合并过的卡片时，用合并版
        cross = run({"questions": [{"sourceId": main["id"], "text": "一", "quote": {"text": "主线"}},
                                   {"sourceId": root["id"], "text": "二", "quote": {"text": "规模"}}]}, SID[5], "好")
        self.assertEqual(cross["parentId"], main["id"])
        prompt = [m for m in h.procs[-1].sent if m["type"] == "user"][0]["message"]["content"]
        self.assertNotIn("视觉知识只在粗粒度上随规模增强", prompt.split("我有")[0].split("[以下内容来自其他对话分支")[0])

        # 取消合并：卡片恢复，收起的卡片又能提问
        restored = h.service.unmerge_subtree(tid, root["id"])
        self.assertNotIn("merged", restored)
        with self.assertRaises(ValueError):
            h.service.unmerge_subtree(tid, root["id"])
        run({"questions": [{"sourceId": g["id"], "text": "再问", "quote": {"text": "可靠"}}]}, SID[6], "可以了")

        # 接口
        app = Flask(__name__)
        app.register_blueprint(create_branchboard_blueprint(h.service))
        client = app.test_client()
        url = f"/api/branchboard/trees/{tid}/turns/{root['id']}/merge"
        self.assertEqual(client.post(url, json={"markdown": "m", "absorbed": [a["id"]]}).status_code, 200)
        self.assertEqual(client.delete(url).status_code, 200)
        self.assertEqual(client.delete(url).status_code, 400)

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

        # 多处提问：从主线和分支 A 各划一段，一次问出 → 新卡片从主线分叉，分支 A 的内容作为引用拼进来
        merged = ask({"questions": [
            {"sourceId": main["id"], "text": "", "quote": {"text": main["answer"]["markdown"].strip()[:6] or "没有"}},
            {"sourceId": branch_a["id"], "text": "结合这两处，暗号到底是什么？原样说出，不要使用任何工具。",
             "quote": {"text": branch_a["answer"]["markdown"].strip()[:6] or "记住了"}},
        ]})
        self.assertEqual(merged["parentId"], main["id"])  # 主线这一轮比分支 A 晚创建，是主父节点
        self.assertIn("7319", merged["answer"]["markdown"])
        # 合并只影响这张新卡片：主线之后的提问仍然不知道暗号
        main_again = ask({"parentId": main["id"], "kind": "follow", "question": ask_secret})
        self.assertNotIn("7319", main_again["answer"]["markdown"])


if __name__ == "__main__":
    unittest.main()
