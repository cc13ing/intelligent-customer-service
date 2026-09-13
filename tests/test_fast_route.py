from src.services.workflows.fast_route import resolve_fast_route


def test_fast_route_order_id():
    plan = resolve_fast_route("帮我查一下订单 ORD-1001")
    assert plan == [("query_order", {"order_id": "ORD-1001"})]


def test_fast_route_tracking():
    plan = resolve_fast_route("查物流 SF1234567890")
    assert plan is not None
    assert plan[0][0] == "fetch_logistics_information"
    assert plan[0][1]["logistics_number"].upper().startswith("SF")


def test_fast_route_ticket():
    plan = resolve_fast_route("查询工单 TKT-A1B2C3D4")
    assert plan == [("query_work_order", {"ticket_id": "TKT-A1B2C3D4"})]


def test_fast_route_my_orders_left_to_intent_handler():
    # 「我的订单」由 my_orders_intent 短路，fast_route 不再抢
    assert resolve_fast_route("我的订单到哪了") is None


def test_fast_route_knowledge():
    plan = resolve_fast_route("手机保修多久")
    assert plan == [("customer_chat", {"query": "手机保修多久"})]


def test_fast_route_skips_ambiguous():
    assert resolve_fast_route("今天天气怎么样") is None
