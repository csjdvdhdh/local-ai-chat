#!/usr/bin/env python3
"""
Local AI Chat — multi-model, context preserved, no server required.
Works offline with Ollama + optional free online keys.
"""

import json
import os
import sys
import time
from pathlib import Path

try:
    import requests
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.prompt import Prompt
    from prompt_toolkit import prompt as pt_prompt
    from prompt_toolkit.history import FileHistory
except ImportError:
    print("pip install -r requirements.txt")
    sys.exit(1)

console = Console()
HISTORY_FILE = Path.home() / ".local_ai_chat_history.json"
CONFIG_FILE = Path.home() / ".local_ai_chat_config.json"
CTX_FILE = Path.home() / ".local_ai_chat_context.json"

# ---------- model registry ----------
MODELS = {
    "1": {
        "id": "ollama/llama3.2",
        "name": "Llama 3.2 (Ollama local)",
        "type": "ollama",
        "model": "llama3.2",
        "base": "http://localhost:11434",
    },
    "2": {
        "id": "ollama/phi3",
        "name": "Phi-3 (Ollama local)",
        "type": "ollama",
        "model": "phi3",
        "base": "http://localhost:11434",
    },
    "3": {
        "id": "ollama/gemma2",
        "name": "Gemma2 (Ollama local)",
        "type": "ollama",
        "model": "gemma2",
        "base": "http://localhost:11434",
    },
    "4": {
        "id": "groq/llama-3.1-8b-instant",
        "name": "Groq Llama-3.1-8B (free key)",
        "type": "openai",
        "model": "llama-3.1-8b-instant",
        "base": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
    },
    "5": {
        "id": "groq/mixtral-8x7b-32768",
        "name": "Groq Mixtral (free key)",
        "type": "openai",
        "model": "mixtral-8x7b-32768",
        "base": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
    },
    "6": {
        "id": "openrouter/auto",
        "name": "OpenRouter free models",
        "type": "openai",
        "model": "openrouter/auto",
        "base": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
    },
}

def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default

def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def load_config():
    return load_json(CONFIG_FILE, {"current": "1", "keys": {}})

def save_config(cfg):
    save_json(CONFIG_FILE, cfg)

def load_context():
    return load_json(CTX_FILE, {"messages": []})

def save_context(ctx):
    save_json(CTX_FILE, ctx)

# ---------- backends ----------
def call_ollama(model_info, messages):
    url = f"{model_info['base']}/api/chat"
    payload = {
        "model": model_info["model"],
        "messages": messages,
        "stream": False,
    }
    r = requests.post(url, json=payload, timeout=120)
    r.raise_for_status()
    return r.json()["message"]["content"]

def call_openai_compat(model_info, messages, api_key):
    url = f"{model_info['base']}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if "openrouter" in model_info["base"]:
        headers["HTTP-Referer"] = "https://github.com/csjdvdhdh/local-ai-chat"
        headers["X-Title"] = "Local AI Chat"
    payload = {
        "model": model_info["model"],
        "messages": messages,
        "temperature": 0.7,
    }
    r = requests.post(url, headers=headers, json=payload, timeout=90)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

def chat(model_info, messages, cfg):
    t = model_info["type"]
    if t == "ollama":
        return call_ollama(model_info, messages)
    if t == "openai":
        key_env = model_info.get("key_env", "")
        key = cfg.get("keys", {}).get(key_env) or os.environ.get(key_env, "")
        if not key:
            raise RuntimeError(f"مفتاح مفقود: ضع {key_env} في الإعدادات أو كمتغير بيئة")
        return call_openai_compat(model_info, messages, key)
    raise RuntimeError("نوع نموذج غير معروف")

# ---------- UI ----------
def show_models(current):
    console.print("\n[bold cyan]النماذج المتاحة[/]")
    for k, m in MODELS.items():
        mark = "→" if k == current else " "
        console.print(f"  {mark} [bold]{k}[/]  {m['name']}")

def switch_model(cfg):
    show_models(cfg["current"])
    choice = Prompt.ask("اختر رقم النموذج", choices=list(MODELS.keys()), default=cfg["current"])
    cfg["current"] = choice
    save_config(cfg)
    console.print(f"[green]تم التبديل إلى:[/] {MODELS[choice]['name']}")
    console.print("[dim]السياق محفوظ — النموذج الجديد يقرأ نفس المحادثة[/]")

def set_key(cfg):
    console.print("\nمفاتيح اختيارية (للنماذج الأونلاين المجانية):")
    console.print("  GROQ_API_KEY      → https://console.groq.com")
    console.print("  OPENROUTER_API_KEY → https://openrouter.ai/keys")
    env = Prompt.ask("اسم المفتاح", choices=["GROQ_API_KEY", "OPENROUTER_API_KEY", "cancel"], default="cancel")
    if env == "cancel":
        return
    val = Prompt.ask(f"قيمة {env}", password=True)
    cfg.setdefault("keys", {})[env] = val.strip()
    save_config(cfg)
    console.print("[green]تم الحفظ[/]")

def clear_context():
    save_context({"messages": []})
    console.print("[yellow]تم مسح السياق[/]")

def main():
    cfg = load_config()
    ctx = load_context()
    console.print(Panel.fit(
        "[bold]Local AI Chat[/]\n"
        "تبديل نماذج بدون ضياع السياق • يعمل بلا سيرفر\n"
        "أوامر: /model  /key  /clear  /quit",
        border_style="cyan"
    ))

    while True:
        mid = cfg.get("current", "1")
        model = MODELS.get(mid, MODELS["1"])
        try:
            user = pt_prompt(
                f"\n[{model['name']}] أنت: ",
                history=FileHistory(str(Path.home() / ".local_ai_chat_input_history")),
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nباي")
            break

        if not user:
            continue
        if user.lower() in ("/q", "/quit", "exit"):
            break
        if user.lower() in ("/model", "/models"):
            switch_model(cfg)
            continue
        if user.lower() in ("/key", "/keys"):
            set_key(cfg)
            continue
        if user.lower() in ("/clear", "/reset"):
            clear_context()
            ctx = load_context()
            continue

        ctx["messages"].append({"role": "user", "content": user})
        # keep last 30 messages to stay light
        if len(ctx["messages"]) > 30:
            ctx["messages"] = ctx["messages"][-30:]

        try:
            with console.status("[bold green]يفكر..."):
                reply = chat(model, ctx["messages"], cfg)
            ctx["messages"].append({"role": "assistant", "content": reply})
            save_context(ctx)
            console.print()
            console.print(Panel(Markdown(reply), title=model["name"], border_style="green"))
        except requests.exceptions.ConnectionError:
            console.print("[red]فشل الاتصال[/] — تأكد أن Ollama شغال: ollama serve")
            ctx["messages"].pop()  # remove last user msg on fail
        except Exception as e:
            console.print(f"[red]خطأ:[/] {e}")
            if ctx["messages"] and ctx["messages"][-1]["role"] == "user":
                ctx["messages"].pop()

if __name__ == "__main__":
    main()
