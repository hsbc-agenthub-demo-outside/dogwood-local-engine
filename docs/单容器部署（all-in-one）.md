# 单容器部署（all-in-one）：一个镜像 + `docker run`，不需要 Compose

`Dockerfile.allinone` 把**引擎 + HTTP 适配器（数据面与控制面）**打进**一个容器、一个 uid**，
用最少的组件对外提供 HTTP 裁决接口。适合单机 / 单租户、agent 就是你自己的可信代码、
想用一条 `docker run` 完成部署并方便对接 DevOps/CI 的场景。

> **这不是防篡改拓扑。** 因为适配器用 `--plane both` 把两个面放在同一个进程里，
> "接收 agent 事件的进程"同时也能改策略，适配器会在启动时明确打印这条警告。
> 需要数据面/控制面的 uid 隔离时，用仓库根目录的 `docker-compose.yml`（三个服务），
> 或在 K8s 里把两个面拆成不同 uid 的容器。

---

## 1. 构建与运行

```bash
# 构建
docker build -f Dockerfile.allinone -t dogwood-allinone .

# 运行（数据面 :8080，控制面 :8081，仅发布到本机）
docker run -d --name dogwood \
  --restart unless-stopped \
  -e DOGWOOD_CONTROL_TOKEN="$(openssl rand -hex 32)" \
  -p 127.0.0.1:8080:8080 \
  -p 127.0.0.1:8081:8081 \
  -v dogwood-state:/var/lib/dogwood \
  dogwood-allinone

# Swagger：数据面 http://127.0.0.1:8080/docs  控制面 http://127.0.0.1:8081/docs
```

两条要点：

1. **`-p` 一定要写成 `127.0.0.1:...`**：控制面只靠 bearer token 保护，一旦发布到其他网卡，
   就等于把"改策略的入口"暴露出去。容器内部必须监听 `0.0.0.0`（否则发布端口到不了），
   这是刻意为之，由 `--allow-non-loopback-control` 记录该决定。
2. **`-v dogwood-state:/var/lib/dogwood`** 是持久化的关键：store（`dogwood.redb`）在里面，
   不挂卷则容器重建后历史与策略都丢。

没有 token 时容器**拒绝启动**（退出码 64），不会起一个"看似在工作、实际谁都能改策略"的端点。

---

## 2. 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `DOGWOOD_CONTROL_TOKEN` | 是 | — | 控制面 bearer token |
| `DOGWOOD_CONTROL_TOKEN_FILE` | 二选一 | — | 从文件读 token（Docker/K8s secret 场景） |
| `DOGWOOD_DATA_TOKEN` | 否 | 无 | 设了则数据面也要求 token |
| `DOGWOOD_STORE_DIR` | 否 | `/var/lib/dogwood` | 引擎状态目录 |
| `DOGWOOD_DATA_BIND` | 否 | `0.0.0.0:8080` | 数据面监听地址 |
| `DOGWOOD_CONTROL_BIND` | 否 | `0.0.0.0:8081` | 控制面监听地址 |
| `DOGWOOD_SNAPSHOT_INTERVAL` | 否 | `10000` | 每 N 个事件一次快照；`0` = 关闭 |
| `DOGWOOD_POLICY_FILE` | 否 | — | 启动时自动安装的 `.dw` 策略 |
| `DOGWOOD_ACTION_SCHEMA_FILE` | 随上一项 | — | `.cedarschema`，安装策略时必填 |
| `DOGWOOD_EVENT_SCHEMA_FILE` | 否 | — | `.dwschema`，可选 |
| `DOGWOOD_LOG` | 否 | `dogwood_http=info,warn` | 适配器日志过滤 |

用 secret 文件而不是环境变量，可以避免 token 出现在 `docker inspect` 与 `/proc/<pid>/environ`：

