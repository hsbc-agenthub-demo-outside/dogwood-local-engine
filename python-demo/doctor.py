"""体检：依赖装对了吗？socket 通吗？策略装了吗？

三种用法：

    python3 doctor.py                 # 只查环境和默认 socket
    python3 doctor.py --stub          # 起离线替身，从零跑一遍完整链路（不需要 Rust）
    python3 doctor.py --socket ... --control ...   # 查真 dogwood-server

退出码 0 = 全部通过，1 = 有硬失败（每条失败都会带上该执行的命令）。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import socket as socketlib
import stat
import sys
import tempfile

from dogwood_client import ControlClient, DogwoodClient, DogwoodError, describe

OK, BAD, WARN = "  [ok]  ", "  [FAIL]", "  [warn]"
failures = 0


def _ok(msg: str) -> None:
    print(f"{OK} {msg}")


def _fail(msg: str, hint: str | None = None) -> None:
    global failures
    failures += 1
    print(f"{BAD} {msg}")
    if hint:
        print(f"         → {hint}")


def _warn(msg: str, hint: str | None = None) -> None:
    print(f"{WARN} {msg}")
    if hint:
        print(f"         → {hint}")


# ── 环境 ────────────────────────────────────────────────────────────────────

REQUIRED = ["deepagents", "langchain", "langgraph", "langchain_core"]
VERIFIED = {"deepagents": "0.6.12", "langchain": "1.3.14", "langgraph": "1.2.9", "langchain_core": "1.5.1"}


def check_python() -> None:
    if sys.version_info >= (3, 11):
        _ok(f"Python {sys.version.split()[0]}（deepagents 要求 >= 3.11）")
    else:
        _fail(
            f"Python {sys.version.split()[0]} 太旧：deepagents 要求 >= 3.11",
            "conda activate dogwood  # 那个环境是 3.12",
        )
    print(f"         解释器：{sys.executable}")


def check_packages() -> None:
    import importlib.metadata as md

    for name in REQUIRED:
        if importlib.util.find_spec(name) is None:
            _fail(
                f"缺 {name}",
                "python3 -m pip install -r requirements.txt",
            )
            continue
        try:
            version = md.version(name)
        except Exception:  # noqa: BLE001 - 元数据缺失不该让体检崩掉
            version = "unknown"
        note = "" if VERIFIED.get(name) in (None, version) else f"（验证过的版本是 {VERIFIED[name]}）"
        _ok(f"{name} {version}{note}")


def check_apis() -> None:
    """光能 import 不够：我们要的钩子和字段必须真的在。"""
    try:
        from langchain.agents.middleware import AgentMiddleware

        if not hasattr(AgentMiddleware, "wrap_tool_call"):
            _fail(
                "AgentMiddleware 上没有 wrap_tool_call",
                "这个版本的 langchain 和 demo 不匹配，装 requirements.txt 里的版本",
            )
        else:
            _ok("AgentMiddleware.wrap_tool_call 存在")
    except Exception as exc:  # noqa: BLE001
        _fail(f"import langchain.agents.middleware 失败：{exc}", "pip install -r requirements.txt")
        return

    try:
        from langgraph.prebuilt.tool_node import ToolCallRequest
        import dataclasses

        fields = {f.name for f in dataclasses.fields(ToolCallRequest)}
        if {"tool_call", "state", "runtime"} <= fields:
            _ok(f"ToolCallRequest 字段：{sorted(fields)}")
        else:
            _fail(f"ToolCallRequest 字段不对：{sorted(fields)}")
    except Exception as exc:  # noqa: BLE001
        _fail(f"import ToolCallRequest 失败：{exc}")

    try:
        from deepagents import create_deep_agent  # noqa: F401

        _ok("deepagents.create_deep_agent 可用")
    except Exception as exc:  # noqa: BLE001
        _fail(f"import deepagents 失败：{exc}", "pip install -r requirements.txt")


# ── 连通性 ──────────────────────────────────────────────────────────────────


def check_path(path: str, label: str) -> bool:
    if not os.path.exists(path):
        _fail(
            f"{label} 不存在：{path}",
            "先起服务：dogwood-server --dir /tmp/dogwood-demo run（或用 --stub）",
        )
        return False
    mode = stat.S_IMODE(os.stat(path).st_mode)
    kind = "socket" if stat.S_ISSOCK(os.stat(path).st_mode) else "不是 socket"
    _ok(f"{label} 存在：{path}（mode={oct(mode)}，{kind}）")
    if kind != "socket":
        _fail(f"{label} 不是 Unix socket")
        return False
    return True


def check_data(path: str) -> None:
    try:
        with DogwoodClient(path) as client:
            pong = client.ping()
        if pong.get("status") == "pong":
            _ok(f"数据面 ping -> version={pong.get('version')}")
        else:
            _fail(f"数据面 ping 返回异常：{pong}")
    except DogwoodError as exc:
        _fail(f"连不上数据面：{exc}", "检查服务是否在跑、socket 路径是否写对")
    except OSError as exc:
        # 典型：控制面 0700，uid 不同 -> EACCES
        _fail(f"连数据面被拒：{exc}", "权限问题：control.sock 是 0700，只有属主 uid 能连")


def check_control(path: str) -> None:
    try:
        with ControlClient(path) as client:
            status = client.status()
    except (DogwoodError, OSError) as exc:
        _fail(f"连不上控制面：{exc}", "控制面是 0700：必须用服务端属主的 uid 跑")
        return
    if status.get("status") != "status":
        _fail(f"控制面 status 返回异常：{status}")
        return
    rules = status.get("rule_count", 0)
    if rules:
        _ok(f"控制面 status -> rule_count={rules}, leaf_count={status.get('leaf_count')}")
        _ok(f"decision_kinds={status.get('decision_kinds')}（用这个，别硬编码 kind 名）")
    else:
        _fail(
            "控制面通了，但策略集是空的（0 条规则）→ 所有请求都会 deny",
            "dogwood-server --dir ... policy install python-demo/demo_policy.dw "
            "--action-schema python-demo/demo_schema.cedarschema",
        )


def probe(data_path: str, action: str, amount: int) -> None:
    """真的发一条 request。注意：这会往 store 里写一个事件。"""
    try:
        with DogwoodClient(data_path) as client:
            response = client.submit(
                action, "request",
                principal='AgentCore::User::"alice"',
                resource='AgentCore::Account::"main"',
                logged={"input": {"amount": amount}},
                context={"input": {"amount": amount}},
            )
        _ok(f"探针 {action}({amount}) -> {describe(response)}")
        if response.get("status") == "decision" and not response.get("allowed"):
            print("         （DENY 不一定是错：历史里本来就没有对应批准。看 reason 是否为空）")
    except DogwoodError as exc:
        _fail(f"探针失败：{exc}")


# ── 主流程 ──────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="python-demo 体检")
    parser.add_argument("--stub", action="store_true", help="起离线替身，从零验证整条链路")
    parser.add_argument("--socket", default=os.environ.get("DOGWOOD_SOCKET"))
    parser.add_argument("--control", default=os.environ.get("DOGWOOD_CONTROL_SOCKET"))
    parser.add_argument("--probe", action="store_true", help="真发一条 request（会写事件）")
    parser.add_argument("--deep", action="store_true", help="额外做一次 create_deep_agent 冒烟")
    args = parser.parse_args()

    print("\n[1] Python 与依赖")
    check_python()
    check_packages()
    check_apis()

    print("\n[2] 连通性")
    stub = None
    if args.stub:
        from dogwood_stub_server import StubServer

        stub = StubServer(tempfile.mkdtemp(prefix="dogwood-doctor-")).start()
        here = os.path.dirname(os.path.abspath(__file__))
        with ControlClient(str(stub.control_socket)) as ctl:
            with open(os.path.join(here, "demo_policy.dw"), encoding="utf-8") as fh:
                policy = fh.read()
            with open(os.path.join(here, "demo_schema.cedarschema"), encoding="utf-8") as fh:
                schema = fh.read()
            ctl.install(policy, schema)
        data_path, control_path = str(stub.data_socket), str(stub.control_socket)
        print(f"          （离线替身：{data_path}；它不解析 .dw，只是让链路能跑）")
    else:
        base = "/tmp/dogwood-demo"
        data_path = args.socket or f"{base}/run/dogwood.sock"
        control_path = args.control or f"{base}/control.sock"

    try:
        if check_path(data_path, "数据面"):
            check_data(data_path)
        if check_path(control_path, "控制面"):
            check_control(control_path)
        if args.probe and os.path.exists(data_path):
            print("\n[3] 探针")
            probe(data_path, 'AgentCore::Action::refund', 500)
        if args.deep:
            print("\n[4] deepagents 冒烟")
            _deep_smoke()
    finally:
        if stub is not None:
            stub.stop()

    print()
    if failures:
        print(f"有 {failures} 项没通过（上面的 → 就是该执行的命令）。")
        return 1
    print("全部通过。下一步：python3 run_demo.py（加 --no-stub 打真服务）")
    return 0


def _deep_smoke() -> None:
    try:
        from run_demo import ScriptedChatModel, approve, refund
        from deepagents import create_deep_agent

        agent = create_deep_agent(model=ScriptedChatModel(), tools=[approve, refund])
        _ok(f"create_deep_agent 跑通：{type(agent).__name__}")
    except Exception as exc:  # noqa: BLE001
        _fail(f"create_deep_agent 失败：{exc}")


if __name__ == "__main__":
    sys.exit(main())
