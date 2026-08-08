import time
from core.parser import parser
from core.mock_data import get_mock_price

class Monitor:
    def __init__(self, instruction: str):
        self.config = parser.parse(instruction)
        if not self.config:
            # Teacher might see this, so keep it clear
            raise ValueError("Couldn't understand the instruction.")

    def check_once(self):
        """Just check the price once."""
        print(f"\nChecking: {self.config['product_name']}")
        
        current_price = get_mock_price(self.config['target_price'])
        target = self.config['target_price']
        op = self.config['operator']
        
        print(f"Current price: ${current_price} | Target: {op} ${target}")
        
        # See if we hit the target price
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
            print("🚨 Price target hit! Time to buy?")
        else:
            print("Not there yet, keep watching.")
            
        return hit

# Simple loop to keep it running
def run_forever(instruction: str, interval_sec=5):
    monitor = Monitor(instruction)
    try:
        while True:
            monitor.check_once()
            time.sleep(interval_sec)
    except KeyboardInterrupt:
        print("\nStopped by user.")