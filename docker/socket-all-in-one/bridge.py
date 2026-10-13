#!/usr/bin/env python3
"""TCP → Unix socket 转发器：让引擎**自己的协议**能被另一个机器访问。

# 它做什么

在一个 TCP 端口上接受连接，为每个连接开一条到引擎 Unix socket 的连接，然后双向搬运
字节。**不解析协议**——帧格式（4 字节长度 + JSON）对一个纯搬运工来说是透明的，这也意味着
引擎那边加动词不用改这里。

# 一条连接对一个上游连接，不能合并

必须的。多路复用会把不同客户端的帧交错进同一条上游流里，而帧边界没有客户端标识——
两个客户端各发半帧，引擎读到的就是垃圾。所以每个 TCP 连接独占一条上游连接。

代价是连接数：引擎每个 socket 上限 256 个并发连接，所以每个转发端口也一样，超了客户端
会被引擎直接关掉（转发器表现为连接立刻断开）。

# 谁被认证了

转发器以**自己的 uid** 连引擎，所以引擎看到的调用者是转发器，不是 TCP 那头的人。
于是：

* **数据面**：影响不大——数据 socket 本来就是 `0666`，谁都能连；
* **控制面**：这就是全部的授权故事了。控制 socket 是 `0700` + 内核 uid 白名单，转发器
  以引擎的 uid 运行（否则它连不上），于是**任何能连到这个 TCP 端口的人都能改策略**。

所以控制面转发是要刻意开、并且刻意收窄的（只绑 loopback，或者前面放真做认证的反代）。
想保留"真实的 OS 身份"就用 SSH 转发 socket（`ssh -L local.sock:remote.sock`），
那对 SDK 来说仍然是 `unix://`。

用法:
    bridge.py --listen 0.0.0.0:9000 --unix /var/lib/dogwood/run/dogwood.sock --label data
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading

BUFFER = 65536


def pump(source: socket.socket, sink: socket.socket) -> None:
    """把一个方向的字节搬完，然后半关对端（让另一端读到 EOF 而不是卡住）。"""
    try:
        while True:
            chunk = source.recv(BUFFER)
            if not chunk:
                break
            sink.sendall(chunk)
    except OSError:
        # 任一端断了都是正常结果：TCP 客户端提前关闭、引擎回收空闲连接、等等。
        pass
    finally:
        try:
            sink.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle(client: socket.socket, unix_path: str, label: str) -> None:
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        upstream.connect(unix_path)
    except OSError as exc:
        log(f"{label}: 连不上 {unix_path}: {exc}")
        client.close()
        return

    to_upstream = threading.Thread(target=pump, args=(client, upstream), daemon=True)
    to_client = threading.Thread(target=pump, args=(upstream, client), daemon=True)
    to_upstream.start()
    to_client.start()
    # 两条泵都结束才算这条连接结束（各自会在结束时半关对端）。
    to_upstream.join()
    to_client.join()
    client.close()
    upstream.close()


def log(message: str) -> None:
    print(f"bridge: {message}", file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="TCP → Unix socket forwarder")
    parser.add_argument("--listen", required=True, help="host:port to listen on")
    parser.add_argument("--unix", required=True, help="the engine socket to relay to")
    parser.add_argument("--label", default="socket", help="name used in log lines")
    args = parser.parse_args()

    host, _, port = args.listen.rpartition(":")
    try:
        address = (host or "0.0.0.0", int(port))
    except ValueError:
        log(f"--listen 需要 host:port，收到 {args.listen!r}")
        return 64

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(address)
    except OSError as exc:
        log(f"bind {args.listen}: {exc}")
        return 1
    server.listen(128)

    log(f"{args.label}: tcp {args.listen} -> {args.unix}（uid {os.getuid()}）")
    if args.label == "control":
        log(
            "control: 警告——引擎认证的是本转发器（不是 TCP 那头的人），所以能连到这个"
            "端口的人都能改策略。容器里必须绑 0.0.0.0 才能被发布的端口投递到，收窄要写在"
            " `-p` 上（例如 -p 127.0.0.1:9001:9001），或者前面放一个真做认证的反代。"
        )

    while True:
        try:
            client, _ = server.accept()
        except OSError:
            continue
        threading.Thread(target=handle, args=(client, args.unix, args.label), daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
