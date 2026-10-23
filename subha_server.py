"""Subha AI - a voice friend (Tamil / English / Tanglish). Deployable version.

Config comes only from environment variables (no secrets in this file):
  SUBHA_API_KEYS         comma-separated Gemini keys (required)
  SUBHA_ACCESS_TOKEN     passcode the app must send (required, long random string)
  SUBHA_API_MODEL        default gemini-3.5-flash-lite
  SUBHA_API_URL          default Gemini OpenAI-compatible URL
  SUBHA_ALLOWED_ORIGINS  extra CORS origins, comma-separated (optional)
  SUBHA_RATE_LIMIT       max API requests per minute per IP (default 40)
  SUBHA_USE_OLLAMA       set to 1 only on your own PC (local fallback)
  KURAL_PATH             optional path to thirukkural.json
  PORT / HOST            set by the hosting platform
Optional voice overrides (edge-tts / Azure voice names):
  SUBHA_VOICE_TAMIL, SUBHA_VOICE_ENGLISH            (girl voices)
  SUBHA_VOICE_TAMIL_MALE, SUBHA_VOICE_ENGLISH_MALE  (boy voices)
  SUBHA_VOICE_RATE (default +5%), SUBHA_VOICE_PITCH (default +0Hz)
  AZURE_SPEECH_KEY, AZURE_SPEECH_REGION             (official voice service)

Thirukkural is used silently: Subha lets its meaning shape her advice,
but never quotes the verse unless you explicitly ask for a Kural.
"""
import asyncio
import hmac
import json
import os
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
DATA_FILE = Path(os.environ.get("KURAL_PATH", BASE_DIR / "data" / "thirukkural.json"))

# Access control + CORS
ACCESS_TOKEN = os.environ.get("SUBHA_ACCESS_TOKEN", "").strip()
ALLOWED_ORIGINS = {o.strip() for o in (
    "https://localhost,capacitor://localhost,http://localhost,ionic://localhost,"
    + os.environ.get("SUBHA_ALLOWED_ORIGINS", "")).split(",") if o.strip()}
RATE_LIMIT = int(os.environ.get("SUBHA_RATE_LIMIT", "40"))

# Local Ollama fallback: OFF in production, ON only if you set SUBHA_USE_OLLAMA=1
USE_OLLAMA = os.environ.get("SUBHA_USE_OLLAMA", "0") == "1"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:4b")

# Cloud LLM: any OpenAI-compatible API. Keys rotate automatically.
CLOUD_KEYS = [k.strip() for k in (os.environ.get("SUBHA_API_KEYS") or os.environ.get("SUBHA_API_KEY") or "").split(",") if k.strip()]
CLOUD_URL = os.environ.get("SUBHA_API_URL", "https://generativelanguage.googleapis.com/v1beta/openai").rstrip("/")
CLOUD_MODEL = os.environ.get("SUBHA_API_MODEL", "gemini-3.5-flash-lite")
USE_CLOUD = bool(CLOUD_KEYS and CLOUD_MODEL)

# "always": every reply is built on a Kural idea.  "relevant": only when the topic matches.
WISDOM_MODE = os.environ.get("SUBHA_WISDOM", "relevant")


class NoBackend(Exception):
    pass


class KeyPool:
    """Round-robin over API keys; a key that hits its limit rests for a while."""
    def __init__(self, keys):
        self.keys, self.cool, self.i, self.lock = keys, {}, 0, threading.Lock()

    def order(self):
        with self.lock:
            n = len(self.keys)
            start, self.i = self.i, (self.i + 1) % max(n, 1)
            now = time.time()
            ordered = [self.keys[(start + j) % n] for j in range(n)]
            return [k for k in ordered if self.cool.get(k, 0) <= now]

    def rest(self, key, seconds):
        with self.lock:
            self.cool[key] = time.time() + seconds


POOL = KeyPool(CLOUD_KEYS)

# --------------------------------------------------------------------------
# Simple in-memory rate limiting (protects your Gemini quota)
# --------------------------------------------------------------------------
_RATE, _FAILS, _RATE_LOCK = {}, [], threading.Lock()


def rate_ok(ip):
    now = time.time()
    with _RATE_LOCK:
        hits = [t for t in _RATE.get(ip, []) if now - t < 60]
        hits.append(now)
        _RATE[ip] = hits
        if len(_RATE) > 5000:
            _RATE.clear()
        return len(hits) <= RATE_LIMIT


def note_auth_fail():
    """Return False when too many wrong passcodes were tried recently (anyone)."""
    now = time.time()
    with _RATE_LOCK:
        _FAILS[:] = [t for t in _FAILS if now - t < 60]
        _FAILS.append(now)
        return len(_FAILS) <= 20


# --------------------------------------------------------------------------
# Thirukkural data (loaded once, indexed once)
# --------------------------------------------------------------------------
def load_kurals():
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[Subha] WARNING: cannot load {DATA_FILE}: {exc}")
        return []
    if isinstance(data, dict):          # your file is {"kural": [...]}
        data = data.get("kural", [])
    return [k for k in data if isinstance(k, dict)]


KURALS = load_kurals()
BY_NUMBER = {int(k.get("Number", 0)): k for k in KURALS}
FIELDS = ("translation", "explanation", "mv", "sp", "mk")
INDEX = [
    (int(k.get("Number", 0)),
     " ".join(str(k.get(f, "") or "") for f in FIELDS).lower())
    for k in KURALS
]

