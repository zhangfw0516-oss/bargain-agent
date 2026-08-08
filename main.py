<<<<<<< HEAD
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from core.monitor import run_forever

if __name__ == "__main__":
    print("===== 智能折扣监控 Agent =====")
    
    # 这里可以从命令行参数或配置文件读取指令
    instruction = "帮我盯着小米手环，如果低于200块就告诉我，每10分钟查一次。"
    
    # 启动监控（为了方便演示，这里用秒，实际用分钟）
    run_forever(instruction, interval_sec=3)
=======
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
>>>>>>> de9343f3d6f00c97242d02fcdcdb03f2fb86043a
