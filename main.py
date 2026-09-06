"""Main entry point — pipeline orchestration demo."""

import argparse
import json
from agent import ParserError, parse_user_instruction
from scraper import get_product_price
from notifier import send_email_notification


def mock_demo() -> None:
    print("=" * 60)
    print("  Bargain Agent — Pipeline Initialized")
    print("=" * 60)

    # Step 1: Parse a sample user instruction
    instruction = "Notify me when wireless headphones drop below $200"
    task = {"product_name": "Wireless Headphones (Mock)",
            "url": "https://example.com/product/123", "target_price": 199.99,
            "frequency_minutes": 60}
    print("MOCK DEMO: no LLM, real price check, or email delivery.")
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Request parsing and offline demo")
    options = parser.add_mutually_exclusive_group(required=True)
    options.add_argument("--mock-demo", action="store_true")
    options.add_argument("--instruction", help="Parse with LLM; no task is executed")
    args = parser.parse_args()
    if args.mock_demo:
        mock_demo()
        return 0
    try:
        task = parse_user_instruction(args.instruction)
    except ParserError as exc:
        print(f"Parser error: {exc}")
        return 1
    print(json.dumps(task, ensure_ascii=False, indent=2))
    print("Parse only: no monitoring task created, website fetched, or email sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
