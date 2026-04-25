import requests
import sys
from pathlib import Path
from dotenv import load_dotenv
import os

# Завантажуємо ключ з .env
env_path = Path(__file__).parent / '.env'
load_dotenv(dotenv_path=env_path)

API_KEY = os.getenv('NEWS_API_KEY', '')

if not API_KEY:
    print("❌ API_KEY не знайдено!")
    sys.exit(1)

print(f"✅ API_KEY: {API_KEY[:10]}...")

url = "https://newsapi.org/v2/everything"
params = {
    'q': 'cryptocurrency',
    'language': 'en',
    'pageSize': 5,
    'apiKey': API_KEY
}

print(f"📡 Виконую запит до: {url}")
print(f"📋 Параметри: {params}")

try:
    print("⏳ Зачекайте...")
    response = requests.get(url, params=params, timeout=10)
    print(f"✅ Статус: {response.status_code}")

    if response.status_code == 200:
        data = response.json()
        print(f"📰 Отримано {len(data.get('articles', []))} новин")
        for article in data.get('articles', [])[:3]:
            print(f"   - {article.get('title', 'No title')[:80]}")
    else:
        print(f"❌ Помилка: {response.text[:200]}")

except requests.Timeout:
    print("❌ ТАЙМАУТ! Запит тривав більше 10 секунд")
except Exception as e:
    print(f"❌ Помилка: {e}")