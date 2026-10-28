"""Real headless tool integration with a local deterministic OpenAI fixture.

No account/model/network credentials are used. Each role executes read, write and
bash outside its checkout; inherited deny rules and process restart are exercised.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import subprocess

from probe_opencode_runtime import probe


def exercise_runtime():
    with tempfile.TemporaryDirectory(prefix="codebot-external-") as scratch:
        external = Path(scratch)
        (external / "input.txt").write_text("fixture input")

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                messages = payload.get("messages", [])
                count = sum(m.get("role") == "tool" for m in messages)
                tools = {t["function"]["name"] for t in payload.get("tools", [])}
                steps = [("read", {"filePath": str(external / "input.txt")}),
                         ("write", {"filePath": str(external / "output.txt"), "content": "fixture output"}),
                         ("bash", {"command": "pwd", "workdir": str(external)})]
                if count < len(steps) and steps[count][0] in tools:
                    name, args = steps[count]
                    message = {"role": "assistant", "content": None, "tool_calls": [{
                        "id": f"call_{count}", "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)}}]}
                    reason = "tool_calls"
                else:
                    message = {"role": "assistant", "content": "fixture complete"}
                    reason = "stop"
                response = {"id": "chatcmpl_fixture", "object": "chat.completion", "created": 1,
                    "model": "fixture", "choices": [{"index": 0, "message": message, "finish_reason": reason}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}
                if payload.get("stream"):
                    delta = {k: v for k, v in message.items() if k != "tool_calls"}
                    if "tool_calls" in message:
                        delta["tool_calls"] = [{"index": 0, **message["tool_calls"][0]}]
                    chunks = [{"id": "chatcmpl_fixture", "object": "chat.completion.chunk", "created": 1,
                               "model": "fixture", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                              {"id": "chatcmpl_fixture", "object": "chat.completion.chunk", "created": 1,
                               "model": "fixture", "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                               "usage": response["usage"]}]
                    body = ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
                            + "data: [DONE]\n\n").encode()
                else:
                    body = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if payload.get("stream") else "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = {"model": "fixture/fixture", "small_model": "fixture/fixture", "provider": {
                "fixture": {"npm": "@ai-sdk/openai-compatible", "name": "Fixture",
                    "options": {"baseURL": f"http://127.0.0.1:{server.server_port}/v1", "apiKey": "fixture"},
                    "models": {"fixture": {"name": "fixture", "limit": {"context": 32000, "output": 1024}}}}},
                "agent": {role: {"permission": {"*": "deny"}} for role in ("build", "general", "explore")}}

            def exercise(request, root, parent):
                roles = []
                for role in ("build", "general", "explore"):
                    child = request("/session", {"parentID": parent["id"], "permission": [
                        {"permission": "*", "pattern": "*", "action": "deny"}]})
                    request("/session/" + child["id"] + "/message", {
                        "agent": role, "model": {"providerID": "fixture", "modelID": "fixture"},
                        "parts": [{"type": "text", "text": "Run fixture tools."}]})
                    history = request("/session/" + child["id"] + "/message")
                    completed = [p for m in history for p in m["parts"] if p.get("type") == "tool"]
                    assert [p["tool"] for p in completed] == ["read", "write", "bash"], completed
                    assert all(p["state"]["status"] == "completed" for p in completed), completed
                    assert (external / "output.txt").read_text() == "fixture output"
                    # Same session and same role after an explicit resume.
                    request("/session/" + child["id"] + "/message", {"agent": role,
                        "parts": [{"type": "text", "text": "Resume fixture."}]})
                    roles.append(role)
                assert request("/permission") == []
                bridge = Path(__file__).resolve().parent.parent / "agent-plugin/runtime/run.js"
                process = subprocess.run(["node", str(bridge)], cwd=root, env=request.environment,
                    input=json.dumps({"directory": str(root), "model": "fixture/fixture",
                                      "prompt": "Run fixture tools."}),
                    capture_output=True, text=True, errors="replace", timeout=30)
                assert process.returncode == 0, (process.stdout, process.stderr)
                events = [json.loads(line) for line in process.stdout.splitlines()]
                assert any(e["type"] == "tool_start" for e in events), events
                assert any(e["type"] == "text" for e in events), events
                return roles

            plugin = Path(__file__).resolve().parent.parent / "agent-plugin/.opencode/plugins/coderbot-openspec.js"
            return [probe(plugin=plugin, exercise=exercise, extra_config=config) for _ in range(2)]
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    print(json.dumps(exercise_runtime()))
