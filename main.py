import sys
import os
# Add core folder to import path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from core.monitor import run_forever

if __name__ == "__main__":
    print("===== Discount Monitor Agent =====")
    
    # Example instruction for the agent
    instruction = "Watch Xiaomi Smart Band, alert if price drops below $200, check every 10 mins."
    
    # Start monitoring (use seconds for quick demo, swap to minutes later)
    run_forever(instruction, interval_sec=3)