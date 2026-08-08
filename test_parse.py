import sys
import os
# Add parent dir to path so we can import core modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.parser import parser

def run_tests():
    """Test if the parser works."""
    test_cases = [
        {
            "input": "Notify me when Sony headphones drop below $300, check every 5 mins",
            "expected_product": "Sony headphones",
            "expected_price": 300.0,
            "expected_op": "<"
        },
        {
            "input": "Want to buy an iPhone, budget no more than $900, check hourly",
            "expected_product": "iPhone",
            "expected_price": 900.0,
            "expected_op": "<="
        }
    ]
    
    print("Testing parser...")
    for i, case in enumerate(test_cases):
        print(f"\nTest {i+1}: {case['input']}")
        result = parser.parse(case['input'])
        
        assert result is not None, "Parser failed"
        assert result['product_name'] == case['expected_product'], "Product name mismatch"
        assert result['target_price'] == case['expected_price'], "Price mismatch"
        assert result['operator'] == case['expected_op'], "Operator mismatch"
        
        print("✓ Passed")
    
    print("\nAll tests passed.")

if __name__ == "__main__":
    run_tests()