```bash
docker run -d --name dogwood --restart unless-stopped \
  -e DOGWOOD_CONTROL_TOKEN_FILE=/run/secrets/dw_token \
  -v "$PWD/dw_token:/run/secrets/dw_token:ro" \
  -p 127.0.0.1:8080:8080 -p 127.0.0.1:8081:8081 \
  -v dogwood-state:/var/lib/dogwood \
  dogwood-allinone
```

---

## 3. 策略从哪来（两条路）

**A. 启动时自动安装**（声明式，适合把策略文件随部署一起发）：

```bash
docker run -d --name dogwood --restart unless-stopped \
  -e DOGWOOD_CONTROL_TOKEN="$(openssl rand -hex 32)" \
  -e DOGWOOD_POLICY_FILE=/policies/policy.dw \
  -e DOGWOOD_ACTION_SCHEMA_FILE=/policies/schema.cedarschema \
  -v "$PWD/policies:/policies:ro" \
  -p 127.0.0.1:8080:8080 -p 127.0.0.1:8081:8081 \
  -v dogwood-state:/var/lib/dogwood \
  dogwood-allinone
```

> 注意：`install` 是**整集替换**（`[DeleteAll; Add …]`），每次启动都会重装。
> 如果你会通过控制面 API 增量改策略，就**别**设这两个变量，否则重启会覆盖你的改动。

**B. 运行时用控制面 API**（增量，适合长期运行、策略常改）：

```bash
# 首次装一整集（替换）
curl -sS -X POST http://127.0.0.1:8081/v1/policy \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "$(jq -n --rawfile p policy.dw --rawfile s schema.cedarschema \
        '{policy:$p, action_schema:$s}')"

# 之后增量增删改（add / update / delete / delete_all / set_action_schema …）
curl -sS -X POST http://127.0.0.1:8081/v1/policy/verbs \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"verbs":[{"verb":"delete_all"}]}'
```

控制面路由：`GET/POST /v1/policy`、`POST /v1/policy/verbs`、`GET /v1/policies`、
`GET /v1/policies/{id}`、`GET /v1/schema`、`GET /v1/status`、`POST /v1/checkpoint`。

---

## 4. 接口契约（调用方必须记住的）

| 情况 | 状态码 | 调用方 |
|---|---|---|
| allowed | 200 | 执行 |
| **denied** | **403** | 拒绝 |
| recorded（历史事件） | 200 | 无事可做 |
| 请求体非法 / 未知字段 | 400 | 拒绝 |
| 缺 / 错 token | 401 | 拒绝 |
| 引擎错误 / 不可达 | 502 | 拒绝 |
| 引擎超时 | 504 | **拒绝，且不要盲目重试**（引擎不做去重） |

**deny 是 403，不是 `200 + allowed:false`**——这样"看 2xx 就放行"和"不抛异常就放行"
两种天真客户端都是 fail-closed。代价是 deny 会进 4xx 指标，看板记得排除。

---

## 5. 运维

```bash
docker logs -f dogwood                                   # 引擎 + 适配器日志
docker inspect -f '{{.State.Health.Status}}' dogwood     # 健康检查（内置，无需 curl）
docker restart dogwood                                   # 历史与策略从卷恢复
docker exec dogwood dogwood-server --dir /var/lib/dogwood status --json
```

- **健康检查**已内置（`HEALTHCHECK` 打数据面 `/healthz`），无 `curl` 依赖。
- **持久化**：状态全在 `/var/lib/dogwood`；重建容器只要复用同一个卷即可。
- **停止**：SIGTERM 会尽力触发一次 checkpoint；即便跳过，事件日志本身已 fsync，
  重放即可恢复，不会丢已确认的提交。
- **升级**：`docker build` 新镜像 → `docker stop` 旧容器 → 用同一个卷 `docker run` 新镜像。

