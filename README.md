# Olly Focus Group — Test Runner & Dashboard

Persona agent'ları GPT-4o ile çalıştırır, Olly pipeline'ını test eder,
0–10 arası skorlar ve sonuçları web dashboard'unda gösterir.

---

## Lokal Kurulum

```bash
pip install -r requirements.txt
cp .env.example .env
# OPENAI_API_KEY'i .env'e yaz

python api.py
# → http://localhost:8000
```

OLLY_API_URL boş bırakılırsa mock modda çalışır.

---

## Railway Deploy

```bash
# 1. Railway CLI kur
npm install -g @railway/cli

# 2. Giriş yap
railway login

# 3. Proje oluştur
railway new

# 4. Ortam değişkenlerini ekle (Railway dashboard Variables sekmesi)
#    OPENAI_API_KEY, OLLY_API_URL, OLLY_AUTH_TYPE, OLLY_AUTH_VALUE
#    SLACK_WEBHOOK_URL (opsiyonel)

# 5. Deploy et
railway up
```

railway.toml ve Dockerfile hazır, başka ayar gerekmez.

---

## .env Ayarları

```env
OPENAI_API_KEY=sk-...

OLLY_API_URL=
OLLY_AUTH_TYPE=none
OLLY_AUTH_VALUE=
OLLY_AUTH_HEADER=X-Api-Key
MOCK_MODE=true

REPORTS_DIR=reports
SLACK_WEBHOOK_URL=
WEBHOOK_URL=

RETENTION_CRITICAL=5.0
RETENTION_IMPROVEMENT=8.0

WEIGHT_ACTION=0.35
WEIGHT_MEMORY=0.25
WEIGHT_INTENT=0.25
WEIGHT_TONE_SAFETY=0.15
WEIGHT_STEP_AVG=0.70
WEIGHT_RETENTION=0.30

OPENAI_MODEL=gpt-4o
PERSONA_TEMPERATURE=0.7
```

---

## Dashboard Sekmeleri

| Sekme | İçerik |
|---|---|
| Live Run | Persona seçimi, test başlat, canlı adım adım log |
| Reports | Geçmiş raporlar listesi, iki raporu karşılaştır |
| Personas | Persona yönetimi — ekle / düzenle / sil |

Sidebar'dan scheduler kurulabilir (günlük veya haftalık, seçilen saat).

---

## Skor Referansı

| Skor | Durum | Aksiyon |
|---|---|---|
| 8–10 | 🟢 Sağlıklı | Takip et |
| 5–7.9 | 🟡 İyileştirme | Sprint'e al |
| < 5 | 🔴 Kritik | Acil müdahale |

---

## Olly API Bağlantısı

runner.py içinde iki yer güncellenir:
1. _normalize_response(raw) — Olly'nin alan adlarını standartla eşleştir
2. call_olly() içindeki payload — request body yapısını ayarla

Auth .env'den otomatik okunur.

---

## Dosya Yapısı

```
olly_focus_group/
├── api.py            FastAPI — REST + SSE
├── runner.py         Test döngüsü, evaluator, skorlama
├── personas.py       10 persona + ground truth
├── config.py         .env ayarları
├── dashboard.html    Tek dosya frontend
├── main.py           CLI (terminal kullanımı)
├── Dockerfile
├── railway.toml
├── requirements.txt
├── .env.example
└── reports/          Otomatik oluşturulur
```
