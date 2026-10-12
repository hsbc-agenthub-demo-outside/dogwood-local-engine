"""把 Dogwood 接到 LangChain / DeepAgents 的工具调用边界上。

============ Dogwood 的运行机制（读代码前先看这 6 段）============

**1. 它只认「事件」和「裁决」，不认工具。** 工具是我们（这个文件）的概念。
每次 submit 送过去的是一个事件：动作名 + kind + principal + resource + 两组字段。
Dogwood 回一个裁决；它不会拦工具、不会执行工具、也不知道 LangChain 是什么。

**2. 事件分两种 kind，这是整套机制的轴。**
``request`` 是**决策点** —— 服务端会（落盘之后）评估策略并回 allow/deny；
``response`` 是**历史** —— 只入账，没有裁决。默认 schema 下就是这两个名字
（别硬编码：控制面 ``status`` 会报 ``decision_kinds``，以那个为准）。

**3. 每次 submit 内部是：线性化 → 赋时间戳 → append + fsync → 评估 → 回裁决。**
所有提交在同一把锁下串行化，时间戳由 store 赋且严格递增（客户端连 ``ts`` 字段
都传不了，传了会被拒）。**先落盘再裁决**，所以"有裁决的事件一定在磁盘上"。
代价是每个事件一次 fsync（约 1.18 ms、单实例约 845 events/s）—— 工具调用这个
频率完全够，但别拿它当高频遥测管道。

**4. 引擎记的不是完整历史，而是每条时序条件的窗口状态。** 拿 demo 的策略说：

    formerly within 1h AgentCore::Action::"approve"::response{
        input.amount: context.input.amount, output.approved: true
    }

引擎只在内存里维护"最近 1 小时内匹配这条条件的事件"，事件滑出窗口就淘汰
（所以窗口有界 ⇒ 状态有界，不用每次重扫全历史）。这里有个关键读法：

* ``input.amount``（不带 context 前缀）读的是**历史事件**的 logged 记录；
* ``context.input.amount`` 读的是**当前这个请求**的 context。

换句话说，"同额批准"这个相关性是靠**当前请求的字段去匹配历史事件的字段**实现的。
再加个 ``when { context.input.amount <= 1000 }`` 就是"当前这次别太大"。

**5. 没有匹配的 permit 就是拒绝（deny by default）。** 这是从 Cedar 继承的语义
（forbid 覆盖 permit）。所以 demo 的策略里必须单独有一条 permit 放行 ``approve``
—— 少了它，审批工具自己也会被拒，于是退款永远等不到历史。

**6. 裁决 ≠ 执行。** Dogwood 只回答"该不该"；本文件才决定"让不让"，
做法就是**不调用 handler**。如果这个文件写错了、把裁决丢掉了，工具照跑不误。

============ 这个文件在机制里的位置 ============

只在 ``AgentMiddleware.wrap_tool_call``（DeepAgents 的 middleware 就是继承它）
这一个钩子里干活，每次工具调用三步：

    工具调用 ──► ① submit request  ──► Dogwood：落盘 + 评估窗口 ──► 裁决
                    │
                    ├─ 不是 ALLOW（含传输错误）──► 返回 ToolMessage，**不执行工具**
                    │                              （这一步才是"拦截"，Dogwood 不管）
                    │
                    └─ ALLOW ──► 执行工具 ──► ② submit response ──► 只入账

第 ② 步不是可选的：``formerly`` 匹配的是**历史里的 response 事件**，少了它，
窗口永远是空的，所有时序策略都会表现为"永远拒绝"——而且看起来像策略写错了。

另外两件"它不做"的事，写在代码边上容易忘：**裁决结果不落盘**，而且内部日志会
被 checkpoint 裁剪 —— 所以它不能当审计日志用；**它也不验证 principal**，
事件里的身份是你填的。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from dogwood_client import DogwoodClient, DogwoodError, describe, is_allowed

log = logging.getLogger("dogwood.guard")

#: 能直接进 JSON 事件的值；其余一律转字符串，免得序列化时才炸。
#: 事件是要过 socket 的 JSON，不是 Python 对象 —— 这一点贯穿全文。
_JSONABLE = (str, int, float, bool, list, dict)


class DogwoodGuard(AgentMiddleware):
    """按 Dogwood 的裁决决定工具跑不跑。

    它是"事件源 + 执行者"：**事件源**指它负责把每次工具调用翻译成两条事件
    （request / response）喂给 Dogwood —— 引擎只能看见你喂给它的东西，漏报了
    它的裁决就是错的；**执行者**指它负责真的不让工具跑。Dogwood 那侧两件事都不管。

    ``protected_tools`` 是**已经建模进策略集**的工具名集合。不在集合里的工具
    走 ``on_unprotected``：默认 ``"deny"``（fail closed）。DeepAgents 自带
    ``ls``/``read_file``/``execute`` 等工具，如果你真的要用它们，就应该在
    action schema 和策略里显式建模并放行，而不是让拦截器悄悄跳过。

    为什么默认是 deny 而不是 allow：漏掉一个工具的代价，两种默认值完全不同 ——
    默认 deny 是"功能报错，你去补策略"；默认 allow 是"策略看着挺全，实际有个洞"。
    """

    def __init__(
        self,
        client: DogwoodClient,
        *,
        protected_tools: set[str],
        principal: str,
        resource: str = 'AgentCore::Account::"main"',
        action_prefix: str = "AgentCore::Action::",
        on_unprotected: str = "deny",
    ):
        super().__init__()
        self.client = client
        self.protected_tools = protected_tools
        # principal 必须是**你的认证上下文**里的身份（ADK 的 invocation/user、
        # LangChain 的 runtime/state 之类），绝不能取模型给的参数：模型可以编造
        # 参数，而 Dogwood 无法验证事件里的身份真假。另外它要写成 `Ns::Type::"id"`
        # 这个形状才会被当成实体（否则只是普通字符串，``principal == ...`` 匹配不上）。
        self.principal = principal
        # resource 同理，要出现在 schema 里声明过的实体集合中。
        self.resource = resource
        # 工具名 → 动作名的前缀拼装。看 _action() 的注释：这个名字必须逐字对齐。
        self.action_prefix = action_prefix
        self.on_unprotected = on_unprotected

    # ---- 决策路径 ------------------------------------------------------

    def _action(self, tool_name: str) -> str:
        """工具名 → 事件里的动作名，例如 ``refund`` → ``AgentCore::Action::refund``。

        两个容易踩的点：

        * **必须和策略里写的动作名逐字一致**。策略写 ``Action::"refund"``，这里就得
          产出 ``...Action::refund``；大小写差一个字母，所有请求都会因为
          deny-by-default 被拒，看起来和"策略写错了"一模一样。
        * 线上字符串**不带引号**（``Action::refund``），而策略里写的是
          ``Action::"refund"`` —— 引号是策略语法的一部分，wire 上不用写。
        """
        return f"{self.action_prefix}{tool_name}"

    def _submit(self, tool_name: str, kind: str, args: dict[str, Any], output: Any = None) -> dict[str, Any]:
        """把一个工具调用（或它的结果）翻译成一条 Dogwood 事件并发送，返回原始裁决。

        事件各字段的分工，正好对应策略能读到的几样东西：

        * ``action`` / ``kind`` —— 命中哪条策略、是不是决策点；
        * ``principal`` / ``resource`` —— 谁能对什么做这件事（你在 `__init__` 注入）；
        * ``logged`` —— **写进历史的账本**。未来的时间点用
          ``formerly ...::response{ input.amount: ... }`` 匹配的就是它（连同
          ``output.*``）；所以 response 事件必须把 input 和 output 都带上。
        * ``context`` —— **只属于当前这次决策**的请求字段。策略里
          ``context.input.amount`` 读的是它，而 ``formerly`` 里裸写的
          ``input.amount`` 读的是历史那条事件的 logged。

        注意这里**没有时间戳**：``WireEvent`` 没有该字段，服务端在 append 时赋值。
        这是设计而不是遗漏 —— 否则客户端可以回填时间、把事件挪出窗口或插队。
        """
        logged: dict[str, Any] = {"input": args}
        if output is not None:
            logged["output"] = output
        return self.client.submit(
            self._action(tool_name),
            kind,
            principal=self.principal,
            resource=self.resource,
            logged=logged,
            context={"input": args},   # 当前这次决策读 context.input.*
        )

    def _before(self, tool_name: str, args: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        """工具执行前的问询：submit 一个 ``request`` 事件，拿裁决。

        这一刻引擎做的判断是：拿**当前请求**（context + principal/resource）去和
        **窗口里积累的历史**（窗口内事件的 logged 记录）比。所以第 1 幕退款被拒
        不是因为"金额不对"，而是因为那条 ``formerly`` 在窗口里找不到匹配的批准。

        fail closed：传输失败、超时、服务端返回 error，一律按**拒绝**处理。反过来
        写（出错就放行）等于给策略开了个后门，而且是最难发现的那种。
        """
        if tool_name not in self.protected_tools:
            allowed = self.on_unprotected == "allow"
            log.warning(
                "tool %r is not modeled in the policy set; %s it (on_unprotected=%r)",
                tool_name,
                "allowing" if allowed else "denying",
                self.on_unprotected,
            )
            return allowed, {"status": "unprotected"}
        try:
            return is_allowed(v := self._submit(tool_name, "request", args)), v
        except DogwoodError as exc:                     # 传输失败 = 拒绝
            log.error("dogwood unavailable, failing closed: %s", exc)
            return False, {"status": "error", "message": str(exc)}

    def _after(self, tool_name: str, args: dict[str, Any], output: Any) -> None:
        """工具执行后：submit 一个 ``response`` 事件，只入账，没有裁决。

        这一步是"喂历史"。它不是为了审计（引擎的日志会被 checkpoint 裁剪、裁决
        结果也不落盘），而是为了**让后续的时序条件有东西可匹配** —— 下一幕的
        ``formerly within 1h approve::response{...}`` 命中的就是这条记录。
        """
        try:
            response = self._submit(tool_name, "response", args, output=self._tool_output(output))
            log.info("history %s: %s", tool_name, describe(response))
        except DogwoodError as exc:
            # 工具已经执行过了，没法回滚；而**下一次**决策会因为这个缺口算错
            # （窗口里少一条记录 ⇒ 该放行的被拒，或该拒的没被拦）。所以必须吵，
            # 不能静默吞掉。真要兜底，应该记进你自己的审计日志并告警。
            log.error("failed to record response for %s: %s", tool_name, exc)

    @staticmethod
    def _tool_output(result: Any) -> Any:
        """把工具结果还原成结构化 JSON，供策略里 ``output.approved`` 之类的字段读取。

        ToolNode 会把工具返回值包成 ``ToolMessage``（结构化结果在 ``artifact``，
        否则是 JSON 字符串形式的 ``content``）；直接调用工具时拿到的就是原始返回值。
        两种都要能吃下 —— 否则策略读到的是 ``"ToolMessage(content='...')"`` 这坨文本，
        ``output.approved: true`` 永远匹配不上，又会变成"历史有了但裁决还是拒绝"。
        """
        artifact = getattr(result, "artifact", None)
        if artifact is not None:
            return artifact if isinstance(artifact, _JSONABLE) else str(artifact)

        content = getattr(result, "content", result)
        if isinstance(content, str):
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return content
        return content if isinstance(content, _JSONABLE) else str(content)

    # ---- 钩子 ----------------------------------------------------------

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Any],
    ) -> ToolMessage | Any:
        """LangChain 的钩子：每次工具调用都会经过这里。

        返回值就是执行结果本身：放行时是工具真正的输出，被拒时是一条我们自己造的
        ``ToolMessage`` —— 从模型的角度看，那只是一次"工具返回了拒绝原因"，它并不
        知道背后有个策略引擎（这样也不容易诱导它去想办法绕过）。
        """
        tool_call = request.tool_call
        name = tool_call["name"]
        args = dict(tool_call.get("args") or {})

        allowed, verdict = self._before(name, args)
        if not allowed:
            log.info("DENY %s(%s): %s", name, args, describe(verdict))
            # 注意：这里**不调用 handler** —— 这才是"拦截"。Dogwood 只回了裁决，
            # 工具没跑，是这一行决定的。
            #
            # 被拒的调用也**不记 response 事件**：没有 outcome 可记。副作用是
            # 一个被拒的尝试不会满足策略里 `formerly ...::response{...}` 条件 ——
            # 也就是说反复重试并不会"把窗口喂开"。这是想要的行为：拒绝不产生历史。
            # （request 事件本身已经进了日志，所以"尝试过"这件事引擎是知道的，
            #   需要统计失败次数的话，策略可以直接匹配 request。）
            return ToolMessage(
                content=self._denial_text(name, verdict),
                tool_call_id=tool_call["id"],
                name=name,
            )

        log.info("ALLOW %s(%s)", name, args)
        # 先执行，再记历史：顺序不能反 —— 没执行就记 response，等于凭空捏造历史。
        result = handler(request)
        self._after(name, args, result)
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Any]],
    ) -> ToolMessage | Any:
        """异步版，语义和同步版逐行对应。

        唯一的差别是 Dogwood 客户端是**阻塞**的（socket + 服务端 fsync 大约 1 ms
        量级），所以在 async 上下文里要丢进线程，否则会卡住整个事件循环 ——
        多个 agent 并发跑时那会很痛。同步版和异步版必须保持同样的决断顺序，
        否则同一份策略在不同调用方式下行为会不一致。
        """
        tool_call = request.tool_call
        name = tool_call["name"]
        args = dict(tool_call.get("args") or {})

        allowed, verdict = await asyncio.to_thread(self._before, name, args)
        if not allowed:
            return ToolMessage(
                content=self._denial_text(name, verdict),
                tool_call_id=tool_call["id"],
                name=name,
            )

        result = await handler(request)
        await asyncio.to_thread(self._after, name, args, result)
        return result

    @staticmethod
    def _denial_text(tool_name: str, verdict: dict[str, Any]) -> str:
        if verdict.get("status") == "error":
            return (
                f"[dogwood] 无法获得 {tool_name!r} 的授权裁决"
                f"（{verdict.get('message')}），按拒绝处理。"
            )
        if verdict.get("status") == "unprotected":
            return f"[dogwood] 工具 {tool_name!r} 没有建模进策略集，按拒绝处理。"
        return (
            f"[dogwood] {tool_name!r} 被策略拒绝。"
            "注意这是拒绝，不是失败：重试一次结果相同。"
        )


# ---- 不依赖 LangChain 的版本（ADK / 自研 harness / 单测都能用）----------------


def guarded_tool_call(
    client: DogwoodClient,
    tool_name: str,
    args: dict[str, Any],
    run: Callable[[dict[str, Any]], Any],
    *,
    principal: str,
    resource: str = 'AgentCore::Account::"main"',
    action_prefix: str = "AgentCore::Action::",
) -> dict[str, Any]:
    """同一个"问 → 拦 → 记"流程，但没有框架依赖。返回结果字典，不抛异常。

    存在意义：Dogwood 那侧的机制跟 agent 框架无关 —— 同一套事件、同一份策略、
    同一个 server。换框架换的只是"钩子挂在哪"，所以这段在三处都能用：

    * ADK 的 ``before_tool_callback`` —— 返回非 None（这里可以返回
      ``{"reason": ...}``）就代表"跳过工具"，正好对上这里的拒绝分支；
    * 自研 harness —— 在真正调用工具的那一行前面调它；
    * 单测 —— ``test_middleware.py`` 就是直接测它，不经过 LLM。

    注意 ``run`` 只在拿到 ALLOW 之后才被调用；被拒或出错时 ``allowed`` 是 False，
    调用方必须照这个值决定跑不跑。
    """
    action = f"{action_prefix}{tool_name}"
    try:
        verdict = client.submit(
            action,
            "request",
            principal=principal,
            resource=resource,
            logged={"input": args},
            context={"input": args},
        )
    except DogwoodError as exc:
        return {"allowed": False, "result": None, "reason": f"dogwood unavailable: {exc}"}

    if not is_allowed(verdict):
        return {"allowed": False, "result": None, "reason": describe(verdict), "verdict": verdict}

    result = run(args)
    client.submit(
        action,
        "response",
        principal=principal,
        resource=resource,
        logged={"input": args, "output": result if isinstance(result, (str, int, float, bool, list, dict)) else str(result)},
    )
    return {"allowed": True, "result": result, "verdict": verdict}
