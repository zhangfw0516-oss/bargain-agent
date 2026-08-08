import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    # LLM Config
    LLM_API_KEY = os.getenv("LLM_API_KEY")
    LLM_BASE_URL = os.getenv("LLM_BASE_URL")
    LLM_MODEL = os.getenv("LLM_MODEL")
    
    # Monitor Config
    CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MINUTES", 1))

settings = Settings()