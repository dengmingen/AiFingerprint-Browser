"""MCP Server 协议级测试：spawn 子进程，逐条发 JSON-RPC 验证响应。"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python"

proc = subprocess.Popen(
    [str(PY), "-m", "app.mcp_server"],
    cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.PIPE, text=True, encoding="utf-8")


def send(msg, expect_response=True):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    if not expect_response:
        return None
    line = proc.stdout.readline()
    assert line.strip(), "子进程未返回响应（可能已退出）"
    return json.loads(line)


def check(name, cond, extra=""):
    print(("PASS" if cond else "FAIL"), name, extra)
    if not cond:
        proc.kill()
        sys.exit(1)


r = send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t"}}})
check("initialize 版本回显", r["result"]["protocolVersion"] == "2025-03-26")
check("serverInfo", r["result"]["serverInfo"]["name"] == "fpworkbench")

send({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_response=False)

r = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
names = [t["name"] for t in r["result"]["tools"]]
check("tools/list 数量>=19", len(names) >= 19, f"共 {len(names)} 个")
check("工具含 inputSchema", all("inputSchema" in t for t in r["result"]["tools"]))

r = send({"jsonrpc": "2.0", "id": 3, "method": "ping"})
check("ping", r["result"] == {})

r = send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
          "params": {"name": "nope", "arguments": {}}})
check("未知工具 -32602", r.get("error", {}).get("code") == -32602)

# status 工具：工作台可能运行也可能没运行，两种都算通过（只验证协议形态）
r = send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
          "params": {"name": "status", "arguments": {}}})
content = r["result"]["content"][0]
check("status 返回文本内容", content["type"] == "text" and content["text"])
print("  status isError =", r["result"]["isError"], "|", content["text"][:120].replace("\n", " "))

r = send({"jsonrpc": "2.0", "id": 6, "method": "resources/list"})
check("未知方法 -32601", r.get("error", {}).get("code") == -32601)

proc.stdin.write("not json\n")
proc.stdin.flush()
line = proc.stdout.readline()
check("坏 JSON -32700", json.loads(line)["error"]["code"] == -32700)

proc.stdin.close()
proc.wait(timeout=15)
check("stdin 关闭后正常退出", proc.returncode == 0)
print("\n全部通过 ✔")
