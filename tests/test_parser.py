import sys
import os
# Add parent dir to import path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.parser import parser

def test_parsing_accuracy():
    """Test the instruction parser."""
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
    
    print("Testing parser:")
    for i, case in enumerate(test_cases):
        print(f"Case {i+1}: {case['input']}")
        result = parser.parse(case['input'])
        
        assert result is not None, "Parser returned None"
        assert result['product_name'] == case['expected_product'], \
            f"Product mismatch: got {result['product_name']}"
        assert result['target_price'] == case['expected_price'], \
            f"Price mismatch: got {result['target_price']}"
        assert result['operator'] == case['expected_op'], \
            f"Operator mismatch: got {result['operator']}"
        
        print(f"Case {i+1} passed")
    
    print("All tests passed.")

if __name__ == "__main__":
    test_parsing_accuracy()