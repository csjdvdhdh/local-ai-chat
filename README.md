# Local AI Chat

تطبيق محادثة ذكاء اصطناعي **يعمل على الهاتف والكمبيوتر** بدون سيرفر.

## نسختان
| المسار | المنصة |
|--------|--------|
| `app.py` | كمبيوتر / Termux (Python) |
| `mobile/` | Android (Flutter) → APK |

## مميزات الهاتف
- تبديل النماذج بدون ضياع السياق
- Ollama محلي (لو شغال على نفس الشبكة أو Termux)
- Groq + OpenRouter مجاني
- يحفظ المحادثة محلياً
- يُبنى APK تلقائي من GitHub Actions

## بناء APK محلياً
```bash
cd mobile
flutter pub get
flutter build apk --release
```
الـ APK: `mobile/build/app/outputs/flutter-apk/app-release.apk`

## تحميل APK من GitHub
Actions → Build Mobile APK → Artifacts → `local-ai-chat-apk`

## تشغيل Python (كمبيوتر / Termux)
```bash
pip install -r requirements.txt
python app.py
```
