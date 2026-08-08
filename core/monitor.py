import time
from core.parser import parser
from core.mock_data import get_mock_price

class Monitor:
    def __init__(self, instruction: str):
        self.config = parser.parse(instruction)
        if not self.config:
            raise ValueError("无法解析指令，监控启动失败")
    
    def check_once(self):
        """执行单次检查"""
        print(f"\n[Monitor] 检查商品: {self.config['product_name']}")
        
        current_price = get_mock_price(self.config['target_price'])
        target = self.config['target_price']
        op = self.config['operator']
        
        print(f"[Monitor] 当前价: ${current_price} | 目标价: {op} ${target}")
        
        # 阈值判断
        hit = False
        if op == '<' and current_price < target:
            hit = True
        elif op == '<=' and current_price <= target:
            hit = True
        elif op == '>' and current_price > target:
            hit = True
        elif op == '>=' and current_price >= target:
            hit = True
            
        if hit:
            print(f"[Monitor] 🚨 触发警报！价格达标！")
        else:
            print(f"[Monitor] 😴 未达标，继续监控。")
            
        return hit

# 简易调度器（后续可替换为 schedule 库）
def run_forever(instruction: str, interval_sec=5):
    monitor = Monitor(instruction)
    try:
        while True:
            monitor.check_once()
            time.sleep(interval_sec)
    except KeyboardInterrupt:
        print("\n[Monitor] 程序手动退出。")