### K8s 对应形态

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: dogwood }
spec:
  replicas: 1                      # 单机单写者：一个 store 只能被一个进程打开
  selector: { matchLabels: { app: dogwood } }
  template:
    metadata: { labels: { app: dogwood } }
    spec:
      containers:
        - name: dogwood
          image: dogwood-allinone:latest
          ports:
            - { name: data, containerPort: 8080 }
            - { name: control, containerPort: 8081 }
          env:
            - name: DOGWOOD_CONTROL_TOKEN_FILE
              value: /run/secrets/dw_token
          volumeMounts:
            - { name: state, mountPath: /var/lib/dogwood }
            - { name: token, mountPath: /run/secrets/dw_token, subPath: dw_token, readOnly: true }
      volumes:
        - name: state
          persistentVolumeClaim: { claimName: dogwood-state }
        - name: token
          secret: { secretName: dogwood-control-token }
```

控制面 Service 建议只做 `ClusterIP` 并加 NetworkPolicy 限制来源；数据面 Service 给 agent 访问。
**`replicas` 保持 1**：引擎是单机单写者，多副本共享一个卷会互相抢锁。

---

## 6. 与三服务 Compose 的取舍

| | 单容器 all-in-one | 三服务 Compose（`docker-compose.yml`） |
|---|---|---|
| 组件数 | 1 | 3 |
| 部署方式 | `docker build` + `docker run` | `docker compose up` |
| 数据面/控制面隔离 | 否（同进程、同 uid） | 是（不同 uid，数据面连不到 control socket） |
| 适合 | 开发、POC、单租户、可信 agent | 生产、不可信 agent、需要防篡改边界 |

两者可以共存：先用 all-in-one 跑通，等 agent 变得不可信或进入多租户，再切到 Compose/K8s 的分 uid 布局。

---

## 7. 测试用例：启动后自检

`dogwood-http/examples/testcases.py` 是一套**写入规则 → 逐条验证裁决**的用例集，
用来确认引擎与 HTTP 面工作正常。只依赖 Python 3 标准库，**不需要 curl / jq / pip**；
策略与 schema 直接读仓库里的 `python-demo/hsbc/`，与仓库自带的 9 个场景是同一份。

### 7.1 前置检查

```bash
docker ps --filter name=dogwood                                      # 容器在跑
docker inspect -f '{{.State.Health.Status}}' dogwood                 # healthy
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/v1/ping   # 200
```

还没起服务就先按 §1 构建并运行。

### 7.2 准备控制面 token

脚本需要一个**有效的**控制面 token——即容器启动时实际使用的那个，它**不一定等于 `.env` 里的值**。
先验证 `.env` 能不能用：

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $(sed -n 's/^DOGWOOD_CONTROL_TOKEN=//p' .env | tail -1)" \
  http://127.0.0.1:8081/v1/status        # 200 = 对得上；401 = 对不上
```

对不上，就从正在运行的容器里取：

```bash
export DOGWOOD_CONTROL_TOKEN=$(docker inspect dogwood \
  --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | sed -n 's/^DOGWOOD_CONTROL_TOKEN=//p')
```

或者干脆重建容器、把 token 写进 `.env`（之后脚本会自动读，无需 export）：

```bash
docker rm -f dogwood
echo "DOGWOOD_CONTROL_TOKEN=$(openssl rand -hex 32)" > .env
docker run -d --name dogwood --restart unless-stopped --env-file .env \
  -p 127.0.0.1:8080:8080 -p 127.0.0.1:8081:8081 \
  -v dogwood-state:/var/lib/dogwood dogwood-allinone:latest
```

### 7.3 运行

```bash
python3 dogwood-http/examples/testcases.py --all       # 全部（56 条断言）
python3 dogwood-http/examples/testcases.py 01 07 10     # 只跑指定用例
python3 dogwood-http/examples/testcases.py --list       # 列出用例
python3 dogwood-http/examples/testcases.py 01 -v        # 打印每次 allow/deny 的完整内容
```