# topic: (english words, tamil stems, tanglish words - used only on the user's message)
TOPICS = {
    "friendship": ("friend,companion", "நட்,நண்ப", "nanban,nanbar,natpu"),
    "anger": (r"anger,angry,mad\b,rage,wrath,furious", "கோப,சின", "kobam,kovam,sinam,kopam"),
    "education": ("study,studies,learn,education,knowledge,exam,college,school",
                  "கல்வி,கற்,அறிவு,படிப்", "kalvi,padipu,padikka,arivu"),
    "effort": ("effort,persever,determination,success,strive,hard work",
               "முயற்,ஊக்க,வெற்றி", "muyarchi,vetri,uzhaippu,ulaippu"),
    "truth": (r"truth,honest,lie\b,lies\b,lying", "உண்மை,பொய்,வாய்மை", "unmai,nermai,poi solla"),
    "kindness": ("kindness,compassion,mercy,charity,generous", "கருணை,அருள்,இரக்க,உதவி",
                 "karunai,udhavi,uthavi,irakkam"),
    "worry": (r"stress,worr,sad\b,sadness,fear,anxi,depress,sorrow,suffer,grief",
              "கவலை,துன்ப,துயர,பயம்", "kavalai,kastam,kashtam,bayam,tension,thunbam"),
    "love": ("love,romance,beloved,affection", "காதல்,அன்பு", "kadhal,kaadhal,anbu"),
    "patience": ("patien,forgive,tolerat,endur", "பொறு,மன்னி", "porumai,mannikka"),
    "laziness": ("lazy,laziness,procrastinat,idleness,sloth", "சோம்பல்,மடி", "sombal,aalasyam,lazyya"),
    "money": ("money,wealth,riches,poverty,income,salary", "செல்வம்,பொருள்,வறுமை,பணம்",
              "panam,selvam,kaasu"),
    "family": ("family,parent,mother,father,children,child", "பெற்றோர்,தாய்,தந்தை,மக்கள்,குடும்ப",
               "appa,amma,kudumbam"),
    "humility": ("humble,humility,pride,arrogan,ego\\b", "பணிவு,செருக்கு,அடக்க", "panivu,thimir"),
    "gratitude": ("gratitude,grateful,thankful,thank", "நன்றி", "nandri,thanks"),
    "courage": (r"courage,brave,failure,fail\b,give up", "துணிவு,தோல்வி,அஞ்ச",
                "bayapadatha,tholvi,thairiyam"),
    "jealousy": ("jealous,envy", "பொறாமை,அழுக்காறு", "poramai"),
    "health": ("health,food,eat,hungry,sick,disease,medicine,diet", "உணவு,நோய்,மருந்து,பசி",
               "saapad,saptiya,udambu,pasi,sappidu"),
    "time": ("time,moment,opportunity,wait,delay", "காலம்,காலத்", "neram,kaalam,samayam"),
    "planning": ("plan,decision,decide,future,goal,career,job,work", "திட்ட,ஆராய்ந்து,தொழில்,வேலை",
                 "planning,vela,velai,career,decision"),
    "gossip": ("gossip,backbit,rumor,rumour", "புறங்கூறல்,புறம்", "gossip,purani,purani pesu"),
    "selfcontrol": ("self-control,discipline,control,habit,addict,temptation", "அடக்க,ஒழுக்க,ஐம்புல",
                    "control,ozhukkam,pazhakkam,addiction"),
    "giving": ("give,giving,donate,generos,beg", "ஈகை,கொடை,இரவ", "kodu,udhavi,dhaanam"),
    "listening": ("listen,advice,counsel,mentor,teacher", "கேள்வி,செவி,அறிவுரை,ஆசிரிய",
                  "advice,kelvi,aasiriyar,upadesam"),
    "hospitality": ("guest,visitor,welcome,host", "விருந்து,விருந்தோம்பல்", "virundhu,guest"),
}
GENERAL_TOPICS = ["effort", "selfcontrol", "humility", "kindness", "truth", "patience", "time", "listening"]


def _pattern(en, ta, extra=""):
    latin = [w for w in (en + "," + extra).split(",") if w]
    tamil = [w for w in ta.split(",") if w]
    parts = []
    if latin:
        parts.append(r"(?<![a-z])(?:" + "|".join(latin) + ")")
    if tamil:
        parts.append("(?:" + "|".join(tamil) + ")")
    return re.compile("|".join(parts))


USER_PATTERNS = {t: _pattern(en, ta, tg) for t, (en, ta, tg) in TOPICS.items()}
KURAL_PATTERNS = {t: _pattern(en, ta) for t, (en, ta, _) in TOPICS.items()}
_top_cache = {}


def detect_topic(text):
    best, best_n = None, 0
    for topic, pat in USER_PATTERNS.items():
        n = len(pat.findall(text))
        if n > best_n:
            best, best_n = topic, n
    return best


def top_kurals(topic):
    if topic not in _top_cache:
        pat = KURAL_PATTERNS[topic]
        scored = sorted(((-len(pat.findall(txt)), num) for num, txt in INDEX))
        _top_cache[topic] = [num for s, num in scored[:10] if s < 0]
    return _top_cache[topic]


def meaning_of(k):
    eng = str(k.get("translation") or k.get("explanation") or "").strip()
    tam = str(k.get("mk") or k.get("mv") or "").strip()
    return eng[:300], tam[:250]


EXPLICIT_WORDS = ("thirukkural", "thirukural", "kural", "valluvar", "திருக்குறள்", "குறள்", "திருவள்ளுவர்")
SMALLTALK_WORDS = set("""hi hii hello helo hey hai vanakkam saptiya sapteena sapitiya epdi epdiye irukka irukkiya
how are you enna panra pandra panre ok okay seri hmm yes no bye thanks thank nandri good morning night evening
wassup sup da di pa nalla fine ya yaa aama illa
வணக்கம் ஹலோ ஹாய் ஹை சாப்டியா சாப்பிட்டியா எப்படி இருக்க இருக்கீங்களா என்ன பண்ற பண்றே சரி நன்றி ஆமா இல்ல
ஐ லவ் யூ மிஸ் தேங்க்ஸ் தேங்க் குட் மார்னிங் நைட் ஈவினிங் பை ஓகே ஓக்கே வாட் ஹவ் ஆர் யு ஹே டா டி பா நல்லா ஃபைன் லோல் ஹாஹா ஹஹா ரியலி ஆம் இல்லை எஸ் நோ சூப்பர் வாவ் ஹ்ம்ம் ம்ம்
love miss lol haha hehe wow super nice cool i u too so much really""".split())


def is_smalltalk(q):
    tokens = re.findall(r"[a-z]+|[\u0B80-\u0BFF]+", q.lower())
    if not tokens:
        return True
    if all(t in SMALLTALK_WORDS for t in tokens):
        return True
    return len(tokens) <= 2 and detect_topic(q.lower()) is None


def make_kural_context(user_text, prev_texts=()):
    """Return (context_text, explicit)."""
    if not KURALS:
        return "", False
    q = user_text.lower()

    if any(w in q for w in EXPLICIT_WORDS):
        m = re.search(r"\b(\d{1,4})\b", q)
        if m and int(m.group(1)) in BY_NUMBER:
            return format_kural(BY_NUMBER[int(m.group(1))]), True
        topic = detect_topic(q)
        if topic and top_kurals(topic):
            return format_kural(BY_NUMBER[top_kurals(topic)[0]]), True
        return ("The user asked about Thirukkural but gave no number or clear topic. "
                "Do not invent a verse. Ask which Kural number or topic he means."), True

    if is_smalltalk(q):                             # greetings: short plain reply, no idea
        return "", False

    topic = detect_topic(q)
    if not topic:                                   # use the recent conversation topic
        for earlier in reversed(list(prev_texts)[-3:]):
            topic = detect_topic(earlier.lower())
            if topic:
                break
    if not topic and WISDOM_MODE == "always":
        topic = random.choice(GENERAL_TOPICS)
    if not topic:
        return "", False

    nums = top_kurals(topic)
    if not nums:
        return "", False
    eng, tam = meaning_of(BY_NUMBER[random.choice(nums)])
    idea = (eng or tam)[:260]
    return ("Idea to build your reply on: " + idea + "\n"
            "Build your reply on this idea: first react to what he actually said, then bring the idea "
            "in naturally as your own gentle opinion, advice or small everyday example (one short sentence). "
            "Keep the whole reply under 20 words and at most 2 short sentences. Never quote it; never mention "
            "a verse, number, book, poet or ancient wisdom."), False


