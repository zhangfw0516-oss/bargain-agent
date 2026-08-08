from agent import parse_user_instruction

def run_tests():
    """
    运行一系列测试用例，验证解析器的准确性
    """
    tests = [
        {
            "input": "苹果手机如果能降到8000元以下，每5分钟提醒我一次",
            "expected_product": "苹果手机",
            "expected_price": 8000,
            "expected_op": "<"
        },
        {
            "input": "我想买个联想的笔记本，预算不超过5000，每天检查一次",
            "expected_product": "联想的笔记本",
            "expected_price": 5000,
            "expected_op": "<="
        },
        {
            "input": "看看显卡啥时候跌破4000块，每小时看一眼就行",
            "expected_product": "显卡",
            "expected_price": 4000,
            "expected_op": "<"
        },
        {
            "input": "Check if Playstation 5 is below $450 at Amazon, monitor every 2 mins.",
            "expected_product": "Playstation 5",
            "expected_price": 450,
            "expected_op": "<"
        }
    ]

    print("=" * 60)
    print("🚀 开始运行自然语言解析测试...")
    print("=" * 60)
    
    passed = 0
    failed = 0

    for i, test in enumerate(tests):
        print(f"\n--- 测试用例 {i+1} ---")
        print(f"输入: {test['input']}")
        
        result = parse_user_instruction(test['input'])
        
        if result:
            # 验证结果是否符合预期
            if (result.get('product_name') == test['expected_product'] and
                result.get('target_price') == test['expected_price'] and
                result.get('operator') == test['expected_op']):
                print(f"✅ 测试通过!")
                print(f"   解析结果: {result}")
                passed += 1
            else:
                print(f"❌ 测试失败!")
                print(f"   期望: Product={test['expected_product']}, Price={test['expected_price']}, Op={test['expected_op']}")
                print(f"   实际: {result}")
                failed += 1
        else:
            print(f"❌ 测试失败! 解析返回为空。")
            failed += 1
        print("-" * 60)

    print("\n📊 测试总结")
    print(f"总计: {len(tests)}")
    print(f"通过: {passed}")
    print(f"失败: {failed}")
    print(f"准确率: {(passed/len(tests))*100:.2f}%")

if __name__ == "__main__":
    run_tests()