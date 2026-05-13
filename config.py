"""
config.py — Olly Focus Group Test Runner
Tüm ayarlar buradan yönetilir. .env dosyasından okunur.
"""

import os
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# OpenAI — Persona agent (GPT-4o ile kullanıcı mesajları üretilir)
# ---------------------------------------------------------------------------
OPENAI_API_KEY:      str   = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL:        str   = os.getenv("OPENAI_MODEL", "gpt-4o")
PERSONA_TEMPERATURE: float = float(os.getenv("PERSONA_TEMPERATURE", "0.7"))

# ---------------------------------------------------------------------------
# Harness API — chat-agent-tester
#
# HARNESS_BASE_URL  : Lambda'nın kök URL'i, /test prefix dahil değil
#                     Örn: https://functions.revolutions.social
# HARNESS_AGENT_KEY : Authorization: Bearer <key>
# HARNESS_BOT_ID    : Opsiyonel — POST /test/conversations/start botId override
# HARNESS_TIMEOUT_MS: Bot yanıt bekleme süresi (ms). Maks 60000.
# HARNESS_FRESH_CONV: Her test koşusunda fresh=true ile yeni konuşma başlat
# ---------------------------------------------------------------------------
HARNESS_BASE_URL:   str = os.getenv("HARNESS_BASE_URL",   "https://functions.revolutions.social")
HARNESS_AGENT_KEY:  str = os.getenv("HARNESS_AGENT_KEY",  "")
HARNESS_BOT_ID:     str = os.getenv("HARNESS_BOT_ID",     "")   # boşsa default bot kullanılır
HARNESS_TIMEOUT_MS: int = int(os.getenv("HARNESS_TIMEOUT_MS", "30000"))
HARNESS_FRESH_CONV: bool = os.getenv("HARNESS_FRESH_CONV", "true").lower() == "true"

# ---------------------------------------------------------------------------
# Eski OLLY_API_URL ayarları (geriye dönük uyumluluk — harness aktifken kullanılmaz)
# ---------------------------------------------------------------------------
OLLY_API_URL:     str = os.getenv("OLLY_API_URL",     "")
OLLY_AUTH_TYPE:   str = os.getenv("OLLY_AUTH_TYPE",   "none")
OLLY_AUTH_VALUE:  str = os.getenv("OLLY_AUTH_VALUE",  "")
OLLY_AUTH_HEADER: str = os.getenv("OLLY_AUTH_HEADER", "X-Api-Key")

# ---------------------------------------------------------------------------
# Çalışma modu
#
# MOCK_MODE=true  → Harness'e hiç bağlanma, sahte yanıtlar üret
# MOCK_MODE=false (default) → HARNESS_AGENT_KEY doluysa harness kullan,
#                              HARNESS_AGENT_KEY boş + OLLY_API_URL doluysa
#                              eski tek-endpoint moda düş, ikisi de boşsa mock.
# ---------------------------------------------------------------------------
_force_mock = os.getenv("MOCK_MODE", "false").lower() == "true"
_has_harness = bool(HARNESS_AGENT_KEY)
_has_legacy  = bool(OLLY_API_URL)

if _force_mock:
    MOCK_MODE    = True
    BACKEND_MODE = "mock"
elif _has_harness:
    MOCK_MODE    = False
    BACKEND_MODE = "harness"    # chat-agent-tester harness
elif _has_legacy:
    MOCK_MODE    = False
    BACKEND_MODE = "legacy"     # eski tek-endpoint
else:
    MOCK_MODE    = True
    BACKEND_MODE = "mock"

# ---------------------------------------------------------------------------
# Raporlama
# ---------------------------------------------------------------------------
REPORTS_DIR:       str = os.getenv("REPORTS_DIR",       "reports")
SLACK_WEBHOOK_URL: str = os.getenv("SLACK_WEBHOOK_URL", "")
WEBHOOK_URL:       str = os.getenv("WEBHOOK_URL",       "")

# ---------------------------------------------------------------------------
# Skorlama eşikleri
# ---------------------------------------------------------------------------
RETENTION_CRITICAL:    float = float(os.getenv("RETENTION_CRITICAL",    "5.0"))
RETENTION_IMPROVEMENT: float = float(os.getenv("RETENTION_IMPROVEMENT", "8.0"))

WEIGHT_ACTION:      float = float(os.getenv("WEIGHT_ACTION",      "0.35"))
WEIGHT_MEMORY:      float = float(os.getenv("WEIGHT_MEMORY",      "0.25"))
WEIGHT_INTENT:      float = float(os.getenv("WEIGHT_INTENT",      "0.25"))
WEIGHT_TONE_SAFETY: float = float(os.getenv("WEIGHT_TONE_SAFETY", "0.15"))

WEIGHT_STEP_AVG:  float = float(os.getenv("WEIGHT_STEP_AVG",  "0.70"))
WEIGHT_RETENTION: float = float(os.getenv("WEIGHT_RETENTION", "0.30"))

# ---------------------------------------------------------------------------
# Test parametreleri
# ---------------------------------------------------------------------------
MAX_STEPS_SHORT: int = int(os.getenv("MAX_STEPS_SHORT", "5"))
MAX_STEPS_LONG:  int = int(os.getenv("MAX_STEPS_LONG",  "10"))
REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "35"))   # saniye (HARNESS_TIMEOUT_MS/1000 + buffer)

# ---------------------------------------------------------------------------
# Validasyon
# ---------------------------------------------------------------------------
def validate() -> list[str]:
    warnings = []
    if not OPENAI_API_KEY:
        warnings.append("OPENAI_API_KEY eksik — persona agent (GPT-4o) çalışmaz")
    if BACKEND_MODE == "mock":
        warnings.append("MOCK_MODE aktif — Olly'e gerçek istek gönderilmeyecek")
    elif BACKEND_MODE == "harness":
        warnings.append(f"Harness modu: {HARNESS_BASE_URL}")
        if not HARNESS_BOT_ID:
            warnings.append("HARNESS_BOT_ID boş — varsayılan bot kullanılacak (CHAT_AGENT_TESTER_DEFAULT_BOT_ID)")
    elif BACKEND_MODE == "legacy":
        warnings.append(f"Legacy mod: {OLLY_API_URL}")
    if not SLACK_WEBHOOK_URL:
        warnings.append("SLACK_WEBHOOK_URL eksik — Slack bildirimi devre dışı")
    return warnings
