#!/usr/bin/env bash
#
# 本地调试用的便捷脚本。
#
# 跑的东西和部署**完全一样**——同一个镜像、同样的两个 TCP 端口、同一个启动方式——
# 只是把本地每次都要做的几步合成一条命令：构建、起容器、等就绪、装一套 demo 策略、
# 把地址打出来。
#
#   ./dev.sh up       构建 + 起 + 装 demo 策略 + 打印地址
#   ./dev.sh test     在宿主机上跑 SDK 测试套件（走 tcp://）
#   ./dev.sh logs     跟踪容器日志
#   ./dev.sh status   容器状态 + 引擎里装了什么
#   ./dev.sh down     停并删容器（数据卷保留）
#   ./dev.sh reset    连数据卷一起删，下次 up 是全新引擎
#
# 可覆盖（环境变量）：
#   DOGWOOD_DEV_NAME=dogwood           容器名
#   DOGWOOD_DEV_IMAGE=dogwood-socket-allinone
#   DOGWOOD_DEV_PORT_DATA=9000         数据面 TCP 端口
#   DOGWOOD_DEV_PORT_CONTROL=9001      控制面 TCP 端口
#   DOGWOOD_DEV_VOLUME=dogwood-state

set -euo pipefail

ROOT=$(cd "$(dirname "$0")" && pwd)
NAME=${DOGWOOD_DEV_NAME:-dogwood}
IMAGE=${DOGWOOD_DEV_IMAGE:-dogwood-socket-allinone}
DATA_PORT=${DOGWOOD_DEV_PORT_DATA:-9000}
CONTROL_PORT=${DOGWOOD_DEV_PORT_CONTROL:-9001}
VOLUME=${DOGWOOD_DEV_VOLUME:-dogwood-state}
DATA="tcp://127.0.0.1:$DATA_PORT"
CONTROL="tcp://127.0.0.1:$CONTROL_PORT"
DEMO=/opt/dogwood/examples

say() { printf 'dev: %s\n' "$*" >&2; }

# 引擎就绪 = 它的数据 socket 能 ping 通。没有 HTTP 探针可用——这套部署不提供 HTTP。
wait_ready() {
    for _ in $(seq 1 60); do
        if docker exec "$NAME" dogwood-server --dir /var/lib/dogwood ping >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    say "60 秒内没起来；看日志： ./dev.sh logs"
    return 1
}

cmd_up() {
    say "构建镜像 ${IMAGE}"
    docker build -f "$ROOT/Dockerfile.socket-allinone" -t "$IMAGE" "$ROOT" >/dev/null

    docker rm -f "$NAME" >/dev/null 2>&1 || true
    say "起容器 ${NAME}（数据面 tcp://127.0.0.1:${DATA_PORT}，控制面 tcp://127.0.0.1:${CONTROL_PORT}）"
    docker run -d --name "$NAME" --restart unless-stopped \
        -e "DOGWOOD_TCP_DATA_BIND=0.0.0.0:$DATA_PORT" \
        -e "DOGWOOD_TCP_CONTROL_BIND=0.0.0.0:$CONTROL_PORT" \
        -p "127.0.0.1:$DATA_PORT:$DATA_PORT" -p "127.0.0.1:$CONTROL_PORT:$CONTROL_PORT" \
        -v "$VOLUME:/var/lib/dogwood" \
        -v "${VOLUME}-run:/var/lib/dogwood/run" \
        -v "${VOLUME}-run:/run/dogwood" \
        "$IMAGE" >/dev/null

    say "等引擎就绪…"
    wait_ready

    # 用镜像里自带的 demo 策略。它走的是容器内的控制 socket（不是 TCP），所以顺带证明
    # 控制 socket 本身是活的。
    say "装 demo 策略"
    docker exec "$NAME" dogwood-entrypoint install \
        "$DEMO/demo_policy.dw" "$DEMO/demo_schema.cedarschema" >/dev/null

    cat >&2 <<EOF

就绪：
  数据面  ${DATA}     （上报事件、拿裁决）
  控制面  ${CONTROL}     （装/改策略）

  Python：
    DogwoodClient("${DATA}")
    DogwoodAdminClient("${CONTROL}")

⚠️ 控制面端口谁能连谁就能改策略（转发器以引擎的 uid 连 socket）。这里只发布到
   loopback；要暴露到别处，前面得放认证反代。

  ./dev.sh test     跑 SDK 测试套件（在宿主机上，走 tcp://）
  ./dev.sh status   看引擎里装了什么
  ./dev.sh reset    清空重来
EOF
}

cmd_test() {
    cd "$ROOT"
    PYTHONPATH="$ROOT/python-sdk" \
        DOGWOOD_ENDPOINT="$DATA" DOGWOOD_CONTROL_ENDPOINT="$CONTROL" \
        python3 -m unittest discover -s python-sdk/tests
}

cmd_logs() { docker logs -f "$NAME"; }

cmd_status() {
    docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' | rg "${NAME}|NAMES" || true
    echo
    docker exec "$NAME" dogwood-server --dir /var/lib/dogwood status --json
}

cmd_down() {
    docker rm -f "$NAME" >/dev/null 2>&1 && say "容器 ${NAME} 已删（数据卷保留）" || say "容器本来就不在"
}

cmd_reset() {
    cmd_down
    docker volume rm "$VOLUME" "${VOLUME}-run" >/dev/null 2>&1 \
        && say "数据卷已删——下次 up 是全新引擎" || true
}

case "${1:-up}" in
    up) cmd_up ;;
    test) cmd_test ;;
    logs) cmd_logs ;;
    status) cmd_status ;;
    down) cmd_down ;;
    reset) cmd_reset ;;
    *) say "未知命令：${1}（可用：up / test / logs / status / down / reset）"; exit 64 ;;
esac
