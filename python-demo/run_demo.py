"""跑一遍完整场景：DeepAgents + Dogwood 拦截器。

三段剧情：

  1. agent 想退款 500 —— 历史里没有批准记录 —— 被拒（工具**没有**执行）
  2. agent 调审批工具，批准 500 —— 这只是一个 history 事件，不进策略窗口
  3. agent 再想退款 500 —— 窗口里有匹配的批准 —— 放行（工具真的执行了）

一次 ``agent.invoke()`` 内部到底发生什么（以第 1 幕为例）：

    HumanMessage("请退款 500")
        │
        ├─► 模型第 1 次被调用 ──► 返回 tool_call: refund(amount=500)
        │
        ├─► middleware.wrap_tool_call(refund, {"amount": 500})
        │       └─ submit {kind: "request"} ──► Dogwood ──► DENY
        │       └─ 不调用 handler（工具没执行），返回一条 ToolMessage
        │
        ├─► 模型第 2 次被调用（这次它看到的是那条 ToolMessage）
        │       └─► 返回最终答复「第 1 次退款被拒绝了。」
        │
        └─► invoke() 返回整条 messages 列表

所以**每一幕消耗剧本里的两条消息**（一次工具调用 + 一次最终答复），三幕就是六条。
这就是下面 ``SCRIPT`` 为什么是 6 项。

为了让 demo 不需要 API key 也确定可复现，这里用一个脚本化的假模型按固定剧本产出
工具调用；换成真模型只需把 ``model=`` 换掉，拦截逻辑一个字都不用改。

默认起进程内的**离线替身**服务（`dogwood_stub_server.py`）。要打真的
`dogwood-server`，先按 README 把它跑起来，然后：

    DOGWOOD_SOCKET=/path/to/run/dogwood.sock DOGWOOD_CONTROL_SOCKET=/path/to/control.sock \
        python3 run_demo.py --no-stub
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from pydantic import PrivateAttr

from dogwood_client import ControlClient, DogwoodClient, describe
from dogwood_middleware import DogwoodGuard

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO_DIR = os.environ.get("DOGWOOD_DEMO_DIR", "/tmp/dogwood-demo")


# ── 被治理的工具 ────────────────────────────────────────────────────────────


@tool
def approve(amount: int) -> dict:
    """审批一笔退款金额（金额需要和后续退款一致）。

    ``@tool`` 把普通函数变成 LangChain 工具：**函数名就是工具名**（这个字符串
    必须和策略里的动作名逐字一致 —— ``approve`` 对 ``Action::"approve"``），
    类型注解 + docstring 组成工具 schema，模型就是看着它决定怎么调的。

    返回值必须是 JSON 能表示的：它会成为 ``response`` 事件的 ``logged.output``，
    策略里的 ``output.approved: true`` 匹配的就是它。
    """
    return {"approved": True, "amount": amount}


@tool
def refund(amount: int) -> dict:
    """执行退款。只有被批准过的金额才应该走到这里。

    这个函数体是否被执行，完全由 middleware 决定 —— 被拒时它根本不会被调用。
    """
    return {"refunded": True, "amount": amount}


# ── 脚本化模型（只为免 API key；换成真模型不影响拦截逻辑）──────────────────


def _call(name: str, args: dict, call_id: str) -> AIMessage:
    """造一条"我要调工具"的模型消息。

    ``tool_calls`` 是 LangChain 里模型表达工具调用的标准字段，每项要有 ``name``
    （工具名）、``args``（参数）和 ``id``（回填 ToolMessage 时要对应上，所以
    middleware 被拒时返回的 ToolMessage 必须带同一个 id）。
    """
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


# 剧本：模型每被调用一次就往下取一条。三幕 × 每幕两条 = 六条。
#
#   第 1 幕（请退款 500）      ① _call(refund, 500)  → ② "第 1 次退款被拒绝了。"
#   第 2 幕（请先审批 500）    ③ _call(approve, 500) → ④ "已审批通过。"
#   第 3 幕（请退款 500）      ⑤ _call(refund, 500)  → ⑥ "退款完成。"
#
# 注意 ② ④ ⑥ 是"模型看到工具结果之后"的答复：真模型会据实际结果编话，
# 我们是写死的，所以哪怕中间被拒了它也会照着说 —— demo 要的正是可复现。
SCRIPT = [
    _call("refund", {"amount": 500}, "call-1"),
    AIMessage("第 1 次退款被拒绝了。"),
    _call("approve", {"amount": 500}, "call-2"),
    AIMessage("已审批通过。"),
    _call("refund", {"amount": 500}, "call-3"),
    AIMessage("退款完成。"),
]


class ScriptedChatModel(BaseChatModel):
    """一个"按剧本走"的聊天模型，用来免掉 API key，并让输出完全可复现。

    为什么要写这么一个类：deepagents 要求 ``model`` 是一个 LangChain 的
    ``BaseChatModel``。真模型的 ``_generate`` 里是发 HTTP 请求；我们把它换成
    "从 ``SCRIPT`` 里取下一条"，其余机制（agent 循环、工具调用、middleware）
    全都照常工作 —— 所以这个假模型照样能把拦截器完整地跑一遍。

    换成真模型时，这个类整个删掉，把 ``model=ScriptedChatModel()`` 换成
    ``model="openai:gpt-5.5"`` 之类即可，**拦截逻辑一行都不用改**。

    LangChain 的模型协议（我们要满足的四件事）：

    * ``_llm_type`` —— 必填属性，标识这是哪个 provider，出现在追踪/序列化里；
    * ``_generate()`` —— 同步入口，返回 ``ChatResult``；外面那层公开的
      ``invoke()`` 会包装它（重试、回调、token 统计都在那层）；
    * ``bind_tools()`` —— agent 在生成之前调用它，把可用工具告诉模型；
    * 实例状态要声明成 pydantic 字段（``BaseChatModel`` 本身是 pydantic 模型）。

    两个"和真模型不一样、但 demo 里可以接受"的地方，用的时候心里有数：

    1. 真模型是**无状态**的（同样的 messages 必然给同样的结果），这个类带游标，
       同一个实例跑第二遍会拿到剧本里更靠后的内容；
    2. 真模型会读 ``messages`` 里发生了什么，这个类**忽略** ``messages``，
       只看自己走到第几步。
    """

    # 游标：走到剧本第几条。下划线开头 = 私有字段（不进模型 schema），
    # PrivateAttr 给它一个默认值 —— 这也正是本类不需要写 __init__ 的原因，
    # 实例是 pydantic 造出来的。
    _cursor: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        """给 LangChain 看的类型标签（出现在 tracing 和日志里）。"""
        return "scripted"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        """把工具绑定到模型上。

        真模型在这里把工具定义翻译成 provider 自己的 tool schema，再发出去。
        我们不需要 —— 剧本已经决定调哪个工具、带什么参数 —— 所以直接返回
        ``self``，让 LangChain 拿到一个"已经绑好工具"的可运行对象即可。

        ``tools`` 参数其实是**观察 agent 到底给模型看了哪些工具**的好地方：
        deepagents 默认还会塞进 ``ls`` / ``read_file`` / ``write_file`` /
        ``edit_file`` / ``glob`` / ``grep`` / ``execute`` / ``task``。
        """
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager=None,  # noqa: ANN001
        **kwargs,  # noqa: ANN003
    ) -> ChatResult:
        """LangChain 真正调用的方法：返回 ``ChatResult``，不是直接返回消息。

        ``messages`` 被刻意忽略（见类文档）；``stop`` / ``run_manager`` /
        ``kwargs`` 是框架传进来的，假模型不需要理会，但签名要接得住。

        ``ChatResult`` 里可以放多个候选，agent 取第一个 —— 所以我们要用
        ``ChatGeneration(message=...)`` 把一条 ``AIMessage`` 包起来。
        """
        # min(...) 是**夹紧**：万一 agent 调模型的次数比剧本多（多一步、重试
        # 之类），就重复最后一条"最终答复"，而不是抛 IndexError 把 demo 打断。
        # 代价是它会掩盖"剧本没对上"的问题 —— 所以下面的 main() 会把每一步的
        # 消息都打出来，对不上时你一眼能看见。
        message = SCRIPT[min(self._cursor, len(SCRIPT) - 1)]
        self._cursor += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


# ── 组装 ────────────────────────────────────────────────────────────────────


def install_policy(control: ControlClient) -> None:
    """把策略源文件和 action schema 送到控制面安装。

    两个文件都是**给 Dogwood 解析的输入**：``.dw`` 是策略（Cedar + ``when
    temporal``），``.cedarschema`` 是 Cedar 现成的 action schema。服务端会先校验
    再原子替换 —— 装不上时正在服务的策略集不受影响，所以这个调用失败是"没变化"
    而不是"策略没了"。
    """
    with open(os.path.join(HERE, "demo_policy.dw"), encoding="utf-8") as fh:
        policy = fh.read()
    with open(os.path.join(HERE, "demo_schema.cedarschema"), encoding="utf-8") as fh:
        schema = fh.read()
    applied = control.install(policy, schema)
    print(f"  install -> {applied}")
    # 立刻回读一次：确认规则真的进去了（rule_count=2），顺便看服务端报出来的
    # decision_kinds —— 决策 kind 是 schema 决定的，别在客户端硬编码。
    status = control.status()
    print(f"  status  -> {status}")
    print(f"  （decision_kinds={status.get('decision_kinds')} 是服务端报出来的，别硬编码）")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-stub", action="store_true", help="连真的 dogwood-server")
    parser.add_argument(
        "--no-install",
        action="store_true",
        help="跳过控制面安装：策略已经装进 store，或控制面不可用（macOS 就是这种情况）",
    )
    parser.add_argument("--quiet", action="store_true", help="只打印结果，不打印拦截器日志")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    stub = None
    if args.no_stub:
        # 打真服务：只连 socket，不管理它的生命周期（服务得你自己先起着）。
        data_path = os.environ.get("DOGWOOD_SOCKET", f"{DEMO_DIR}/run/dogwood.sock")
        control_path = os.environ.get("DOGWOOD_CONTROL_SOCKET", f"{DEMO_DIR}/control.sock")
    else:
        # 离线替身：进程内起，同协议但语义是硬编码的，只为了让 demo 在没编译
        # Rust 的机器上也能跑完整条链路（它不是 Dogwood 实现，见它的模块文档）。
        from dogwood_stub_server import StubServer

        stub = StubServer(DEMO_DIR).start()
        data_path, control_path = str(stub.data_socket), str(stub.control_socket)
        print(f"（离线替身已起在 {data_path}；真服务请用 --no-stub）")

    # 两个 socket、两个客户端，权限也不同：控制面 0700 只有属主 uid 能连。
    # agent 该拿到的只有 data 那一个 —— 这里为了演示才两个都在手上。
    control = ControlClient(control_path)
    data = DogwoodClient(data_path)
    try:
        if args.no_install:
            # 控制面按设计只在 Linux 上可用（对端认证靠 SO_PEERCRED，fail-closed），
            # 所以 macOS 上要先用别的方式把策略装进 store，再跑这里。
            print("\n[1] 跳过策略安装（--no-install）：沿用 store 里已有的策略集")
        else:
            print("\n[1] 安装策略集")
            install_policy(control)

        print("\n[2] 组装 DeepAgents：工具 + 拦截器 middleware")
        from deepagents import create_deep_agent

        # 拦截器只需要三样东西：一个 Dogwood 连接、哪些工具归策略管、
        # 以及**谁**在调用（principal 来自你的认证上下文，绝不是模型给的参数）。
        guard = DogwoodGuard(
            data,
            protected_tools={"approve", "refund"},   # 没列进来的工具默认按拒绝处理
            principal='AgentCore::User::"alice"',
        )
        # middleware= 是 deepagents 的扩展点；我们的 guard 继承 LangChain 的
        # AgentMiddleware，用它的 wrap_tool_call 钩住每一次工具调用。
        agent = create_deep_agent(
            model=ScriptedChatModel(),
            tools=[approve, refund],
            system_prompt="你是一个退款助手。退款前必须先取得对同一金额的审批。",
            middleware=[guard],
        )
        print("  agent 就绪；受策略管辖的工具：approve, refund")

        # 三幕剧情。注意每一幕都是独立的一次 invoke()，而工具产生的历史会累积在
        # Dogwood 里 —— 这就是第 3 幕能通过的原因。
        turns = [
            ("第 1 次退款 500（没有批准历史）", "请退款 500"),
            ("先审批 500", "请先审批 500"),
            ("第 2 次退款 500（已有批准历史）", "请退款 500"),
        ]
        for i, (title, prompt) in enumerate(turns, start=1):
            print(f"\n[3.{i}] {title}")
            out = agent.invoke({"messages": [HumanMessage(prompt)]})
            # 把整条消息列表打出来：ToolMessage 是被拒/执行的结果，
            # AIMessage 是模型（也就是剧本）说的话。对照上面那张剧本表看。
            for m in out["messages"]:
                kind = type(m).__name__
                text = (m.content or "").strip()
                if kind == "ToolMessage":
                    print(f"  ToolMessage({m.name}): {text}")
                elif text:
                    print(f"  {kind}: {text}")

        # 这段**不走 agent**，就是直接对 Dogwood 问两个问题，用来演示裁决本身：
        # 同样的动作、同样的调用者，只有金额不同 —— 历史里那条批准只对得上 500。
        print("\n[4] 直接问一句历史（这条不走 agent，纯粹展示裁决）")
        recheck = data.submit(
            'AgentCore::Action::refund', "request",
            principal='AgentCore::User::"alice"',
            resource='AgentCore::Account::"main"',
            logged={"input": {"amount": 500}},
            context={"input": {"amount": 500}},
        )
        print(f"  refund(500) -> {describe(recheck)}")
        other = data.submit(
            'AgentCore::Action::refund', "request",
            principal='AgentCore::User::"alice"',
            resource='AgentCore::Account::"main"',
            logged={"input": {"amount": 999}},
            context={"input": {"amount": 999}},
        )
        print(f"  refund(999) -> {describe(other)}   ← 金额对不上，历史里那条批准不适用")
    finally:
        # 关掉所有连接；替身还要把 socket 文件删掉。
        data.close()
        control.close()
        if stub is not None:
            stub.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
