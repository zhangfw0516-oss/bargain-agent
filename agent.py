<<<<<<< HEAD
import os
import json
import re
import random
from openai import OpenAI
from dotenv import load_dotenv

# 加载 .env 文件里的密钥
load_dotenv()

# 初始化 OpenAI 客户端
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def parse_user_instruction(instruction: str):
    """
    使用 LLM 将自然语言指令解析为结构化 JSON
    """
    print(f"[DEBUG] 正在解析指令: {instruction}")
    
    # 定义我们希望得到的 JSON 结构
    schema = {
        "type": "object",
        "properties": {
            "product_name": {"type": "string"},
            "target_price": {"type": "number"},
            "frequency_minutes": {"type": "integer"},
            "operator": {"type": "string", "enum": ["<", "<=", "=="]}
        },
        "required": ["product_name", "target_price", "frequency_minutes"]
    }

    # 系统提示词 + 少量示例 (Few-Shot Prompting)
    system_prompt = f"""
    你是一个专业的电商监控指令解析器。请将用户的自然语言指令转换为严格的 JSON 格式。
    请只输出 JSON 内容，不要包含任何 Markdown 标记或解释性文字。
    输出必须遵循以下 Schema:
    {json.dumps(schema, ensure_ascii=False)}
    """

    # 给 AI 看的例子，教它怎么转换
    few_shots = [
        {"role": "user", "content": "Notify me when the Sony headphones drop below $300. Check every 5 minutes."},
        {"role": "assistant", "content": json.dumps({"product_name": "Sony headphones", "target_price": 300, "frequency_minutes": 5, "operator": "<"})},
        {"role": "user", "content": "I want to know if the iPhone is under 900 dollars. Check hourly."},
        {"role": "assistant", "content": json.dumps({"product_name": "iPhone", "target_price": 900, "frequency_minutes": 60, "operator": "<="})}
    ]

    try:
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "system", "content": system_prompt}] + few_shots + [{"role": "user", "content": instruction}],
            temperature=0 # 设置为 0，让输出更稳定，便于解析
        )
        
        content = response.choices[0].message.content.strip()
        # 清理可能存在的 Markdown 代码块标记
        content = re.sub(r'^```json\s*|\s*```$', '', content, flags=re.IGNORECASE)
        
        print(f"[DEBUG] LLM 返回的原始结果: {content}")
        return json.loads(content)

    except Exception as e:
        print(f"[ERROR] 调用 OpenAI API 或解析 JSON 失败: {e}")
        return None

def run_monitoring_task(task_json):
    """
    模拟监控任务：随机生成一个价格并检查是否满足触发条件
    """
    if not task_json:
        return

    # 模拟当前商品价格波动（围绕目标价的 70% 到 130% 之间）
    current_price = round(random.uniform(task_json['target_price'] * 0.7, task_json['target_price'] * 1.3), 2)
    
    print(f"\n[监控中] 商品: {task_json['product_name']}")
    print(f"         当前模拟价格: ${current_price}")
    print(f"         目标阈值: {task_json['operator']} ${task_json['target_price']}")

    # 判断是否触发通知
    should_alert = False
    if task_json['operator'] == '<' and current_price < task_json['target_price']:
        should_alert = True
    elif task_json['operator'] == '<=' and current_price <= task_json['target_price']:
        should_alert = True
    elif task_json['operator'] == '==' and current_price == task_json['target_price']:
        should_alert = True

    if should_alert:
        print(f"🚨 [ALERT] 价格达标！{task_json['product_name']} 现在只要 ${current_price}！")
    else:
        print(f"ℹ️ [INFO] 价格未达标，继续监控...")

if __name__ == "__main__":
    # 单独测试解析功能
    instruction = "帮我盯着小米手环，如果低于200块就告诉我，每10分钟查一次。"
    parsed_data = parse_user_instruction(instruction)
    print(f"解析结果: {parsed_data}")
    
    # 单独测试监控功能
    if parsed_data:
        run_monitoring_task(parsed_data)
=======
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
>>>>>>> de9343f3d6f00c97242d02fcdcdb03f2fb86043a
