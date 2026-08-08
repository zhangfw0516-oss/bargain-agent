import json
import re
import random
from openai import OpenAI
from config.settings import settings

client = OpenAI(
    api_key=settings.LLM_API_KEY,
    base_url=settings.LLM_BASE_URL
)

def parse_user_instruction(instruction: str) -> dict | None:
    """Parse user instruction into structured JSON using LLM."""
    
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

    # Few-shot examples to guide the LLM
    messages = [
        {"role": "system", "content": f"Output JSON only. Schema: {json.dumps(schema)}"},
        {"role": "user", "content": "Notify me when Sony headphones drop below $300. Check every 5 mins."},
        {"role": "assistant", "content": json.dumps({"product_name": "Sony headphones", "target_price": 300, "frequency_minutes": 5, "operator": "<"})},
        {"role": "user", "content": "iPhone under $900, check hourly."},
        {"role": "assistant", "content": json.dumps({"product_name": "iPhone", "target_price": 900, "frequency_minutes": 60, "operator": "<="})},
        {"role": "user", "content": instruction}
    ]

    try:
        response = client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=messages,
            temperature=0
        )
        
        content = response.choices[0].message.content.strip()
        # Strip markdown code blocks if present
        content = re.sub(r'^```json\s*|\s*```$', '', content)
        return json.loads(content)

    except Exception as e:
        print(f"Parse error: {e}")
        return None

def run_monitoring_task(task_json):
    """Simulate a price check."""
    if not task_json:
        return

    # Simulate price fluctuation
    current_price = round(random.uniform(
        task_json['target_price'] * 0.7, 
        task_json['target_price'] * 1.3
    ), 2)
    
    print(f"\nChecking: {task_json['product_name']}")
    print(f"Current: ${current_price} | Target: {task_json['operator']} ${task_json['target_price']}")

    # Check threshold
    hit = False
    if task_json['operator'] == '<' and current_price < task_json['target_price']:
        hit = True
    elif task_json['operator'] == '<=' and current_price <= task_json['target_price']:
        hit = True

    if hit:
        print("🚨 Alert! Price target hit.")
    else:
        print("Target not met.")

# Quick local test (optional)
if __name__ == "__main__":
    test_instruction = "Check if MacBook is under $1000, every 10 mins."
    result = parse_user_instruction(test_instruction)
    print(f"Result: {result}")
    if result:
        run_monitoring_task(result)