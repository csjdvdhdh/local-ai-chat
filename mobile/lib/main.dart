import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const LocalAIApp());
}

class LocalAIApp extends StatelessWidget {
  const LocalAIApp({super.key});
  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Local AI Chat',
      debugShowCheckedModeBanner: false,
      theme: ThemeData.dark().copyWith(
        primaryColor: const Color(0xFF6C5CE7),
        scaffoldBackgroundColor: const Color(0xFF0D0D14),
        appBarTheme: const AppBarTheme(backgroundColor: Color(0xFF1A1A24)),
      ),
      home: const ChatScreen(),
    );
  }
}

class ModelInfo {
  final String id, name, type, model, base, keyEnv;
  ModelInfo(this.id, this.name, this.type, this.model, this.base, {this.keyEnv = ''});
}

final models = [
  ModelInfo('ollama/llama3.2', 'Llama 3.2 (Ollama)', 'ollama', 'llama3.2', 'http://127.0.0.1:11434'),
  ModelInfo('ollama/phi3', 'Phi-3 (Ollama)', 'ollama', 'phi3', 'http://127.0.0.1:11434'),
  ModelInfo('ollama/gemma2', 'Gemma2 (Ollama)', 'ollama', 'gemma2', 'http://127.0.0.1:11434'),
  ModelInfo('groq/llama', 'Groq Llama-3.1-8B', 'openai', 'llama-3.1-8b-instant', 'https://api.groq.com/openai/v1', keyEnv: 'GROQ'),
  ModelInfo('groq/mixtral', 'Groq Mixtral', 'openai', 'mixtral-8x7b-32768', 'https://api.groq.com/openai/v1', keyEnv: 'GROQ'),
  ModelInfo('openrouter', 'OpenRouter Free', 'openai', 'openrouter/auto', 'https://openrouter.ai/api/v1', keyEnv: 'OPENROUTER'),
];

class ChatScreen extends StatefulWidget {
  const ChatScreen({super.key});
  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> {
  final _ctrl = TextEditingController();
  final _scroll = ScrollController();
  List<Map<String, String>> messages = [];
  int modelIdx = 0;
  bool loading = false;
  String groqKey = '';
  String openrouterKey = '';
  String ollamaBase = 'http://127.0.0.1:11434';

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final p = await SharedPreferences.getInstance();
    setState(() {
      modelIdx = p.getInt('modelIdx') ?? 0;
      groqKey = p.getString('groqKey') ?? '';
      openrouterKey = p.getString('openrouterKey') ?? '';
      ollamaBase = p.getString('ollamaBase') ?? 'http://127.0.0.1:11434';
      final raw = p.getString('messages');
      if (raw != null) {
        messages = (jsonDecode(raw) as List).map((e) => Map<String, String>.from(e)).toList();
      }
    });
  }

  Future<void> _save() async {
    final p = await SharedPreferences.getInstance();
    await p.setInt('modelIdx', modelIdx);
    await p.setString('groqKey', groqKey);
    await p.setString('openrouterKey', openrouterKey);
    await p.setString('ollamaBase', ollamaBase);
    await p.setString('messages', jsonEncode(messages));
  }

