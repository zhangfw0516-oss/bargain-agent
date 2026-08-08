import random

def get_mock_price(base_price: float) -> float:
    """Fake a price swing for testing."""
    # ±30% fluctuation to simulate market volatility
    fluctuation = random.uniform(-0.3, 0.3)
    return round(base_price * (1 + fluctuation), 2)