"""Main entry point — pipeline orchestration demo."""

from agent import parse_user_instruction
from scraper import get_product_price
from notifier import send_email_notification


def main() -> None:
    print("=" * 60)
    print("  Bargain Agent — Pipeline Initialized")
    print("=" * 60)

    # Step 1: Parse a sample user instruction
    instruction = "Notify me when wireless headphones drop below $200"
    task = parse_user_instruction(instruction)
    print(f"\n[1] Parsed task: {task}")

    # Step 2: Mock price check
    price_info = get_product_price(task["url"])
    print(f"[2] Current price: {price_info}")

    # Step 3: Check threshold & notify if deal detected
    if price_info["price"] <= task["target_price"]:
        send_email_notification(
            to_email="user@example.com",
            subject=f"Price Drop Alert: {task['product_name']}",
            body=(
                f"{task['product_name']} is now {price_info['currency']} "
                f"{price_info['price']:.2f} (below your target of "
                f"{task['target_price']:.2f}).\nCheck it out: {task['url']}"
            ),
        )
    else:
        print("[3] No deal detected yet.")

    print("\nPipeline complete.\n")


if __name__ == "__main__":
    main()