def format_kural(item):
    return (
        f"The user asked for a Kural, so you MAY share it now.\n"
        f"Kural {item.get('Number')}:\n{item.get('Line1', '')}\n{item.get('Line2', '')}\n"
        f"Meaning (Mu. Varadarajan): {item.get('mv', '')}\n"
        f"Meaning (Kalaignar): {item.get('mk', '')}\n"
        "Quote the couplet exactly, then explain it simply like a friend. Never change the number."
    )


# --------------------------------------------------------------------------
# Voices: girl and boy, Tamil and English (edge-tts, or Azure if configured)
# --------------------------------------------------------------------------
try:
    import edge_tts
    HAVE_EDGE_TTS = True
except ImportError:
    HAVE_EDGE_TTS = False

TAMIL_RE = re.compile(r"[\u0B80-\u0BFF]")

SAFE = {                                    # known-good voices, used if a chosen one fails
    ("female", "ta"): "ta-IN-PallaviNeural",
    ("female", "en"): "en-IN-NeerjaNeural",
    ("male", "ta"): "ta-IN-ValluvarNeural",
    ("male", "en"): "en-IN-PrabhatNeural",
}
VOICES = {
    ("female", "ta"): os.environ.get("SUBHA_VOICE_TAMIL", "").strip() or SAFE[("female", "ta")],
    ("female", "en"): os.environ.get("SUBHA_VOICE_ENGLISH", "").strip() or "en-IN-NeerjaExpressiveNeural",
    ("male", "ta"): os.environ.get("SUBHA_VOICE_TAMIL_MALE", "").strip() or SAFE[("male", "ta")],
    ("male", "en"): os.environ.get("SUBHA_VOICE_ENGLISH_MALE", "").strip() or SAFE[("male", "en")],
}
VOICE_RATE = os.environ.get("SUBHA_VOICE_RATE", "+5%").strip() or "+5%"
VOICE_PITCH = os.environ.get("SUBHA_VOICE_PITCH", "+0Hz").strip() or "+0Hz"
SPEEDS = {"slow": "-8%", "normal": VOICE_RATE, "fast": "+15%"}
AZURE_KEY = os.environ.get("AZURE_SPEECH_KEY", "").strip()
AZURE_REGION = os.environ.get("AZURE_SPEECH_REGION", "").strip()


def pick_voice(text, gender):
    """Return (voice, safe_voice) for this text: Tamil script -> Tamil voice, else English voice."""
    key = (gender if gender in ("female", "male") else "female", "ta" if TAMIL_RE.search(text) else "en")
    return VOICES[key], SAFE[key]


