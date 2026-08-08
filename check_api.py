import os
import sys
from dotenv import load_dotenv
from openai import OpenAI, AuthenticationError, RateLimitError, APIConnectionError, APITimeoutError

load_dotenv()

def check_api():
    """Quick test to see if the API credentials work."""
    
    api_key = os.getenv("LLM_API_KEY")
    base_url = os.getenv("LLM_BASE_URL")
    model = os.getenv("LLM_MODEL")
    
    print("API Test")
    print("-" * 20)
    print(f"URL: {base_url}")
    print(f"Model: {model}")
    
    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=10.0,
        max_retries=0
    )
    
    # Test 1: List models (free call)
    print("\nTest 1: Listing models...")
    try:
        models = client.models.list()
        model_ids = [m.id for m in models.data]
        print(f"Connected. Found {len(model_ids)} models.")
        
        if model not in model_ids:
            print(f"Warning: Model '{model}' not in list.")
    except AuthenticationError:
        print("Auth failed. Check your API key.")
        return False
    except APIConnectionError:
        print("Network error. Check URL or internet.")
        return False
    except Exception as e:
        print(f"Unexpected error: {e}")
        return False
    
    # Test 2: Tiny completion (uses tokens)
    print(f"\nTest 2: Testing inference...")
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5
        )
        print(f"Response: {response.choices[0].message.content}")
        print(f"Tokens used: {response.usage.total_tokens}")
    except RateLimitError:
        print("Rate limited. Key works but no credits.")
        return False
    except Exception as e:
        print(f"Inference failed: {e}")
        return False
    
    print("\nAPI check passed.")
    return True

if __name__ == "__main__":
    success = check_api()
    sys.exit(0 if success else 1)