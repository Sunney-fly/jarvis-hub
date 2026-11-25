"""成本记账测试。"""
import pytest

from core.accounting import estimate_cost


def test_estimate_cost_deepseek():
    # 默认价格：deepseek 输入 2 元/百万，输出 8 元/百万
    cost = estimate_cost("deepseek", 1_000_000, 1_000_000)
    assert cost == pytest.approx(10.0)


def test_estimate_cost_partial():
    cost = estimate_cost("deepseek", 500_000, 250_000)
    assert cost == pytest.approx(1.0 + 2.0)


def test_estimate_cost_unknown_provider():
    assert estimate_cost("unknown", 1_000_000, 1_000_000) == 0.0
