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