默认打 `http://127.0.0.1:8080`（数据面）与 `http://127.0.0.1:8081`（控制面）。换端口用
`--data-url` / `--control-url`，或环境变量 `DOGWOOD_DATA_URL` / `DOGWOOD_CONTROL_URL`；
数据面若也设了 token，用 `--data-token` / `DOGWOOD_DATA_TOKEN`。

期望结尾：

```text
56 passed, 0 failed
```

### 7.4 退出码与 CI

全部通过退出码 `0`，有失败为 `1`，可直接进流水线：

```bash
python3 dogwood-http/examples/testcases.py --all || exit 1
```

### 7.5 用例清单

| 编号 | 规则 | 验证点 |
|---|---|---|
| 00 | 服务契约 | ping、控制面无 token 401、带 token 200、未知字段 400 |
| 01 | maker-checker（`formerly`） | 小额 allow → 大额无批准 deny → 批准 → 大额 allow |
| 02 | 频率限制（`count_within` + `::request`） | 前 5 笔 allow，第 6 笔 deny |
| 03 | 累计额度（`sum_within`） | 2 笔 allow，第 3 笔超限 deny |
| 04 | 反异常（`bind` + 聚合） | 结算 1000 后 500 allow / 2000 deny / 800 allow |
| 05 | 信息隔离（`formerly` + 输入过滤） | 对外 allow → 读机密 → 对外 deny，对内仍 allow |
| 06 | 收款人分散度（`count_distinct_within`） | 第 4 个收款人 deny |
| 07 | 制裁名单（`forbid` 覆盖 `permit`） | 普通 allow，SANCTIONED deny |
| 08 | 会话有效性（`!失败 since 成功`） | 未验证 deny → 验证成功 allow → 一次失败后又 deny |
| 09 | 一次批准消费一次（聚合对聚合） | 批准后第 1 笔 allow，第 2 笔 deny |
| 10 | 运行时写入/删除规则（`/v1/policy/verbs`） | 在线加 `forbid` → 立即 deny → 删除 → 恢复 allow |

每个用例换一个全新 principal（历史按 principal 分键）并重装自己的策略集，所以用例之间互不污染，
**同一个服务上可以反复运行**（重跑仍是 56/0，不影响容器里已有的数据）。

### 7.6 allow/deny 的"执行内容"在哪看

**引擎与适配器都不逐个记录裁决**——这是设计使然（Dogwood 不做审计落盘），所以
`docker logs` 里只有启动信息，看不到任何 allow/deny。要看每次决策的完整内容，用 `-v`：

```text
  ok   写入规则：安装 01_dual_approval.dw（2 条规则）
       → 请求: {"action":"HSBC::Action::transfer","kind":"request","principal":"HSBC::User::\"alice-01-…\"",
                "resource":"HSBC::Account::\"main\"","logged":{"input":{"amount":20000,…}},
                "context":{"input":{"amount":20000,…}}}
       ← 响应: http 200 {"status":"decision","allowed":true,
                        "reason":["SPALbxIavsTysZ25burX7sD6"],"recorded_at_nanos":1791860112948100833}
         决定规则: hsbc_large_transfer_needs_approval
```

响应体里三件事值得注意：

- `allowed` 是裁决；HTTP 状态码跟着它走（allow→200，deny→**403**）；
- `reason` 是**决定这次裁决的策略 token**，脚本会反查成 `@id(...)` 的规则名；
- `recorded_at_nanos` 是引擎给事件盖的**不可伪造时间戳**，所有窗口都以它为基准。

三种典型形态：

| 情形 | 响应 |
|---|---|
| 被 `permit` 允许 | `allowed:true` + `reason:[规则]` |
| 被 `forbid` 显式拒绝 | `allowed:false` + `reason:[规则]` |
| **deny by default**（没有 permit 覆盖，也没被 forbid 命中） | `allowed:false` + **无 `reason`** |
