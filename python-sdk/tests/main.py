if __name__ == '__main__':
    import time
    from dogwood_sdk import DogwoodClient, DogwoodAdminClient

    CONTROL = "tcp://127.0.0.1:9001"  # 运维侧
    DATA = "tcp://127.0.0.1:9000"  # agent 侧

    # 1) 装一套策略（演示用的：退款必须有一小时内的同额批准）
    admin = DogwoodAdminClient(CONTROL)
    admin.install_files("../../python-demo/demo_policy.dw",
                        "../../python-demo/demo_schema.cedarschema")
    print("策略已装:", admin.status().rule_count, "条规则")

    # 2) 问裁决
    dogwood = DogwoodClient(DATA)
    amount = time.time_ns() % 1_000_000 + 1000  # 每次换个金额，保证下一步是 DENY
    call = dict(principal='AgentCore::User::"alice"',
                resource='AgentCore::Account::"main"',
                logged={"input": {"amount": amount}},
                context={"input": {"amount": amount}})

    print("退款(还没批准):", dogwood.decide("AgentCore::Action::refund", "request", **call))

    # 3) 批准工具跑完了，把结果记进历史（时序规则靠它）
    dogwood.record("AgentCore::Action::approve", "response",
                   principal=call["principal"], resource=call["resource"],
                   logged={"input": {"amount": amount}, "output": {"approved": True}},
                   context={"input": {"amount": amount}})

    # 4) 再问一次
    print("退款(已批准):  ", dogwood.decide("AgentCore::Action::refund", "request", **call))
