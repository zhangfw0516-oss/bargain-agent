import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.parser import parser

def test_parsing_accuracy():
    test_cases = [
        {
            "input": "当索尼耳机低于 $300 时通知我，每5分钟检查一次",
            "expected_product": "索尼耳机",
            "expected_price": 300.0,
            "expected_op": "<"
        },
        {
            "input": "我想买 iPhone，预算不超过 900 美元，每小时查一次",
            "expected_product": "iPhone",
            "expected_price": 900.0,
            "expected_op": "<="
        }
    ]
    
    print("===== 开始解析模块单元测试 =====")
    for i, case in enumerate(test_cases):
        print(f"\nCase {i+1}: {case['input']}")
        result = parser.parse(case['input'])
        
        assert result is not None, "解析失败，返回 None"
        assert result['product_name'] == case['expected_product'], \
            f"商品名不匹配: {result['product_name']}"
        assert result['target_price'] == case['expected_price'], \
            f"价格不匹配: {result['target_price']}"
        assert result['operator'] == case['expected_op'], \
            f"操作符不匹配: {result['operator']}"
        
        print(f"✅ Case {i+1} Passed")
    
    print("\n🎉 所有测试用例通过！")

if __name__ == "__main__":
    test_parsing_accuracy()