import json
import re
from openai import OpenAI
from config.settings import settings

class InstructionParser:
    def __init__(self):
        self.client = OpenAI(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL
        )
    
    def parse(self, instruction: str) -> dict | None:
        """Parse user instruction using LLM."""
        print(f"Parsing: {instruction}")
        
        # Basic prompt to extract fields
        system_prompt = """
        你是一个电商监控指令解析器。请将用户输入转换为JSON格式。
        字段包括: product_name (商品名), target_price (目标价格), operator (<|<=|>|>=), frequency_minutes (检查频率).
        示例: 用户输入"低于200块通知我，每10分钟查一次"
        输出: {"product_name": "商品", "target_price": 200, "operator": "<", "frequency_minutes": 10}
        只输出JSON，不要有任何多余文字。
        """
        
        try:
            response = self.client.chat.completions.create(
                model=settings.LLM_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": instruction}
                ],
                temperature=0
            )
            
            content = response.choices[0].message.content.strip()
            # Remove markdown formatting if present
            content = re.sub(r'^```json\s*|\s*```$', '', content)
            
            result = json.loads(content)
            print(f"Result: {result}")
            return result
            
        except Exception as e:
            print(f"Error: {e}")
            # Fallback to simple rule parsing
            return self._fallback_parse(instruction)

    def _fallback_parse(self, instruction: str) -> dict:
        """Simple parser if API fails."""
        # Find price
        price_match = re.search(r'(\d+)', instruction)
        price = float(price_match.group(1)) if price_match else 0.0
        
        # Find operator
        op = "<"
        if "高于" in instruction or "超过" in instruction:
            op = ">"
            
        # Find frequency
        freq = 10
        if "小时" in instruction:
            freq = 60
            
        return {
            "product_name": "Unknown",
            "target_price": price,
            "operator": op,
            "frequency_minutes": freq
        }

# Global parser instance
parser = InstructionParser()