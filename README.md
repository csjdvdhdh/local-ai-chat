# Local AI Chat

تطبيق محادثة ذكاء اصطناعي محلي يعمل **بدون سيرفر**.

## المميزات
- تبديل بين نماذج مجانية بدون ما يضيع السياق
- يعمل من جهازك فقط (Ollama محلي + APIs مجانية اختيارية)
- يحفظ المحادثة محلياً
- يُبنى تلقائياً على GitHub Actions

## النماذج المدعومة
| الاسم | النوع | ملاحظات |
|-------|-------|---------|
| ollama / llama3.2 | محلي 100% | يحتاج Ollama مثبت |
| ollama / phi3 | محلي 100% | خفيف |
| ollama / gemma2 | محلي 100% | |
| groq / llama-3.1-8b | مجاني عبر الإنترنت | مفتاح مجاني من groq.com |
| groq / mixtral-8x7b | مجاني عبر الإنترنت | |
| openrouter / free | نماذج مجانية متعددة | مفتاح من openrouter.ai |

## التثبيت السريع
```bash
# 1. ثبت Ollama (محلي)
# https://ollama.com

# 2. سحب نماذج
ollama pull llama3.2
ollama pull phi3

# 3. شغل التطبيق
pip install -r requirements.txt
python app.py
```

## البناء التلقائي
كل push يشغل workflow يتحقق من الكود.
