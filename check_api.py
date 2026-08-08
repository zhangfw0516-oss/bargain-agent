import os
import sys
from dotenv import load_dotenv
from openai import OpenAI, AuthenticationError, RateLimitError, APIConnectionError, APITimeoutError

load_dotenv()

def check_api():
    """探针函数：用最小代价验证 API 是否可用"""
    
    api_key = os.getenv("LLM_API_KEY")
    base_url = os.getenv("LLM_BASE_URL")
    model = os.getenv("LLM_MODEL")
    
    print("=" * 50)
    print("🔬 API 探针测试")
    print("=" * 50)
    print(f"Base URL: {base_url}")
    print(f"Model: {model}")
    print(f"API Key: {api_key[:10]}...（已隐藏）")
    print("-" * 50)
    
    # 关键：设置短超时 + 禁用自动重试，避免无效 Key 反复请求
    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=15.0,        # 15秒超时
        max_retries=0        # 不重试，快速失败
    )
    
    # 第一步：测试 models.list()（免费、只读）
    print("\n[测试1] 调用 models.list() 验证 Key 和 Base URL...")
    try:
        models = client.models.list()
        model_ids = [m.id for m in models.data]
        print(f"✅ 连接成功！该平台可用模型数量: {len(model_ids)}")
        # 检查你配置的 model 是否在列表中
        if model in model_ids:
            print(f"✅ 你配置的模型 '{model}' 存在！")
        else:
            print(f"⚠️ 警告：你配置的模型 '{model}' 不在可用列表里")
            print(f"   可用模型示例: {model_ids[:5]}")
    except AuthenticationError as e:
        print(f"❌ 认证失败：Key 无效/已撤销/权限不足")
        print(f"   详情: {e}")
        return False
    except APIConnectionError as e:
        print(f"❌ 网络错误：无法连接到 {base_url}")
        print(f"   详情: {e}")
        return False
    except APITimeoutError as e:
        print(f"❌ 超时错误：15秒内未收到响应")
        print(f"   详情: {e}")
        return False
    except RateLimitError as e:
        print(f"⚠️ 速率限制：Key 有效但触发了限流（免费额度可能已用完）")
        print(f"   详情: {e}")
        # 速率限制不代表 Key 完全不可用，继续往下测
    except Exception as e:
        print(f"❌ 未知错误: {type(e).__name__}: {e}")
        return False
    
    # 第二步：发一条极小的 chat 请求（消耗极少额度，验证推理能力）
    print(f"\n[测试2] 发送最小 chat 请求（model={model}）...")
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,           # 限制输出长度，减少消耗
            temperature=0
        )
        reply = response.choices[0].message.content
        print(f"✅ 推理测试成功！模型回复: '{reply}'")
        print(f"✅ 消耗 tokens: {response.usage.total_tokens}")
    except RateLimitError as e:
        print(f"❌ 额度不足/速率限制：{e}")
        print("   👉 这意味着 Key 有效，但你的账户没有足够的额度/免费额度已耗尽")
        return False
    except Exception as e:
        print(f"❌ 推理测试失败: {type(e).__name__}: {e}")
        return False
    
    print("\n" + "=" * 50)
    print("🎉 全部测试通过！这个 API 可以安全接入项目。")
    print("=" * 50)
    return True

if __name__ == "__main__":
    success = check_api()
    sys.exit(0 if success else 1)