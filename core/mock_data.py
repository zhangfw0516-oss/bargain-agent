import random

def get_mock_price(base_price: float) -> float:
    """模拟价格波动"""
    fluctuation = random.uniform(-0.3, 0.3) # 波动 ±30%
    return round(base_price * (1 + fluctuation), 2)