  Future<String> _call(List<Map<String, String>> msgs) async {
    final m = models[modelIdx];
    if (m.type == 'ollama') {
      final r = await http.post(
        Uri.parse('$ollamaBase/api/chat'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({'model': m.model, 'messages': msgs, 'stream': false}),
      ).timeout(const Duration(seconds: 120));
      if (r.statusCode != 200) throw Exception('Ollama ${r.statusCode}: ${r.body}');
      return jsonDecode(r.body)['message']['content'] as String;
    }
    // openai compat
    final key = m.keyEnv == 'GROQ' ? groqKey : openrouterKey;
    if (key.isEmpty) throw Exception('أضف مفتاح ${m.keyEnv} من الإعدادات');
    final headers = {
      'Authorization': 'Bearer $key',
      'Content-Type': 'application/json',
    };
    if (m.keyEnv == 'OPENROUTER') {
      headers['HTTP-Referer'] = 'https://github.com/csjdvdhdh/local-ai-chat';
      headers['X-Title'] = 'Local AI Chat';
    }
    final r = await http.post(
      Uri.parse('${m.base}/chat/completions'),
      headers: headers,
      body: jsonEncode({'model': m.model, 'messages': msgs, 'temperature': 0.7}),
    ).timeout(const Duration(seconds: 90));
    if (r.statusCode != 200) throw Exception('${r.statusCode}: ${r.body}');
    return jsonDecode(r.body)['choices'][0]['message']['content'] as String;
  }

  Future<void> _send() async {
    final text = _ctrl.text.trim();
    if (text.isEmpty || loading) return;
    setState(() {
      messages.add({'role': 'user', 'content': text});
      if (messages.length > 40) messages = messages.sublist(messages.length - 40);
      loading = true;
      _ctrl.clear();
    });
    _save();
    _scrollToEnd();
    try {
      final reply = await _call(messages);
      setState(() {
        messages.add({'role': 'assistant', 'content': reply});
        loading = false;
      });
    } catch (e) {
      setState(() {
        messages.add({'role': 'assistant', 'content': 'خطأ: $e'});
        loading = false;
      });
    }
    _save();
    _scrollToEnd();
  }

  void _scrollToEnd() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients) {
        _scroll.animateTo(_scroll.position.maxScrollExtent,
            duration: const Duration(milliseconds: 250), curve: Curves.easeOut);
      }
    });
  }

  void _clear() {
    setState(() => messages = []);
    _save();
  }

  void _showModels() {
    showModalBottomSheet(
      context: context,
      backgroundColor: const Color(0xFF1A1A24),
      builder: (_) => ListView.builder(
        shrinkWrap: true,
        itemCount: models.length,
        itemBuilder: (_, i) {
          final m = models[i];
          return ListTile(
            title: Text(m.name, style: const TextStyle(color: Colors.white)),
            subtitle: Text(m.type, style: const TextStyle(color: Colors.white54)),
            trailing: i == modelIdx ? const Icon(Icons.check, color: Color(0xFF6C5CE7)) : null,
            onTap: () {
              setState(() => modelIdx = i);
              _save();
              Navigator.pop(context);
              ScaffoldMessenger.of(context).showSnackBar(SnackBar(
                content: Text('تم التبديل إلى ${m.name} — السياق محفوظ'),
                duration: const Duration(seconds: 2),
              ));
            },
          );
        },
      ),
    );
  }

  void _showSettings() {
    final gCtrl = TextEditingController(text: groqKey);
    final oCtrl = TextEditingController(text: openrouterKey);
    final bCtrl = TextEditingController(text: ollamaBase);
    showDialog(
      context: context,
      builder: (_) => AlertDialog(
        backgroundColor: const Color(0xFF1A1A24),
        title: const Text('إعدادات', style: TextStyle(color: Colors.white)),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(
                controller: bCtrl,
                style: const TextStyle(color: Colors.white),
                decoration: const InputDecoration(
                  labelText: 'Ollama Base URL',
                  labelStyle: TextStyle(color: Colors.white70),
                  hintText: 'http://127.0.0.1:11434',
                ),
              ),
              TextField(
                controller: gCtrl,
                style: const TextStyle(color: Colors.white),
                obscureText: true,
                decoration: const InputDecoration(
                  labelText: 'GROQ_API_KEY',
                  labelStyle: TextStyle(color: Colors.white70),
                ),
              ),
              TextField(
                controller: oCtrl,
                style: const TextStyle(color: Colors.white),
                obscureText: true,
                decoration: const InputDecoration(
                  labelText: 'OPENROUTER_API_KEY',
                  labelStyle: TextStyle(color: Colors.white70),
                ),
              ),
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: () {
              setState(() {
                groqKey = gCtrl.text.trim();
                openrouterKey = oCtrl.text.trim();
                ollamaBase = bCtrl.text.trim().isEmpty ? 'http://127.0.0.1:11434' : bCtrl.text.trim();
              });
              _save();
              Navigator.pop(context);
            },
            child: const Text('حفظ'),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final m = models[modelIdx];
    return Scaffold(
      appBar: AppBar(
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('Local AI Chat', style: TextStyle(fontSize: 16)),
            Text(m.name, style: const TextStyle(fontSize: 11, color: Colors.white70)),
          ],
        ),
        actions: [
          IconButton(icon: const Icon(Icons.swap_horiz), onPressed: _showModels, tooltip: 'تبديل النموذج'),
          IconButton(icon: const Icon(Icons.settings), onPressed: _showSettings),
          IconButton(icon: const Icon(Icons.delete_outline), onPressed: _clear, tooltip: 'مسح السياق'),
        ],
      ),
      body: Column(
        children: [
          Expanded(
            child: ListView.builder(
              controller: _scroll,
              padding: const EdgeInsets.all(12),
              itemCount: messages.length + (loading ? 1 : 0),
              itemBuilder: (_, i) {
                if (loading && i == messages.length) {
                  return const Padding(
                    padding: EdgeInsets.all(12),
                    child: Text('يفكر...', style: TextStyle(color: Colors.white54)),
                  );
                }
                final msg = messages[i];
                final isUser = msg['role'] == 'user';
                return Align(
                  alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
                  child: Container(
                    margin: const EdgeInsets.symmetric(vertical: 4),
                    padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
                    constraints: BoxConstraints(maxWidth: MediaQuery.of(context).size.width * 0.8),
                    decoration: BoxDecoration(
                      color: isUser ? const Color(0xFF6C5CE7) : const Color(0xFF1E1E2A),
                      borderRadius: BorderRadius.circular(14),
                    ),
                    child: Text(msg['content'] ?? '', style: const TextStyle(color: Colors.white, height: 1.35)),
                  ),
                );
              },
            ),
          ),
          Container(
            padding: const EdgeInsets.fromLTRB(12, 8, 12, 12),
            color: const Color(0xFF1A1A24),
            child: SafeArea(
              child: Row(
                children: [
                  Expanded(
                    child: TextField(
                      controller: _ctrl,
                      style: const TextStyle(color: Colors.white),
                      decoration: InputDecoration(
                        hintText: 'اكتب رسالة...',
                        hintStyle: const TextStyle(color: Colors.white38),
                        filled: true,
                        fillColor: const Color(0xFF0D0D14),
                        border: OutlineInputBorder(borderRadius: BorderRadius.circular(24), borderSide: BorderSide.none),
                        contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
                      ),
                      onSubmitted: (_) => _send(),
                    ),
                  ),
                  const SizedBox(width: 8),
                  CircleAvatar(
                    backgroundColor: const Color(0xFF6C5CE7),
                    child: IconButton(
                      icon: const Icon(Icons.send, color: Colors.white, size: 20),
                      onPressed: loading ? null : _send,
                    ),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}
