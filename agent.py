"""Agent module — LLM instruction parsing and task scheduling.

To be implemented by Member 4 (Xiao Zhang) and Member 5 (Wenhan Zhang).
"""


def parse_user_instruction(user_instruction: str) -> dict:
    """Parse a natural-language monitoring request into a structured task.

    Args:
        user_instruction: e.g. "Notify me when wireless headphones drop below $200"

    Returns:
        dict with keys: product_name, url, target_price, frequency_minutes
    """
    # TODO: Integrate LLM API to parse real instructions.
    return {
        "product_name": "Wireless Headphones (Mock)",
        "url": "https://example.com/product/123",
        "target_price": 199.99,
        "frequency_minutes": 60,
    }