def _xml(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def synthesize_azure(text, voice, rate):
    ssml = (f"<speak version='1.0' xml:lang='{voice[:5]}'><voice name='{voice}'>"
            f"<prosody rate='{rate}' pitch='{VOICE_PITCH}'>{_xml(text)}</prosody></voice></speak>")
    req = Request(
        f"https://{AZURE_REGION}.tts.speech.microsoft.com/cognitiveservices/v1",
        data=ssml.encode("utf-8"),
        headers={"Ocp-Apim-Subscription-Key": AZURE_KEY,
                 "Content-Type": "application/ssml+xml",
                 "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
                 "User-Agent": "SubhaAI"},
        method="POST")
    with urlopen(req, timeout=30) as r:
        return r.read()


def synthesize_edge(text, voice, safe, rate):
    def run_with(v):
        async def run():
            audio = bytearray()
            async for chunk in edge_tts.Communicate(text, v, rate=rate, pitch=VOICE_PITCH).stream():
                if chunk["type"] == "audio":
                    audio += chunk["data"]
            if not audio:
                raise RuntimeError(f"no audio received for voice {v}")
            return bytes(audio)
        return asyncio.run(run())

    try:
        return run_with(voice)
    except Exception as exc:
        if voice == safe:
            raise
        print(f"[Subha] voice {voice} failed ({exc}); using {safe}")
        return run_with(safe)


def synthesize(text, gender="female", speed="normal"):
    voice, safe = pick_voice(text, gender)
    rate = SPEEDS.get(speed, VOICE_RATE)
    if AZURE_KEY and AZURE_REGION:                  # official service first
        try:
            return synthesize_azure(text, voice, rate)
        except Exception as exc:
            print(f"[Subha] azure tts failed: {exc}")
            if not HAVE_EDGE_TTS:
                raise
    return synthesize_edge(text, voice, safe, rate)


# --------------------------------------------------------------------------
# Persona (girl = Subha, boy = Subhash) + language rules + examples per language
# --------------------------------------------------------------------------
NAMES = {"female": ("Subha", "சுபா", "girl"), "male": ("Subhash", "சுபாஷ்", "boy")}

PERSONA = """You are {name} ({name_ta}), the user's close {gender} friend chatting on a voice call. You are a {gender}, so speak naturally as a {gender} would. Talk like a real friend, not an assistant.
- Follow the LANGUAGE RULE at the end exactly. It decides your language, even if the user mixes languages.
- Very short: 1-2 sentences, max 25 words. Never lecture. Casual, playful, warm. A greeting gets a greeting back.
- Do not assume the user is sad or stressed. Be sympathetic only if they say something is wrong.
- Use everyday spoken style, never bookish or formal.
- No lists, markdown or emojis. Your words are read aloud.
- You are an AI friend. Do not claim human experiences.
- Match the length of the user's message: a short or casual message gets a very short reply (under 12 words). Never give moral lessons or life advice unless the user asks for advice.
- You do not know the user's name. Never assume one. In your first reply, greet and casually ask their name once. After they tell you, use it naturally now and then.
- Never mention Thirukkural, Valluvar or verses unless the user explicitly asks for a Kural.
- If a [private note] is attached, build your reply on its idea in your own casual words, as your own opinion. Never quote or reveal the note.
- If there is no note (greetings, small talk), just chat naturally and keep it very short, under 12 words."""

LANGUAGE_INSTRUCTIONS = {
    "auto": "LANGUAGE RULE: Reply in the same language style the user just used: Tamil script if they wrote Tamil script, "
            "English if English, Tanglish (Tamil in English letters, mixed with English) if Tanglish.",
    "English": "LANGUAGE RULE: Reply ONLY in simple, natural spoken English, even if the user uses Tamil words. "
               "Never use Tamil script and never write Tamil words in English letters.",
    "Tamil": "LANGUAGE RULE: Reply ONLY in natural spoken Tamil written in Tamil script (colloquial, not bookish). "
             "No English letters except unavoidable technical words.",
    "Tanglish": "LANGUAGE RULE: Reply ONLY in natural spoken Tanglish: Tamil written in English letters mixed with "
                "easy English words, the way friends text. Never use Tamil script.",
}

_NOTE = "\n\n[private note: Idea to build your reply on: {}]"
FEWSHOT_BASE = {
    "English": [
        ("hi", "Hey! I'm {name}. What's your name?"),
        ("how are you", "Doing great! How's your day going?"),
        ("I'm bored" + _NOTE.format("Free time is a chance to learn something new."),
         "Same, honestly. Why not learn one tiny new thing today? I can throw out an idea if you want."),
    ],
    "Tamil": [
        ("ஹாய்", "ஹாய்! நான் {name_ta}. உன் பேர் என்ன?"),
        ("என்ன பண்ற?", "சும்மா தான் இருக்கேன், நீ சொல்லு. இன்னைக்கு எப்படி போச்சு?"),
        ("எனக்கு படிக்கவே பிடிக்கல" + _NOTE.format("Learning is a treasure nobody can take from you."),
         "புரியுது, சில நாள் அப்படித்தான் இருக்கும். ஆனா படிப்பு யாரும் பறிக்க முடியாத சொத்து, பத்து நிமிஷம் மட்டும் ட்ரை பண்ணு."),
    ],
    "Tanglish": [
        ("hi", "Hey! Naan {name}. Un per enna?"),
        ("saptiya", "Aama, saptten. Nee saptiya?"),
        ("loosu", "Hahaha, nee thaan loosu! Mood nalla irukka?"),
        ("bore adikkuthu" + _NOTE.format("Free time is a chance to learn something new."),
         "Same pinch! Chinna ah onnu puthusa kathukalaama? Naan oru idea sollattuma?"),
    ],
}
FEWSHOT_BASE["auto"] = FEWSHOT_BASE["Tanglish"]


def build_persona(gender):
    name, name_ta, word = NAMES[gender]
    return PERSONA.replace("{name_ta}", name_ta).replace("{name}", name).replace("{gender}", word)


def few_shot(language, gender):
    name, name_ta, _ = NAMES[gender]
    out = []
    for user, bot in FEWSHOT_BASE.get(language, FEWSHOT_BASE["Tanglish"]):
        out.append({"role": "user", "content": user})
        out.append({"role": "assistant", "content": bot.replace("{name_ta}", name_ta).replace("{name}", name)})
    return out


# --------------------------------------------------------------------------
# Web page
# --------------------------------------------------------------------------
HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#10131b">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Subha AI">
<title>Subha AI</title>
<style>
:root{color-scheme:dark;--bg:#10131b;--panel:#181d29;--panel2:#202738;--text:#f3f5fb;--muted:#a3adc1;--accent:#8b7cff;--border:#30394d;--live:#ff6b8a}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--text);font-family:"Segoe UI","Nirmala UI",Arial,sans-serif;display:flex;justify-content:center}
.app{width:100%;max-width:920px;height:100vh;height:100dvh;display:flex;flex-direction:column;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}
header{padding:14px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px}
.logo{width:42px;height:42px;border-radius:14px;background:linear-gradient(135deg,#a89cff,#6653db);display:grid;place-items:center;font-size:23px;font-weight:700;color:#fff}
.brand{flex:1;min-width:0}
.brand h1{margin:0;font-size:19px}
.brand p{margin:3px 0 0;color:var(--muted);font-size:12px;overflow-wrap:anywhere}
select,button,textarea{font:inherit}
.chip{border:1px solid var(--border);background:var(--panel2);color:var(--text);border-radius:20px;padding:8px 13px;cursor:pointer;font-size:13px;white-space:nowrap}
#messages{flex:1;overflow-y:auto;padding:22px 16px;display:flex;flex-direction:column;gap:16px}
.welcome{margin:auto;max-width:440px;text-align:center;padding:24px 10px}
.welcome .big{font-size:46px}
.welcome h2{margin:10px 0 8px;font-size:25px}
.welcome p{color:var(--muted);line-height:1.6}
.message{display:flex;gap:10px;max-width:90%;align-items:flex-start}
.message.user{align-self:flex-end;flex-direction:row-reverse}
.avatar{flex:0 0 32px;height:32px;border-radius:11px;display:grid;place-items:center;font-size:14px;background:var(--panel2)}
.message.user .avatar{background:#5547bd}
.bubble{padding:11px 15px;border-radius:16px;line-height:1.65;white-space:pre-wrap;overflow-wrap:anywhere;background:var(--panel)}
.message.user .bubble{background:#39316c}
.bubble.error{border:1px solid #a54c59}
.composer-wrap{padding:10px 14px 14px;border-top:1px solid var(--border)}
.composer{display:flex;gap:10px;align-items:flex-end;padding:9px;border:1px solid var(--border);border-radius:18px;background:var(--panel)}
textarea{flex:1;resize:none;max-height:140px;min-height:42px;border:0;outline:0;color:var(--text);background:transparent;padding:10px 5px;line-height:1.5;font-size:16px}
#mic{flex:0 0 46px;height:46px;border-radius:50%;border:0;background:var(--panel2);color:var(--text);font-size:20px;cursor:pointer}
#mic.on{background:#4b3fb5}
#mic.live{background:var(--live);animation:pulse 1.3s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(255,107,138,.55)}100%{box-shadow:0 0 0 16px rgba(255,107,138,0)}}
@media (prefers-reduced-motion:reduce){#mic.live{animation:none}}
#send{border:0;border-radius:12px;padding:12px 16px;background:var(--accent);color:#fff;cursor:pointer;font-weight:600}
#send:disabled{opacity:.5}
button:focus-visible,select:focus-visible,textarea:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.tools{display:flex;justify-content:space-between;align-items:center;margin-top:9px;gap:10px}
.smallbtn{border:1px solid var(--border);border-radius:9px;color:var(--muted);background:var(--panel);padding:7px 10px;cursor:pointer}
.hint{color:var(--muted);font-size:11px}
dialog{background:var(--panel);color:var(--text);border:1px solid var(--border);border-radius:18px;padding:20px;width:calc(100% - 32px);max-width:420px}
dialog::backdrop{background:rgba(0,0,0,.65)}
dialog h2{margin:0 0 14px;font-size:20px}
fieldset{border:1px solid var(--border);border-radius:12px;margin:0 0 12px;padding:6px 12px 8px}
legend{color:var(--muted);font-size:12px;padding:0 6px}
label.opt{display:inline-flex;align-items:center;gap:7px;margin-right:16px;padding:7px 0;font-size:15px}
input[type=radio],input[type=checkbox]{accent-color:var(--accent);width:18px;height:18px}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}
.row button{border:1px solid var(--border);background:var(--panel2);color:var(--text);border-radius:10px;padding:10px 14px;cursor:pointer}
.row button.primary{background:var(--accent);border-color:var(--accent);color:#fff;margin-left:auto}
.tip{color:var(--muted);font-size:12px;line-height:1.5;margin:0 0 12px}
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="logo">சு</div>
    <div class="brand"><h1 id="title">Subha AI</h1><p id="status">Tap the mic and just talk</p></div>
    <button id="gear" class="chip" type="button" aria-label="Open settings">⚙ <span id="chipText"></span></button>
  </header>

  <main id="messages" aria-live="polite">
    <div class="welcome" id="welcome">
      <div class="big">🌷</div>
      <h2>Hi, I'm your Subha!</h2>
      <p>Tap the mic and talk to me like a friend. Open ⚙ to choose your language and a girl or boy voice.</p>
    </div>
  </main>

  <div class="composer-wrap">
    <form id="chatForm" class="composer">
      <button id="mic" type="button" aria-label="Start or stop voice chat" title="Voice chat">🎙</button>
      <textarea id="input" rows="1" placeholder="Message..." aria-label="Message"></textarea>
      <button id="send" type="submit">Send</button>
    </form>
    <div class="tools">
      <button id="clear" class="smallbtn" type="button">New chat</button>
      <span class="hint">Speak a full sentence, then pause</span>
    </div>
  </div>
</div>

<dialog id="settings" aria-label="Settings">
  <h2>Settings</h2>
  <fieldset>
    <legend>Language (what you speak and what Subha replies in)</legend>
    <label class="opt"><input type="radio" name="lang" value="english"> English</label>
    <label class="opt"><input type="radio" name="lang" value="tamil"> தமிழ்</label>
    <label class="opt"><input type="radio" name="lang" value="tanglish"> Tanglish</label>
  </fieldset>
  <fieldset>
    <legend>Voice</legend>
    <label class="opt"><input type="radio" name="voice" value="female"> Girl</label>
    <label class="opt"><input type="radio" name="voice" value="male"> Boy</label>
  </fieldset>
  <fieldset>
    <legend>Speaking speed</legend>
    <label class="opt"><input type="radio" name="speed" value="slow"> Slow</label>
    <label class="opt"><input type="radio" name="speed" value="normal"> Normal</label>
    <label class="opt"><input type="radio" name="speed" value="fast"> Fast</label>
  </fieldset>
  <label class="opt"><input type="checkbox" id="speakOn"> Speak replies aloud</label>
  <p class="tip">Tanglish: you speak Tamil with English words (the mic listens in Tamil). Replies are written in English letters.</p>
  <div class="row">
    <button id="testVoice" type="button">▶ Test voice</button>
    <button id="lock" type="button">Change passcode</button>
    <button id="closeSettings" class="primary" type="button">Done</button>
  </div>
</dialog>

<script>
const $ = id => document.getElementById(id);
const messagesEl = $('messages'), inputEl = $('input'), sendBtn = $('send'), micBtn = $('mic');
const statusEl = $('status'), dlg = $('settings');
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const MOBILE = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);

/* ---------- settings (saved on this device) ---------- */
const LANG_LABEL = {english: 'English', tamil: 'தமிழ்', tanglish: 'Tanglish'};
const MIC_LANG = {english: 'en-IN', tamil: 'ta-IN', tanglish: 'ta-IN'};
const REPLY_LANG = {english: 'English', tamil: 'Tamil', tanglish: 'Tanglish'};
const NAME = {female: 'Subha', male: 'Subhash'};
const S = {lang: 'tanglish', voice: 'female', speed: 'normal', speak: true};
try { Object.assign(S, JSON.parse(localStorage.getItem('subha_settings') || '{}')); } catch (e) {}
if (!LANG_LABEL[S.lang]) S.lang = 'tanglish';
if (!NAME[S.voice]) S.voice = 'female';
if (!['slow', 'normal', 'fast'].includes(S.speed)) S.speed = 'normal';
S.speak = S.speak !== false;

function saveSettings() { try { localStorage.setItem('subha_settings', JSON.stringify(S)); } catch (e) {} }
function renderSettings() {
  for (const k of ['lang', 'voice', 'speed']) {
    const r = document.querySelector('input[name=' + k + '][value=' + S[k] + ']');
    if (r) r.checked = true;
  }
  $('speakOn').checked = S.speak;
  $('chipText').textContent = (S.voice === 'male' ? 'Boy' : 'Girl') + ' · ' + LANG_LABEL[S.lang];
  $('title').textContent = NAME[S.voice] + ' AI';
  document.title = NAME[S.voice] + ' AI';
}
document.querySelectorAll('#settings input[type=radio]').forEach(r => r.addEventListener('change', () => {
  S[r.name] = r.value; saveSettings(); renderSettings();
  if (r.name === 'lang' && listening && rec) { try { rec.abort(); } catch (e) {} }   // restarts in new language
}));
$('speakOn').addEventListener('change', e => { S.speak = e.target.checked; saveSettings(); });
$('gear').addEventListener('click', () => { dlg.showModal ? dlg.showModal() : dlg.setAttribute('open', ''); });
$('closeSettings').addEventListener('click', () => { dlg.close ? dlg.close() : dlg.removeAttribute('open'); });
renderSettings();

/* ---------- server URL + passcode ----------
   In the browser the page and the API share one origin, so BASE is ''.
   In the Capacitor app, set window.SUBHA_BASE = 'https://your-app.onrender.com' before this script. */
const BASE = (window.SUBHA_BASE || '').replace(/\/$/, '');
let TOKEN = '';
try { TOKEN = localStorage.getItem('subha_token') || ''; } catch (e) {}
function askToken() {
  const t = (window.prompt('Enter Subha passcode:') || '').trim();
  if (t) { TOKEN = t; try { localStorage.setItem('subha_token', t); } catch (e) {} }
  return t;
}
function clearToken() { TOKEN = ''; try { localStorage.removeItem('subha_token'); } catch (e) {} }
async function apiFetch(path, opts = {}) {
  if (!TOKEN && !askToken()) throw new Error('Passcode needed.');
  opts.headers = Object.assign({}, opts.headers || {}, {'X-Subha-Token': TOKEN});
  const res = await fetch(BASE + path, opts);
  if (res.status === 401) clearToken();
  return res;
}
$('lock').addEventListener('click', () => { clearToken(); askToken(); });

let history = [], busy = false, controller = null;
let voiceMode = false, listening = false, rec = null;
let ttsQueue = [], speaking = false, ttsGen = 0, curAudio = null;

const scrollBottom = () => { messagesEl.scrollTop = messagesEl.scrollHeight; };
const setStatus = t => { statusEl.textContent = t; };

function addMessage(role, text, isError = false) {
  const w = $('welcome'); if (w) w.remove();
  const row = document.createElement('div');
  row.className = 'message ' + (role === 'user' ? 'user' : 'assistant');
  const av = document.createElement('div');
  av.className = 'avatar'; av.textContent = role === 'user' ? 'நீ' : 'சு';
  const bubble = document.createElement('div');
  bubble.className = 'bubble' + (isError ? ' error' : '');
  bubble.textContent = text;
  row.append(av, bubble);
  messagesEl.appendChild(row);
  scrollBottom();
  return bubble;
}

/* ---------- speech output ---------- */
let voices = [];
const loadVoices = () => { voices = speechSynthesis.getVoices(); };
if ('speechSynthesis' in window) { loadVoices(); speechSynthesis.onvoiceschanged = loadVoices; }

function pickBrowserVoice(text) {
  const tamil = /[\u0B80-\u0BFF]/.test(text);
  let list = voices.filter(v => v.lang.toLowerCase().startsWith(tamil ? 'ta' : 'en'));
  if (!tamil) {
    const indian = list.filter(v => v.lang.toLowerCase() === 'en-in');
    if (indian.length) list = indian;
  }
  const re = S.voice === 'male' ? /prabhat|valluvar|ravi|male|david|mark|guy|hemant/i
                                : /pallavi|neerja|female|heera|veena|natural|zira|aria/i;
  return list.find(v => re.test(v.name)) || list[0] || null;
}

function cleanForSpeech(s) {
  return s.replace(/[*_`#>]/g, '').replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/gu, '').trim();
}

function browserSpeak(text, done) {
  let finished = false;
  const end = () => { if (!finished) { finished = true; done(); } };
  if (!('speechSynthesis' in window)) { end(); return; }
  const u = new SpeechSynthesisUtterance(text);
  const v = pickBrowserVoice(text);
  if (v) { u.voice = v; u.lang = v.lang; } else { u.lang = /[\u0B80-\u0BFF]/.test(text) ? 'ta-IN' : 'en-IN'; }
  u.rate = S.speed === 'slow' ? 0.9 : S.speed === 'fast' ? 1.2 : 1.05;
  u.pitch = S.voice === 'male' ? 0.85 : 1.1;
  u.onend = u.onerror = end;
  speechSynthesis.speak(u);
}

// Each reply is fetched as ONE audio clip (only very long replies are split).
function fetchAudio(text) {
  return apiFetch('/api/tts', {method: 'POST', headers: {'Content-Type': 'application/json'},
                               body: JSON.stringify({text, voice: S.voice, speed: S.speed})})
    .then(r => r.ok ? r.blob() : null).catch(() => null);
}

function enqueueSpeech(raw) {
  const s = cleanForSpeech(raw);
  if (!/[\p{L}\p{N}]/u.test(s)) return;
  ttsQueue.push({text: s, audio: fetchAudio(s)});
  if (!speaking) nextSpeech();
}

async function nextSpeech() {
  const item = ttsQueue.shift();
  if (!item) { speaking = false; maybeResume(); return; }
  speaking = true;
  setStatus('Speaking...');
  const gen = ttsGen;
  const advance = () => { if (gen === ttsGen) nextSpeech(); };
  const blob = await item.audio;
  if (gen !== ttsGen) return;                       // interrupted while loading
  if (blob && blob.size > 0) {
    const url = URL.createObjectURL(blob);
    const a = new Audio(url);
    curAudio = a;
    let finished = false;
    const end = () => { if (finished) return; finished = true; URL.revokeObjectURL(url); advance(); };
    a.onended = end; a.onerror = end;
    try { await a.play(); } catch (e) { finished = true; browserSpeak(item.text, advance); }
  } else {
    browserSpeak(item.text, advance);               // fallback voice
  }
}

// One audio for the whole reply = no gaps. Only very long replies are split.
function speakAll(text) {
  let rest = text.trim();
  while (rest.length > 260) {
    let cut = Math.max(rest.lastIndexOf('. ', 260), rest.lastIndexOf('? ', 260), rest.lastIndexOf('! ', 260));
    if (cut < 80) cut = rest.lastIndexOf(' ', 260);
    if (cut < 1) cut = 260;
    enqueueSpeech(rest.slice(0, cut + 1));
    rest = rest.slice(cut + 1).trim();
  }
  if (rest) enqueueSpeech(rest);
}

function stopSpeech() {
  ttsGen++; ttsQueue = [];
  if (curAudio) { curAudio.pause(); curAudio = null; }
  if ('speechSynthesis' in window) speechSynthesis.cancel();
}

$('testVoice').addEventListener('click', () => {
  stopSpeech(); speaking = false;
  speakAll({english: "Hi! This is how I sound. Nice to talk to you.",
            tamil: "வணக்கம்! என் குரல் இப்படித்தான் இருக்கும். உன்கூட பேசுறது சந்தோஷம்.",
            tanglish: "Hi! En kural ippadi thaan irukkum. Unnoda pesurathu santhosham."}[S.lang]);
});

/* ---------- speech input ---------- */
const MIC_MSG = {
  'no-speech': 'I did not hear anything. Speak when the mic turns pink.',
  'audio-capture': 'No microphone found. Check your mic settings.',
  'network': 'Speech service not reachable. Check internet, or try Chrome.',
  'not-allowed': 'Microphone is blocked. Allow it in the browser/app settings.',
  'service-not-allowed': 'Speech recognition is blocked in this browser. Try Chrome.',
  'language-not-supported': 'This language is not supported for voice input. Change the language in settings.'
};
const MIC_FATAL = ['audio-capture', 'network', 'not-allowed', 'service-not-allowed', 'language-not-supported'];

function startListening() {
  if (!SR) { setStatus('Voice input needs Chrome or Edge. You can still type.'); voiceMode = false; micBtn.classList.remove('on'); return; }
  if (listening || busy || speaking) return;
  listening = true;
  let latest = '', fatal = false, heard = false, timer = null;
  const r = rec = new SR();
  r.lang = MIC_LANG[S.lang];
  r.interimResults = true; r.maxAlternatives = 1;
  r.continuous = !MOBILE;                 // desktop: wait for a real pause; phones: browser decides
  // Stop after ~1.4 s of silence once you started speaking (so sentences are not cut short).
  const arm = () => { clearTimeout(timer); timer = setTimeout(() => { try { r.stop(); } catch (e) {} }, 1400); };
  r.onstart = () => { micBtn.classList.add('live'); setStatus('Listening... (' + LANG_LABEL[S.lang] + ')'); };
  r.onspeechstart = () => { heard = true; setStatus('Hearing you...'); };
  r.onresult = e => {
    let fin = '', interim = '';
    for (let i = 0; i < e.results.length; i++) {
      const x = e.results[i];
      if (x.isFinal) fin += x[0].transcript + ' '; else interim += x[0].transcript;
    }
    latest = (fin + interim).trim();
    inputEl.value = latest;
    if (!MOBILE) arm();
  };
  r.onerror = e => {
    if (e.error !== 'aborted') setStatus(MIC_MSG[e.error] || ('Mic error: ' + e.error));
    if (MIC_FATAL.includes(e.error)) { fatal = true; voiceMode = false; micBtn.classList.remove('on'); }
  };
  r.onend = () => {
    clearTimeout(timer);
    listening = false; micBtn.classList.remove('live');
    const t = latest.trim();
    if (t && voiceMode) { inputEl.value = ''; sendMessage(t); }
    else if (voiceMode && !fatal) {
      if (heard) setStatus('I heard a sound but could not understand. Speak a little longer.');
      setTimeout(startListening, 700);
    }
    else if (!fatal) setStatus('Tap the mic and just talk');
  };
  try { r.start(); } catch (e) { listening = false; }
}

function maybeResume() {
  if (voiceMode && !busy && !speaking && ttsQueue.length === 0) startListening();
  else if (!voiceMode && !busy && !speaking) setStatus('Ready');
}

micBtn.addEventListener('click', () => {
  if (!voiceMode) {
    voiceMode = true; micBtn.classList.add('on');
    stopSpeech(); startListening();
  } else if (busy || speaking) {          // tap to interrupt and talk
    if (controller) controller.abort();
    stopSpeech(); speaking = false;
    if (!busy) startListening();
  } else {                                // turn voice chat off
    voiceMode = false; micBtn.classList.remove('on', 'live');
    if (rec) try { rec.abort(); } catch (e) {}
    setStatus('Tap the mic and just talk');
  }
});

/* ---------- chat ---------- */
async function sendMessage(textArg) {
  const text = (textArg ?? inputEl.value).trim();
  if (!text || busy) return;
  stopSpeech(); speaking = false;
  if (listening && rec) try { rec.abort(); } catch (e) {}

  addMessage('user', text);
  inputEl.value = ''; inputEl.style.height = '42px';
  history.push({role: 'user', content: text});

  busy = true; sendBtn.disabled = true; setStatus('Thinking...');
  const bubble = addMessage('assistant', '...');
  controller = new AbortController();
  const speak = S.speak || voiceMode;
  let answer = '';

  try {
    const res = await apiFetch('/api/chat', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      signal: controller.signal,
      body: JSON.stringify({messages: history, language: REPLY_LANG[S.lang], voice: S.voice})
    });
    if (res.status === 401) throw new Error('Wrong passcode. Send again and enter it once more.');
    if (!res.ok) { const d = await res.json(); throw new Error(d.error || 'The request failed.'); }

    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = '';
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      let i;
      while ((i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
        if (!line) continue;
        const msg = JSON.parse(line);
        if (msg.error) throw new Error(msg.error);
        if (msg.t) { answer += msg.t; bubble.textContent = answer; scrollBottom(); }
      }
    }
    if (!answer.trim()) throw new Error('Empty reply. Try again.');
    history.push({role: 'assistant', content: answer});
    if (speak) speakAll(answer);
  } catch (err) {
    if (err.name === 'AbortError') {
      if (answer.trim()) history.push({role: 'assistant', content: answer}); else { history.pop(); bubble.closest('.message').remove(); }
    } else {
      history.pop();
      bubble.textContent = 'I could not reach ' + NAME[S.voice] + ': ' + err.message;
      bubble.classList.add('error');
      setStatus('Connection problem. Check internet and try again.');
    }
  } finally {
    busy = false; controller = null; sendBtn.disabled = false;
    if (!speaking && ttsQueue.length === 0) maybeResume();
  }
}

$('chatForm').addEventListener('submit', e => { e.preventDefault(); sendMessage(); });
inputEl.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
inputEl.addEventListener('input', () => {
  inputEl.style.height = '42px';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 140) + 'px';
});
$('clear').addEventListener('click', () => {
  if (controller) controller.abort();
  stopSpeech(); speaking = false; history = [];
  messagesEl.innerHTML = '<div class="welcome" id="welcome"><div class="big">🌷</div><h2>Fresh start!</h2><p>What would you like to talk about?</p></div>';
  inputEl.value = ''; inputEl.focus();
});
</script>
</body>
</html>'''


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
class SubhaHandler(BaseHTTPRequestHandler):
    def client_ip(self):
        fwd = self.headers.get("X-Forwarded-For", "")
        return (fwd.split(",")[0].strip() if fwd else self.client_address[0]) or "?"

    def cors(self):
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Subha-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Max-Age", "600")

    def send_bytes(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message):
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_bytes(status, body, "application/json; charset=utf-8")

    def gate(self):
        """Rate limit + passcode check for every /api/ call. Returns True if allowed."""
        if not rate_ok(self.client_ip()):
            self.send_error_json(429, "Too many requests. Wait a minute.")
            return False
        sent = self.headers.get("X-Subha-Token", "")
        if not ACCESS_TOKEN or not hmac.compare_digest(sent.encode("utf-8"), ACCESS_TOKEN.encode("utf-8")):
            if not note_auth_fail():
                self.send_error_json(429, "Too many wrong attempts. Wait a minute.")
            else:
                self.send_error_json(401, "unauthorized")
            return False
        return True

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self.send_bytes(200, b"ok", "text/plain; charset=utf-8")
        elif path in ("/", "/index.html"):
            self.send_bytes(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self.send_bytes(404, b"Not found", "text/plain; charset=utf-8")

    def do_HEAD(self):
        # Uptime monitors often use HEAD
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if not self.path.startswith("/api/"):
            self.send_error_json(404, "Not found")
            return
        if not self.gate():
            return
        if self.path == "/api/tts":
            self.handle_tts()
            return
        if self.path != "/api/chat":
            self.send_error_json(404, "Not found")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))

            language = payload.get("language", "auto")
            if language not in LANGUAGE_INSTRUCTIONS:
                language = "auto"
            gender = payload.get("voice") if payload.get("voice") in NAMES else "female"

            raw = payload.get("messages", [])
            if not isinstance(raw, list) or not raw:
                raise ValueError("Please enter a message.")

            clean = []
            for item in raw[-12:]:      # short history = faster replies
                if (isinstance(item, dict) and item.get("role") in ("user", "assistant")
                        and isinstance(item.get("content"), str)):
                    clean.append({"role": item["role"], "content": item["content"][:4000]})
            if not clean or clean[-1]["role"] != "user":
                raise ValueError("The latest message must be from you.")

            system_text = build_persona(gender) + "\n\n" + LANGUAGE_INSTRUCTIONS[language]
            prev_user = [m["content"] for m in clean[:-1] if m["role"] == "user"]
            context, _explicit = make_kural_context(clean[-1]["content"], prev_user)
            if context:
                clean[-1]["content"] += "\n\n[private note: " + context + "]"

            messages = [{"role": "system", "content": system_text}] + few_shot(language, gender) + clean
            resp, cloud_used = None, False

            if USE_CLOUD:
                body = {"model": CLOUD_MODEL, "messages": messages, "stream": True,
                        "temperature": 0.8, "max_tokens": 90}
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                for key in POOL.order():
                    req = Request(CLOUD_URL + "/chat/completions", data=data, method="POST",
                                  headers={"Content-Type": "application/json",
                                           "Authorization": "Bearer " + key})
                    try:
                        resp, cloud_used = urlopen(req, timeout=60), True
                        break
                    except HTTPError as exc:
                        if exc.code in (401, 403):
                            POOL.rest(key, 6 * 3600)      # bad/blocked key
                        elif exc.code == 429 or exc.code == 402 or exc.code >= 500:
                            POOL.rest(key, 120)           # limit reached: rest this key
                        else:
                            print(f"[Subha] cloud API HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}")
                            raise NoBackend()
                        print(f"[Subha] key {key[:6]}... got HTTP {exc.code}, trying next key")
                    except URLError:
                        break                             # no internet / API down

            if resp is None and USE_OLLAMA:               # local PC only
                body = {"model": MODEL, "messages": messages, "stream": True, "keep_alive": "60m",
                        "options": {"temperature": 0.8, "top_p": 0.9, "repeat_penalty": 1.1,
                                    "num_ctx": 2048, "num_predict": 80}}
                req = Request(OLLAMA_URL,
                              data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                              headers={"Content-Type": "application/json"}, method="POST")
                resp = urlopen(req, timeout=120)

            if resp is None:
                raise NoBackend()
        except (NoBackend, HTTPError, URLError):
            self.send_error_json(503, "Subha is busy right now. Try again in a minute.")
            return
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_error_json(400, str(exc))
            return
        except Exception as exc:
            print(f"[Subha] chat error: {exc}")
            self.send_error_json(500, "Subha hit an error. Try again.")
            return

        # Stream tokens to the client as newline-delimited JSON.
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.cors()
            self.end_headers()
            with resp:
                for line in resp:
                    line = line.strip()
                    if not line:
                        continue
                    if cloud_used:
                        if not line.startswith(b"data:"):
                            continue
                        data = line[5:].strip()
                        if data == b"[DONE]":
                            break
                        choices = json.loads(data.decode("utf-8")).get("choices") or [{}]
                        text = (choices[0].get("delta") or {}).get("content") or ""
                        done = False
                    else:
                        chunk = json.loads(line.decode("utf-8"))
                        text = chunk.get("message", {}).get("content", "")
                        done = chunk.get("done")
                    if text:
                        self.wfile.write(json.dumps({"t": text}, ensure_ascii=False).encode("utf-8") + b"\n")
                        self.wfile.flush()
                    if done:
                        break
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass    # client interrupted (user tapped the mic) - that's fine
        except Exception as exc:
            print(f"[Subha] stream error: {exc}")
            try:
                self.wfile.write(json.dumps({"error": "Connection dropped. Try again."}).encode("utf-8") + b"\n")
            except Exception:
                pass

    def handle_tts(self):
        if not (HAVE_EDGE_TTS or (AZURE_KEY and AZURE_REGION)):
            self.send_error_json(503, "Voice service not available")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 10_000:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            text = str(payload.get("text", ""))[:600].strip()
            if not text:
                raise ValueError("No text")
            gender = payload.get("voice") if payload.get("voice") in NAMES else "female"
            speed = payload.get("speed") if payload.get("speed") in SPEEDS else "normal"
            audio = synthesize(text, gender, speed)
            if not audio:
                raise RuntimeError("No audio returned")
            self.send_bytes(200, audio, "audio/mpeg")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_error_json(400, str(exc))
        except Exception as exc:
            print(f"[Subha] tts error: {exc}")
            self.send_error_json(500, "Voice error. Try again.")

    def log_message(self, fmt, *args):
        print("[Subha]", fmt % args)


def warm_up():
    """Local Ollama only: load the model into memory so the first reply is fast."""
    try:
        body = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}],
                "stream": False, "keep_alive": "60m", "options": {"num_predict": 1}}
        urlopen(Request(OLLAMA_URL, data=json.dumps(body).encode("utf-8"),
                        headers={"Content-Type": "application/json"}, method="POST"), timeout=300).read()
        print("[Subha] Local model warmed up.")
    except Exception as exc:
        print(f"[Subha] Warm-up skipped: {exc}")


if __name__ == "__main__":
    print("=" * 52)
    print(f"Subha AI is running ({len(KURALS)} Kurals loaded) on port {PORT}.")
    print("Voices: " + ("edge-tts girl + boy" if HAVE_EDGE_TTS else "browser voice only (pip install edge-tts)")
          + (" + Azure" if AZURE_KEY and AZURE_REGION else ""))
    print(f"Brain: " + (f"cloud ({len(CLOUD_KEYS)} keys) {CLOUD_MODEL}" if USE_CLOUD else "no cloud keys")
          + (f" + local {MODEL}" if USE_OLLAMA else ""))
    if not ACCESS_TOKEN:
        print("WARNING: SUBHA_ACCESS_TOKEN is not set, so every API call will be refused.")
    if not USE_CLOUD and not USE_OLLAMA:
        print("WARNING: no SUBHA_API_KEYS and SUBHA_USE_OLLAMA is off, so Subha cannot answer.")
    print("=" * 52)
    if USE_OLLAMA and not USE_CLOUD:
        threading.Thread(target=warm_up, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), SubhaHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Subha...")
    finally:
        server.server_close()
