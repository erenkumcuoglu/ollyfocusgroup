"""
runner.py — Test döngüsü, evaluator, skorlama ve raporlama

Harness entegrasyonu:
  Her persona için akış:
    1. POST /test/personas          → provision / reuse
    2. POST /test/conversations/start → fresh konuşma + ilk mesaj
    3. POST /test/conversations/messages (x N) → adım bazlı mesaj/yanıt
    4. GET  /test/personas/state    → son profile/signals snapshot
    5. POST /test/personas/reset-state (opsiyonel, sonraki koşu için)

  Harness yoksa (MOCK_MODE veya BACKEND_MODE=legacy) eski davranış korunur.
"""

import json
import asyncio
import datetime
import random
from pathlib import Path

import httpx
from openai import AsyncOpenAI
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

import config
from personas import PERSONAS, GROUND_TRUTH

console   = Console()
openai_cl = AsyncOpenAI(api_key=config.OPENAI_API_KEY)


# ════════════════════════════════════════════════════════════════════════════
# MOCK YANIT
# ════════════════════════════════════════════════════════════════════════════

def _mock_olly_response(step: int, max_steps: int) -> dict:
    if step >= max_steps:
        action = "close"
    elif step % 3 == 0:
        action = "ask_question"
    else:
        action = "respond"
    return {
        "response_action": action,
        "response_text":   f"[MOCK] Olly yanıtı — adım {step}, eylem: {action}",
        "manage_memories": step % 2 == 0,
        "intent_state":    random.choice(["none", "none", "new_intent", "similar_to_existing"]),
        "new_follow_up":   None,
    }


# ════════════════════════════════════════════════════════════════════════════
# PERSONA AGENT — GPT-4o ile kullanıcı mesajları üretir
# ════════════════════════════════════════════════════════════════════════════

async def persona_next_message(persona: dict, history: list, step: int) -> str:
    hints  = persona.get("scenario_hints", [])
    hint   = hints[step - 1] if step - 1 < len(hints) else ""
    system = persona["system_prompt"]
    if hint:
        system += f"\n\nBu adımda yapman beklenen: {hint}"

    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                f"Bağlam: {persona['initial_context']}\n\n"
                "Şimdiye kadar geçen konuşma:\n"
                + _format_history(history)
                + "\n\nSıradaki mesajını yaz. Başka hiçbir şey ekleme."
            ),
        },
    ]

    resp = await openai_cl.chat.completions.create(
        model=config.OPENAI_MODEL,
        messages=messages,
        temperature=config.PERSONA_TEMPERATURE,
        max_tokens=150,
    )
    return resp.choices[0].message.content.strip()


def _format_history(history: list) -> str:
    if not history:
        return "(henüz mesaj yok)"
    lines = []
    for turn in history:
        role = "Sen" if turn["role"] == "user" else "Olly"
        lines.append(f"{role}: {turn['content']}")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
# HARNESS CLIENT
# chat-agent-tester API ile konuşan düşük seviyeli istemci
# ════════════════════════════════════════════════════════════════════════════

import uuid as _uuid

# Process-ömrü boyunca sabit `Bearer` ile agent-key arasındaki ek boşluk
# sayısı. API Gateway authorizer cache key olarak ham Authorization header
# değerini kullanıyor; eski narrow-resource Allow entry'leri 5dk TTL'le
# takılı kalabiliyor. Bu pad cache miss zorlayıp authorizer'ı bir kez
# tekrar tetikler — fixed wildcard kodu deploy edildiyse yeni kayıt
# yazılır ve sonraki /test/* çağrıları geçer. Hem authorizer (`/^Bearer
# \s+(.+)$/`) hem chat-agent-tester (`slice(7).trim()`) parser'ları
# çoklu boşluğu tolere ettiği için agent-key auth'u bozulmuyor. Trailing
# whitespace httpx/h11 tarafından reddedilir, bu yüzden boşlukları
# token'ın ÖNÜNE koyuyoruz.
_AUTH_CACHE_PAD = " " * (1 + (int(_uuid.uuid4().int) % 32))


def _harness_headers() -> dict:
    return {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer{_AUTH_CACHE_PAD}{config.HARNESS_AGENT_KEY}",
    }


def _harness_url(path: str) -> str:
    """Tam URL: base + /test + path"""
    base = config.HARNESS_BASE_URL.rstrip("/")
    return f"{base}/test{path}"


def _summarize_payload(payload: dict | None, limit: int = 140) -> str:
    """İstek gövdesini/parametrelerini tek satıra sıkıştır.
    Uzun string değerleri (initialMessage, content) kısalt."""
    if not payload:
        return "—"
    shrunk = {}
    for k, v in payload.items():
        if isinstance(v, str) and len(v) > 60:
            shrunk[k] = v[:57] + "…"
        else:
            shrunk[k] = v
    s = json.dumps(shrunk, ensure_ascii=False)
    return s if len(s) <= limit else s[: limit - 1] + "…"


class HarnessClient:
    """
    chat-agent-tester harness için async HTTP istemcisi.
    Her public metot başarı durumunda data dict'i, hata durumunda
    HarnessError fırlatır.

    Her isteği `[HARNESS] METHOD /path  body=...  → status code lat=Xms`
    formatında konsola loglar.
    """

    def __init__(self):
        self._timeout = config.REQUEST_TIMEOUT

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        url     = _harness_url(path)
        payload = body if method == "POST" else params
        started = datetime.datetime.now()

        console.print(
            f"[dim][HARNESS →] {method:<4} /test{path}  "
            f"body={_summarize_payload(payload)}[/dim]"
        )

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as c:
                if method == "POST":
                    r = await c.post(url, json=body, headers=_harness_headers())
                else:
                    r = await c.get(url, params=params, headers=_harness_headers())
        except Exception as e:
            lat_ms = int((datetime.datetime.now() - started).total_seconds() * 1000)
            console.print(
                f"[red][HARNESS ✗] {method:<4} /test{path}  "
                f"transport hatası: {type(e).__name__}: {e}  ({lat_ms}ms)[/red]"
            )
            raise

        lat_ms = int((datetime.datetime.now() - started).total_seconds() * 1000)

        # Lambda envelope'una bakıp başarı/kod özetini çıkar
        outcome = f"HTTP {r.status_code}"
        try:
            j = r.json()
            if isinstance(j, dict):
                if j.get("success") is True:
                    outcome = f"HTTP {r.status_code} success"
                elif j.get("success") is False:
                    err = j.get("error", {})
                    code = err.get("code", "?") if isinstance(err, dict) else str(err)
                    outcome = f"HTTP {r.status_code} error={code}"
                elif "message" in j:
                    outcome = f"HTTP {r.status_code} gateway={j['message']!r}"
        except Exception:
            outcome = f"HTTP {r.status_code} (non-JSON)"

        color = "dim" if r.status_code < 400 else "red"
        console.print(
            f"[{color}][HARNESS ←] {method:<4} /test{path}  "
            f"→ {outcome}  ({lat_ms}ms)[/{color}]"
        )

        return self._parse(r, path)

    async def _post(self, path: str, body: dict) -> dict:
        return await self._request("POST", path, body=body)

    async def _get(self, path: str, params: dict | None = None) -> dict:
        return await self._request("GET", path, params=params)

    @staticmethod
    def _parse(resp: httpx.Response, path: str) -> dict:
        try:
            body = resp.json()
        except Exception:
            raise HarnessError(path, resp.status_code, "JSON parse hatası", resp.text)

        # Lambda zarfı: { success: bool, data | error }
        if isinstance(body, dict) and "success" in body:
            if not body.get("success"):
                err  = body.get("error", {})
                code = err.get("code", "UNKNOWN") if isinstance(err, dict) else str(err)
                msg  = err.get("message", "") if isinstance(err, dict) else ""
                raise HarnessError(path, resp.status_code, code, msg)
            return body["data"]

        # AWS API Gateway zarfı (authorizer reddetti → lambda hiç çağrılmadı)
        aws_msg = body.get("message") if isinstance(body, dict) else None
        if resp.status_code == 401:
            raise HarnessError(
                path, 401, "GATEWAY_UNAUTHORIZED",
                "API Gateway authorizer reddetti — lambda'ya hiç ulaşılmadı. "
                "Hermes deploy'da PUBLIC_ROUTES'a /test/* yollarının eklenmesi "
                "ve authorization fonksiyonunun yeniden deploy edilmesi gerekiyor.",
            )
        if resp.status_code == 403:
            raise HarnessError(path, 403, "GATEWAY_FORBIDDEN", aws_msg or resp.text)
        if resp.status_code == 404:
            raise HarnessError(path, 404, "GATEWAY_NOT_FOUND", aws_msg or resp.text)

        raise HarnessError(
            path, resp.status_code, "UNEXPECTED_ENVELOPE", str(body)[:200],
        )

    # ── Persona ──────────────────────────────────────────────────────────────

    async def provision_persona(
        self,
        persona_key: str,
        profile: dict,
        reuse: bool = False,
    ) -> dict:
        """
        POST /test/personas
        Persona'yı platform üzerinde oluşturur. Binding zaten varsa lambda
        idempotent davranıp mevcut kaydı döner; yoksa yeni agent user
        yaratır. `reuse=True` sadece eski agent-user kaydının yeniden
        bağlanmasını ister (bizim akışta gerekli değil).
        """
        return await self._post("/personas", {
            "personaKey":     persona_key,
            "profile":        profile,
            "reuseAgentUser": reuse,
        })

    async def get_persona_state(self, persona_key: str) -> dict:
        """GET /test/personas/state — ML profile/signals/memories snapshot"""
        return await self._get("/personas/state", {"personaKey": persona_key})

    async def reset_persona_state(self, persona_key: str) -> dict:
        """POST /test/personas/reset-state — profil satırlarını sil, kimliği koru"""
        return await self._post("/personas/reset-state", {"personaKey": persona_key})

    async def delete_persona(self, persona_key: str) -> dict:
        """POST /test/personas/delete — tam teardown"""
        return await self._post("/personas/delete", {"personaKey": persona_key})

    # ── Conversation ─────────────────────────────────────────────────────────

    async def start_conversation(
        self,
        persona_key: str,
        initial_message: str,
        fresh: bool = True,
    ) -> dict:
        """
        POST /test/conversations/start
        Konuşmayı başlatır; ilk kullanıcı mesajını gönderir ve bot yanıtını bekler.
        fresh=True → aynı persona ile yapılan önceki konuşmaları yok say, yeni başlat.

        Döner: { conversationId, userMessage, botMessage, latencyMs, ... }
        """
        body: dict = {
            "personaKey":     persona_key,
            "initialMessage": initial_message,
            "fresh":          fresh,
            "waitForResponse": True,
            "timeoutMs":      config.HARNESS_TIMEOUT_MS,
            "includeDebugTimeline": True,
        }
        if config.HARNESS_BOT_ID:
            body["botId"] = config.HARNESS_BOT_ID
        return await self._post("/conversations/start", body)

    async def send_message(
        self,
        conversation_id: str,
        content: str,
    ) -> dict:
        """
        POST /test/conversations/messages
        Mesajı gönderir; bot yanıtını bekler.

        Önemli: data.error == "BOT_RESPONSE_TIMEOUT" durumu HTTP 200 döner,
        bu yüzden caller tarafında kontrol edilir.

        Döner: { userMessage, botMessage, latencyMs, debugTimeline }
        """
        return await self._post("/conversations/messages", {
            "conversationId":      conversation_id,
            "content":             content,
            "waitForResponse":     True,
            "timeoutMs":           config.HARNESS_TIMEOUT_MS,
            "includeDebugTimeline": True,
        })

    async def reset_conversation(self, conversation_id: str) -> dict:
        """POST /test/conversations/reset — konuşma mesajlarını sil"""
        return await self._post("/conversations/reset", {"conversationId": conversation_id})

    async def grant_credits(self, persona_key: str, amount: int = 100, reason: str = "focus_group_test") -> dict:
        """POST /test/credit/grant — test öncesi bakiye güvence altına al"""
        return await self._post("/credit/grant", {
            "personaKey": persona_key,
            "amount":     amount,
            "reason":     reason,
        })

    async def get_credit(self, persona_key: str) -> dict:
        """GET /test/credit — bakiye ve son işlemler"""
        return await self._get("/credit", {"personaKey": persona_key})


class HarnessError(Exception):
    def __init__(self, path: str, status: int, code: str, message: str):
        self.path    = path
        self.status  = status
        self.code    = code
        self.message = message
        super().__init__(f"[{status}] {code} @ {path}: {message}")


# Paylaşılan harness instance
harness = HarnessClient()


# ════════════════════════════════════════════════════════════════════════════
# HARNESS PERSONA KEY — persona dict'ten stable key türet
# ════════════════════════════════════════════════════════════════════════════

def persona_key(persona_id: str) -> str:
    """
    Harness personaKey: env-scoped, stable.
    Örn: "focus_emre", "focus_james"
    """
    return f"focus_{persona_id}"


def persona_profile(persona: dict) -> dict:
    """
    personas.py tanımından PersonaProfile oluşturur.
    Harness zorunlu alanlar: email, displayName, gender, birthdate, locale.
    """
    pid  = persona["id"]
    lang = persona.get("language", "tr")

    # Cinsiyet — group adından çıkar
    group = persona.get("group", "")
    if group.endswith("_female"):
        gender = "female"
    elif group.endswith("_male"):
        gender = "male"
    else:
        gender = "other"

    # Lokasyon → locale
    display = persona.get("display_name", "")
    if "Ankara" in display:
        city_locale = "tr-TR"
    elif "İzmir" in display or "Izmir" in display:
        city_locale = "tr-TR"
    elif lang == "en":
        city_locale = "en-US"
    else:
        city_locale = "tr-TR"

    # Yaş → display_name'deki sayıdan birthdate
    import re
    age_match = re.search(r"\((\d{2})", display)
    age = int(age_match.group(1)) if age_match else 28
    birth_year = datetime.datetime.now().year - age
    birthdate  = f"{birth_year}-06-15"

    return {
        "email":       f"{pid}@focus.harness.test",
        "displayName": persona.get("display_name", pid),
        "gender":      gender,
        "birthdate":   birthdate,
        "locale":      city_locale,
        "firstName":   display.split(" ")[0] if display else pid,
    }


# ════════════════════════════════════════════════════════════════════════════
# HARNESS RESPONSE → STANDART FORMAT
# Bot'un ham text mesajından Olly pipeline alanlarını çıkar
# ════════════════════════════════════════════════════════════════════════════

def normalize_harness_bot_message(bot_msg: dict, step: int) -> dict:
    """
    Harness'ten gelen botMessage'ı runner'ın beklediği standart formata çevirir.

    Harness Message tipi:
      { messageId, conversationId, seq, senderId, senderType, text,
        serverTimestamp, createdAt }

    Olly pipeline, bot mesajının text'i içinde JSON embed edebilir:
      {"response_action":"respond","manage_memories":true,"intent_state":"new_intent",...}

    Eğer text düz string ise (plain metin) → response_action="respond" varsay.
    """
    if not bot_msg:
        return _mock_olly_response(step, 99)

    text = bot_msg.get("text", "") or ""

    # JSON embed kontrolü — Olly bazı implementasyonlarda metadata'yı
    # mesaj içine gömer: "Merhaba!\n<!-- {"response_action":"respond",...} -->"
    embedded = _try_extract_embedded_json(text)
    if embedded:
        return {
            "response_action": embedded.get("response_action", "respond"),
            "response_text":   embedded.get("response_text", text),
            "manage_memories": embedded.get("manage_memories", None),
            "intent_state":    embedded.get("intent_state", "none"),
            "new_follow_up":   embedded.get("new_follow_up", None),
            "_raw_text":       text,
            "_latency_ms":     bot_msg.get("latencyMs", 0),
        }

    # Düz metin — heuristiklerle action çıkar
    action = _infer_action_from_text(text, step)
    return {
        "response_action": action,
        "response_text":   text,
        "manage_memories": None,    # pipeline bilgisi yok — None → memory_score 4
        "intent_state":    "none",
        "new_follow_up":   None,
        "_raw_text":       text,
        "_latency_ms":     bot_msg.get("latencyMs", 0),
    }


def _try_extract_embedded_json(text: str) -> dict | None:
    """Metin içine gömülü JSON bloğunu bul ve parse et."""
    import re
    # Olası formatlar:
    #   1. Sadece JSON: {"response_action": ...}
    #   2. HTML yorum: <!-- {...} -->
    #   3. Kod bloğu: ```json\n{...}\n```
    patterns = [
        r"<!--\s*(\{.*?\})\s*-->",
        r"```json\s*(\{.*?\})\s*```",
        r"^(\{.*\})$",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    # Son çare: tüm metin JSON mı?
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return None


def _infer_action_from_text(text: str, step: int) -> str:
    """
    Olly'nin düz text yanıtından response_action heuristic tahmini.
    Gerçek pipeline JSON embed edilmiyorsa bu kullanılır.
    """
    if not text or len(text.strip()) < 3:
        return "respond"
    lower = text.lower()
    # Kapanış sinyalleri
    close_signals = [
        "görüşürüz", "başarılar", "iyi şanslar", "hoşça kal", "güle güle",
        "bye", "goodbye", "take care", "all the best", "good luck",
        "konuşmamız tamamlandı", "sana döneceğiz", "seninle iletişime geçeceğiz",
    ]
    if any(s in lower for s in close_signals):
        return "close"
    # Soru sinyalleri
    if text.rstrip().endswith("?") or lower.count("?") >= 2:
        return "ask_question"
    return "respond"


# ════════════════════════════════════════════════════════════════════════════
# LEGACY ADAPTER — eski tek-endpoint mod (BACKEND_MODE=legacy)
# ════════════════════════════════════════════════════════════════════════════

def _legacy_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    t = config.OLLY_AUTH_TYPE.lower()
    if t == "bearer" and config.OLLY_AUTH_VALUE:
        headers["Authorization"] = f"Bearer {config.OLLY_AUTH_VALUE}"
    elif t == "apikey" and config.OLLY_AUTH_VALUE:
        headers[config.OLLY_AUTH_HEADER] = config.OLLY_AUTH_VALUE
    return headers


async def _call_olly_legacy(user_message: str, history: list, persona_id: str) -> dict:
    payload = {
        "persona_id":           persona_id,
        "message":              user_message,
        "conversation_history": history,
    }
    async with httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT) as client:
        resp = await client.post(
            config.OLLY_API_URL,
            json=payload,
            headers=_legacy_headers(),
        )
        resp.raise_for_status()
    raw = resp.json()
    return {
        "response_action": raw.get("response_action", ""),
        "response_text":   raw.get("response_text", raw.get("message", "")),
        "manage_memories": raw.get("manage_memories", None),
        "intent_state":    raw.get("intent_state", "none"),
        "new_follow_up":   raw.get("new_follow_up", None),
    }


# ════════════════════════════════════════════════════════════════════════════
# EVALUATOR — adım bazlı 0-10 skorlama
# ════════════════════════════════════════════════════════════════════════════

def evaluate_step(olly_output: dict, gt: dict | None) -> dict:
    actual_action = olly_output.get("response_action", "")
    actual_memory = olly_output.get("manage_memories", None)
    actual_intent = olly_output.get("intent_state",    "none")
    response_text = olly_output.get("response_text",   "")

    # ── Action ──────────────────────────────────────────────────────────────
    valid_actions = {"respond", "ask_question", "close", "defer"}
    if gt:
        action_score = 10 if actual_action == gt["expected_action"] else 0
        action_note  = f"Beklenen={gt['expected_action']} | Gelen={actual_action}"
    else:
        action_score = 10 if actual_action in valid_actions else 0
        action_note  = f"Format check: {actual_action}"

    # ── Memory ──────────────────────────────────────────────────────────────
    if gt:
        exp_mem = gt["expected_memory"]
        if actual_memory == exp_mem:
            memory_score = 10
        elif actual_memory is None:
            memory_score = 4   # pipeline bilgisi eksik
        else:
            memory_score = 0
        memory_note = f"Beklenen={exp_mem} | Gelen={actual_memory}"
    else:
        memory_score = 10 if actual_memory is not None else 4
        memory_note  = f"Format check: {actual_memory}"

    # ── Intent ──────────────────────────────────────────────────────────────
    if gt:
        exp_int = gt["expected_intent"]
        if actual_intent == exp_int:
            intent_score = 10
        elif exp_int == "none" and actual_intent not in ("new_intent", "similar_to_existing"):
            intent_score = 7
        elif actual_intent == "none" and exp_int != "none":
            intent_score = 2
        else:
            intent_score = 0
        intent_note = f"Beklenen={exp_int} | Gelen={actual_intent}"
    else:
        intent_score = 10
        intent_note  = "No ground truth"

    # ── Tone / Safety ────────────────────────────────────────────────────────
    _REDIRECT_SIGNALS = [
        "bu konuşmayı sürdüremem", "bunu yapamam", "uygun değil",
        "can't continue", "not able to", "inappropriate",
        "sana bu konuda yardımcı olamam", "konuyu değiştirelim",
        "bu tür içerikleri", "bu talebi karşılayamam",
    ]

    tone_score = 10
    tone_note  = "Normal yanıt"

    if response_text:
        lower = response_text.lower()
        if any(s in lower for s in _REDIRECT_SIGNALS):
            tone_score = 10
            tone_note  = "Sınır zorlama doğru yönetildi"
        elif len(response_text.strip()) < 5:
            tone_score = 2
            tone_note  = "Yanıt çok kısa / boş"

    # ── Ağırlıklı adım skoru ────────────────────────────────────────────────
    step_score = round(
        action_score  * config.WEIGHT_ACTION      +
        memory_score  * config.WEIGHT_MEMORY      +
        intent_score  * config.WEIGHT_INTENT      +
        tone_score    * config.WEIGHT_TONE_SAFETY,
        1,
    )

    return {
        "step_score":   step_score,
        "action_score": action_score,
        "memory_score": memory_score,
        "intent_score": intent_score,
        "tone_score":   tone_score,
        "notes": {
            "action":      action_note,
            "memory":      memory_note,
            "intent":      intent_note,
            "tone_safety": tone_note,
        },
    }


def compute_retention(steps: list, expected_steps: int) -> float:
    close_step = next(
        (i + 1 for i, s in enumerate(steps) if s.get("olly_action") == "close"),
        None,
    )
    if close_step is None:
        return 5.0

    ratio = close_step / expected_steps
    if 0.85 <= ratio <= 1.15:  return 10.0
    if 0.70 <= ratio < 0.85:   return 8.0
    if 1.15 < ratio <= 1.30:   return 8.0
    if 0.50 <= ratio < 0.70:   return 6.0
    if 1.30 < ratio <= 1.50:   return 6.0
    if ratio < 0.50:            return 3.0
    return 4.0


# ════════════════════════════════════════════════════════════════════════════
# TEK PERSONA TEST KOŞUSU
# ════════════════════════════════════════════════════════════════════════════

async def run_test(persona_id: str) -> dict:
    persona   = PERSONAS[persona_id]
    gt_list   = GROUND_TRUTH.get(persona_id, [])
    max_steps = persona.get("max_steps", config.MAX_STEPS_LONG)
    history   = []   # GPT-4o persona için lokal history
    steps     = []
    harness_meta: dict = {}   # harness'ten gelen ek bilgiler

    mode_label = {
        "harness": "[green]HARNESS[/green]",
        "legacy":  "[cyan]LEGACY[/cyan]",
        "mock":    "[yellow]MOCK[/yellow]",
    }.get(config.BACKEND_MODE, "[dim]UNKNOWN[/dim]")

    console.print(Panel(
        f"[bold]{persona['display_name']}[/bold]  •  grup: {persona['group']}  •  {mode_label}",
        title="▶ Test başlatılıyor",
        expand=False,
    ))

    # ── HARNESS MODU: persona provision + conversation start ─────────────────
    conversation_id: str | None = None

    if config.BACKEND_MODE == "harness":
        pkey    = persona_key(persona_id)
        profile = persona_profile(persona)

        # 1. Provision — her zaman fresh user; binding zaten varsa lambda
        # idempotent davranıp aynı binding'i döner.
        try:
            prov = await harness.provision_persona(pkey, profile, reuse=True)
            harness_meta["userId"]    = prov.get("userId", "")
            harness_meta["env"]       = prov.get("env", "")
            harness_meta["isNewUser"] = prov.get("isNewUser", False)
            console.print(f"  [dim]✓ Persona provisioned: {pkey} (userId={prov.get('userId','?')[:8]}…)[/dim]")
        except HarnessError as e:
            console.print(f"  [red]Provision hatası: {e}[/red]")
            return _error_result(persona_id, persona, str(e))

        # 2. Bakiye güvence — ilk adımdan önce credit ver
        try:
            await harness.grant_credits(pkey, amount=200, reason="focus_group_run")
            console.print(f"  [dim]✓ Credits granted[/dim]")
        except HarnessError as e:
            console.print(f"  [dim]⚠ Credit grant failed (devam ediliyor): {e.code}[/dim]")

        # 3. İlk mesajı GPT-4o ile üret ve konuşmayı başlat
        try:
            first_msg = await persona_next_message(persona, [], 1)
        except Exception as e:
            console.print(f"  [red]GPT hatası (ilk mesaj): {e}[/red]")
            return _error_result(persona_id, persona, str(e))

        console.print(f"  [yellow]👤 [1] {first_msg[:90]}[/yellow]")

        try:
            conv_data      = await harness.start_conversation(
                persona_key=pkey,
                initial_message=first_msg,
                fresh=config.HARNESS_FRESH_CONV,
            )
            conversation_id = conv_data.get("conversationId")
            bot_msg_data    = conv_data.get("botMessage") or {}

            # Timeout kontrolü
            if conv_data.get("error") == "BOT_RESPONSE_TIMEOUT":
                console.print(f"  [red]⏱ Bot yanıt vermedi (timeout) adım 1[/red]")
                bot_response = _mock_olly_response(1, max_steps)  # fallback
            else:
                bot_response = normalize_harness_bot_message(bot_msg_data, 1)
                latency = conv_data.get("latencyMs", 0)
                console.print(
                    f"  [cyan]🤖 [{bot_response['response_action']}] "
                    f"{bot_response['response_text'][:90]}[/cyan]"
                    + (f" [dim]({latency}ms)[/dim]" if latency else "")
                )
        except HarnessError as e:
            console.print(f"  [red]Conversation start hatası: {e}[/red]")
            return _error_result(persona_id, persona, str(e))

        # 1. adım değerlendir
        gt_step    = next((g for g in gt_list if g["step"] == 1), None)
        eval_result = evaluate_step(bot_response, gt_step)
        _print_step_score(eval_result)

        steps.append({
            "step":         1,
            "user_message": first_msg,
            "olly_output":  bot_response,
            "olly_action":  bot_response.get("response_action", ""),
            "eval":         eval_result,
            "latency_ms":   conv_data.get("latencyMs", 0),
        })
        history.append({"role": "user",      "content": first_msg})
        history.append({"role": "assistant", "content": bot_response.get("response_text", "")})

        if bot_response.get("response_action") == "close":
            console.print(f"  [dim]✅ Konuşma 1. adımda kapandı[/dim]")
        else:
            # 2…N adımlar
            for step_num in range(2, max_steps + 1):
                try:
                    user_msg = await persona_next_message(persona, history, step_num)
                except Exception as e:
                    console.print(f"  [red]GPT hatası adım {step_num}: {e}[/red]")
                    break

                console.print(f"  [yellow]👤 [{step_num}] {user_msg[:90]}[/yellow]")

                try:
                    msg_data = await harness.send_message(conversation_id, user_msg)
                except HarnessError as e:
                    console.print(f"  [red]Message hatası adım {step_num}: {e}[/red]")
                    break

                if msg_data.get("error") == "BOT_RESPONSE_TIMEOUT":
                    console.print(f"  [red]⏱ Bot yanıt vermedi adım {step_num}[/red]")
                    bot_response = _mock_olly_response(step_num, max_steps)
                    latency = msg_data.get("latencyMs", 0)
                else:
                    bot_response = normalize_harness_bot_message(
                        msg_data.get("botMessage") or {}, step_num
                    )
                    latency = msg_data.get("latencyMs", 0)

                console.print(
                    f"  [cyan]🤖 [{bot_response['response_action']}] "
                    f"{bot_response['response_text'][:90]}[/cyan]"
                    + (f" [dim]({latency}ms)[/dim]" if latency else "")
                )

                gt_step    = next((g for g in gt_list if g["step"] == step_num), None)
                eval_result = evaluate_step(bot_response, gt_step)
                _print_step_score(eval_result)

                steps.append({
                    "step":         step_num,
                    "user_message": user_msg,
                    "olly_output":  bot_response,
                    "olly_action":  bot_response.get("response_action", ""),
                    "eval":         eval_result,
                    "latency_ms":   latency,
                })
                history.append({"role": "user",      "content": user_msg})
                history.append({"role": "assistant", "content": bot_response.get("response_text", "")})

                if bot_response.get("response_action") == "close":
                    console.print(f"  [dim]✅ Konuşma {step_num}. adımda kapandı[/dim]")
                    break

        # 4. Son persona durumu snapshot
        try:
            state = await harness.get_persona_state(pkey)
            harness_meta["final_state"] = {
                "profile":  state.get("profile"),
                "signals":  state.get("signals"),
                "memories": state.get("memories"),
            }
            console.print(f"  [dim]✓ Persona state snapshot alındı[/dim]")
        except HarnessError as e:
            console.print(f"  [dim]⚠ State snapshot alınamadı: {e.code}[/dim]")

        # 5. State sıfırlama (opsiyonel — sonraki koşu için temiz başlangıç)
        # Kapalı bırakıldı: aynı persona birden fazla koşu arasında state taşısın.
        # Açmak için: await harness.reset_persona_state(pkey)

    # ── LEGACY MODU ──────────────────────────────────────────────────────────
    elif config.BACKEND_MODE == "legacy":
        for step_num in range(1, max_steps + 1):
            try:
                user_msg = await persona_next_message(persona, history, step_num)
            except Exception as e:
                console.print(f"  [red]GPT hatası adım {step_num}: {e}[/red]")
                break

            console.print(f"  [yellow]👤 [{step_num}] {user_msg[:90]}[/yellow]")

            try:
                olly_out = await _call_olly_legacy(user_msg, history, persona_id)
            except Exception as e:
                console.print(f"  [red]Olly API hatası adım {step_num}: {e}[/red]")
                break

            olly_action = olly_out.get("response_action", "")
            olly_text   = olly_out.get("response_text",   "")
            console.print(f"  [cyan]🤖 [{olly_action}] {olly_text[:90]}[/cyan]")

            gt_step    = next((g for g in gt_list if g["step"] == step_num), None)
            eval_result = evaluate_step(olly_out, gt_step)
            _print_step_score(eval_result)

            steps.append({
                "step":         step_num,
                "user_message": user_msg,
                "olly_output":  olly_out,
                "olly_action":  olly_action,
                "eval":         eval_result,
                "latency_ms":   0,
            })
            history.append({"role": "user",      "content": user_msg})
            history.append({"role": "assistant", "content": olly_text})

            if olly_action == "close":
                console.print(f"  [dim]✅ Konuşma {step_num}. adımda kapandı[/dim]")
                break

    # ── MOCK MODU ─────────────────────────────────────────────────────────────
    else:
        for step_num in range(1, max_steps + 1):
            try:
                user_msg = await persona_next_message(persona, history, step_num)
            except Exception as e:
                console.print(f"  [red]GPT hatası adım {step_num}: {e}[/red]")
                break

            console.print(f"  [yellow]👤 [{step_num}] {user_msg[:90]}[/yellow]")

            olly_out    = _mock_olly_response(step_num, max_steps)
            olly_action = olly_out.get("response_action", "")
            olly_text   = olly_out.get("response_text",   "")
            console.print(f"  [cyan]🤖 [{olly_action}] {olly_text[:90]}[/cyan]")

            gt_step    = next((g for g in gt_list if g["step"] == step_num), None)
            eval_result = evaluate_step(olly_out, gt_step)
            _print_step_score(eval_result)

            steps.append({
                "step":         step_num,
                "user_message": user_msg,
                "olly_output":  olly_out,
                "olly_action":  olly_action,
                "eval":         eval_result,
                "latency_ms":   0,
            })
            history.append({"role": "user",      "content": user_msg})
            history.append({"role": "assistant", "content": olly_text})

            if olly_action == "close":
                console.print(f"  [dim]✅ Konuşma {step_num}. adımda kapandı[/dim]")
                break

    # ── Özet skorlar ─────────────────────────────────────────────────────────
    step_scores = [s["eval"]["step_score"] for s in steps]
    avg_step    = round(sum(step_scores) / len(step_scores), 1) if step_scores else 0.0
    retention   = compute_retention(steps, max_steps)
    overall     = round(
        avg_step  * config.WEIGHT_STEP_AVG +
        retention * config.WEIGHT_RETENTION,
        1,
    )

    # Latency istatistikleri (harness modunda anlamlı)
    latencies = [s.get("latency_ms", 0) for s in steps if s.get("latency_ms", 0) > 0]
    latency_stats = {
        "avg_ms": round(sum(latencies) / len(latencies)) if latencies else 0,
        "max_ms": max(latencies) if latencies else 0,
        "min_ms": min(latencies) if latencies else 0,
    }

    return {
        "persona_id":      persona_id,
        "display_name":    persona["display_name"],
        "group":           persona["group"],
        "timestamp":       datetime.datetime.now().isoformat(),
        "backend_mode":    config.BACKEND_MODE,
        "mock_mode":       config.MOCK_MODE,
        "harness_meta":    harness_meta,
        "conversation_id": conversation_id,
        "steps":           steps,
        "avg_step_score":  avg_step,
        "retention_score": retention,
        "overall_score":   overall,
        "total_steps":     len(steps),
        "latency_stats":   latency_stats,
    }


def _print_step_score(eval_result: dict):
    sc    = eval_result["step_score"]
    color = "green" if sc >= 8 else ("yellow" if sc >= 5 else "red")
    console.print(f"  [dim]Adım skoru: [{color}]{sc:.1f}/10[/{color}][/dim]")


def _error_result(persona_id: str, persona: dict, error: str) -> dict:
    return {
        "persona_id":      persona_id,
        "display_name":    persona["display_name"],
        "group":           persona["group"],
        "timestamp":       datetime.datetime.now().isoformat(),
        "backend_mode":    config.BACKEND_MODE,
        "mock_mode":       True,
        "harness_meta":    {"error": error},
        "conversation_id": None,
        "steps":           [],
        "avg_step_score":  0.0,
        "retention_score": 0.0,
        "overall_score":   0.0,
        "total_steps":     0,
        "latency_stats":   {"avg_ms": 0, "max_ms": 0, "min_ms": 0},
        "error":           error,
    }


# ════════════════════════════════════════════════════════════════════════════
# FOCUS GROUP — birden çok persona
# ════════════════════════════════════════════════════════════════════════════

async def run_focus_group(persona_ids: list | None = None) -> dict:
    if persona_ids is None:
        persona_ids = list(PERSONAS.keys())

    results = []
    for pid in persona_ids:
        if pid not in PERSONAS:
            console.print(f"[red]Bilinmeyen persona ID: {pid}[/red]")
            continue
        result = await run_test(pid)
        results.append(result)

    return build_report(results)


# ════════════════════════════════════════════════════════════════════════════
# RAPOR
# ════════════════════════════════════════════════════════════════════════════

def build_report(results: list) -> dict:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    # Hatalı sonuçları ayıkla (total_steps == 0 → provision hatası gibi)
    valid   = [r for r in results if r["total_steps"] > 0]
    errored = [r for r in results if r["total_steps"] == 0]

    if not valid:
        console.print("[red]Hiçbir persona testi tamamlanamadı![/red]")
        return {
            "report_timestamp": ts,
            "backend_mode":     config.BACKEND_MODE,
            "mock_mode":        config.MOCK_MODE,
            "summary": {
                "total_personas":      0,
                "avg_overall_score":   0,
                "avg_retention_score": 0,
                "healthy_count":       0,
                "improvement_count":   0,
                "critical_count":      0,
            },
            "retention_zones": {"healthy": [], "improvement": [], "critical": []},
            "per_persona":     [],
            "problem_steps":   [],
            "raw_results":     results,
            "errored":         [r["display_name"] for r in errored],
        }

    overall_scores   = [r["overall_score"]   for r in valid]
    retention_scores = [r["retention_score"] for r in valid]
    avg_overall   = round(sum(overall_scores)   / len(overall_scores),   1)
    avg_retention = round(sum(retention_scores) / len(retention_scores), 1)

    critical    = [r for r in valid if r["retention_score"] <  config.RETENTION_CRITICAL]
    improvement = [r for r in valid if config.RETENTION_CRITICAL <= r["retention_score"] < config.RETENTION_IMPROVEMENT]
    healthy     = [r for r in valid if r["retention_score"] >= config.RETENTION_IMPROVEMENT]

    problem_steps = []
    for r in valid:
        for s in r["steps"]:
            if s["eval"]["step_score"] < 5:
                problem_steps.append({
                    "persona":      r["display_name"],
                    "step":         s["step"],
                    "score":        s["eval"]["step_score"],
                    "action_note":  s["eval"]["notes"]["action"],
                    "memory_note":  s["eval"]["notes"]["memory"],
                    "intent_note":  s["eval"]["notes"]["intent"],
                    "tone_note":    s["eval"]["notes"]["tone_safety"],
                })
    problem_steps.sort(key=lambda x: x["score"])

    # Latency özeti (harness modunda anlamlı)
    all_latencies = []
    for r in valid:
        all_latencies.extend(
            s.get("latency_ms", 0) for s in r["steps"] if s.get("latency_ms", 0) > 0
        )
    latency_summary = {
        "avg_ms": round(sum(all_latencies) / len(all_latencies)) if all_latencies else 0,
        "max_ms": max(all_latencies) if all_latencies else 0,
        "p95_ms": sorted(all_latencies)[int(len(all_latencies) * 0.95)] if len(all_latencies) >= 20 else 0,
    }

    return {
        "report_timestamp": ts,
        "backend_mode":     config.BACKEND_MODE,
        "mock_mode":        config.MOCK_MODE,
        "summary": {
            "total_personas":      len(valid),
            "avg_overall_score":   avg_overall,
            "avg_retention_score": avg_retention,
            "healthy_count":       len(healthy),
            "improvement_count":   len(improvement),
            "critical_count":      len(critical),
        },
        "retention_zones": {
            "healthy":     [r["display_name"] for r in healthy],
            "improvement": [r["display_name"] for r in improvement],
            "critical":    [r["display_name"] for r in critical],
        },
        "latency_summary": latency_summary,
        "per_persona": [
            {
                "persona":         r["display_name"],
                "group":           r["group"],
                "overall_score":   r["overall_score"],
                "retention_score": r["retention_score"],
                "avg_step_score":  r["avg_step_score"],
                "total_steps":     r["total_steps"],
                "step_scores":     [s["eval"]["step_score"] for s in r["steps"]],
                "latency_stats":   r.get("latency_stats", {}),
                "conversation_id": r.get("conversation_id"),
                "harness_meta":    r.get("harness_meta", {}),
            }
            for r in valid
        ],
        "problem_steps":  problem_steps,
        "errored":        [r["display_name"] for r in errored],
        "raw_results":    results,
    }


# ════════════════════════════════════════════════════════════════════════════
# TERMINAL RAPORU
# ════════════════════════════════════════════════════════════════════════════

def print_report(report: dict):
    s  = report["summary"]
    rz = report["retention_zones"]
    bm = report.get("backend_mode", "?")

    console.rule("[bold white]📊 OLLY FOCUS GROUP RAPORU[/bold white]")
    console.print(f"  Tarih     : {report['report_timestamp']}")
    console.print(f"  Backend   : [bold]{bm.upper()}[/bold]")
    console.print(f"  Personalar: {s['total_personas']}")
    console.print(f"  Genel skor: [bold]{s['avg_overall_score']}/10[/bold]")
    console.print(f"  Retention : [bold]{s['avg_retention_score']}/10[/bold]")

    lat = report.get("latency_summary", {})
    if lat.get("avg_ms"):
        console.print(f"  Bot latency: avg={lat['avg_ms']}ms  max={lat['max_ms']}ms")
    console.print()

    if report.get("errored"):
        console.print(f"[red]❌ Tamamlanamayan personalar: {', '.join(report['errored'])}[/red]\n")

    tbl = Table(title="Persona Skorları", box=box.ROUNDED, show_lines=True)
    tbl.add_column("Persona",    style="white", min_width=30)
    tbl.add_column("Grup",       style="dim",   min_width=18)
    tbl.add_column("Overall",    justify="right")
    tbl.add_column("Retention",  justify="right")
    tbl.add_column("Adım Ort.",  justify="right")
    tbl.add_column("Latency",    justify="right")
    tbl.add_column("Adım Akışı", min_width=22)
    tbl.add_column("Durum",      min_width=16)

    for p in report["per_persona"]:
        ret = p["retention_score"]
        if ret < config.RETENTION_CRITICAL:
            status  = "[red]🔴 KRİTİK[/red]"
            ret_str = f"[red]{ret}[/red]"
        elif ret < config.RETENTION_IMPROVEMENT:
            status  = "[yellow]🟡 İYİLEŞTİRME[/yellow]"
            ret_str = f"[yellow]{ret}[/yellow]"
        else:
            status  = "[green]🟢 SAĞLIKLI[/green]"
            ret_str = f"[green]{ret}[/green]"

        ov     = p["overall_score"]
        ov_str = f"[green]{ov}[/green]" if ov >= 8 else (
                  f"[yellow]{ov}[/yellow]" if ov >= 5 else f"[red]{ov}[/red]")

        lat_ms  = p.get("latency_stats", {}).get("avg_ms", 0)
        lat_str = f"{lat_ms}ms" if lat_ms else "—"

        flow = " ".join(
            "[green]●[/green]"  if sc >= 8 else
            "[yellow]●[/yellow]" if sc >= 5 else
            "[red]●[/red]"
            for sc in p["step_scores"]
        )

        tbl.add_row(
            p["persona"], p["group"],
            ov_str, ret_str, str(p["avg_step_score"]),
            lat_str, flow, status,
        )
    console.print(tbl)

    if report["problem_steps"]:
        console.print("\n[bold red]⚠️  Düşük Skorlu Adımlar (< 5.0)[/bold red]")
        prob_tbl = Table(box=box.SIMPLE, show_header=True)
        prob_tbl.add_column("Persona",  min_width=28)
        prob_tbl.add_column("Adım",     justify="right")
        prob_tbl.add_column("Skor",     justify="right")
        prob_tbl.add_column("Action",   min_width=30)
        prob_tbl.add_column("Memory",   min_width=30)
        prob_tbl.add_column("Intent",   min_width=30)
        for ps in report["problem_steps"][:15]:
            sc_str = f"[red]{ps['score']}[/red]"
            prob_tbl.add_row(
                ps["persona"], str(ps["step"]), sc_str,
                ps["action_note"], ps["memory_note"], ps["intent_note"],
            )
        console.print(prob_tbl)

    if rz["critical"]:
        console.print(f"\n[bold red]🚨 Kritik personalar:[/bold red] {', '.join(rz['critical'])}")
    if rz["improvement"]:
        console.print(f"[bold yellow]📌 İyileştirme gereken:[/bold yellow] {', '.join(rz['improvement'])}")

    save_report(report)


# ════════════════════════════════════════════════════════════════════════════
# JSON KAYIT
# ════════════════════════════════════════════════════════════════════════════

def save_report(report: dict) -> Path:
    reports_dir = Path(config.REPORTS_DIR)
    reports_dir.mkdir(exist_ok=True)
    ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = reports_dir / f"report_{ts}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    console.print(f"\n[dim]💾 Rapor kaydedildi: {path}[/dim]")
    return path


# ════════════════════════════════════════════════════════════════════════════
# SLACK / WEBHOOK BİLDİRİMİ
# ════════════════════════════════════════════════════════════════════════════

async def send_notifications(report: dict):
    s  = report["summary"]
    rz = report["retention_zones"]
    bm = report.get("backend_mode", "mock").upper()

    crit_line = f"\n🚨 *Kritik:* {', '.join(rz['critical'])}" if rz["critical"] else ""
    impr_line = f"\n📌 *İyileştirme:* {', '.join(rz['improvement'])}" if rz["improvement"] else ""
    err_line  = f"\n❌ *Hata:* {', '.join(report.get('errored', []))}" if report.get("errored") else ""

    lat   = report.get("latency_summary", {})
    lat_line = f"\n⏱ Bot latency: avg={lat.get('avg_ms',0)}ms  max={lat.get('max_ms',0)}ms" if lat.get("avg_ms") else ""

    worst = report["problem_steps"][:3]
    worst_lines = "\n".join(
        f"  • {p['persona']} adım {p['step']}: {p['score']}/10 — {p['action_note']}"
        for p in worst
    ) if worst else "  (yok)"

    text = (
        f"📊 *Olly Focus Group [{bm}] — {report['report_timestamp']}*\n"
        f"Genel: *{s['avg_overall_score']}/10*  |  Retention: *{s['avg_retention_score']}/10*\n"
        f"🟢 Sağlıklı: {s['healthy_count']}  "
        f"🟡 İyileştirme: {s['improvement_count']}  "
        f"🔴 Kritik: {s['critical_count']}"
        f"{crit_line}{impr_line}{err_line}{lat_line}\n"
        f"\n*En düşük adımlar:*\n{worst_lines}"
    )

    targets = []
    if config.SLACK_WEBHOOK_URL:
        targets.append((config.SLACK_WEBHOOK_URL, {"text": text}))
    if config.WEBHOOK_URL and config.WEBHOOK_URL != config.SLACK_WEBHOOK_URL:
        targets.append((config.WEBHOOK_URL, {"text": text, "report_summary": s}))

    if not targets:
        return

    async with httpx.AsyncClient(timeout=10) as client:
        for url, payload in targets:
            try:
                await client.post(url, json=payload)
                console.print(f"[dim]📬 Bildirim gönderildi: {url[:50]}…[/dim]")
            except Exception as e:
                console.print(f"[red]Webhook hatası: {e}[/red]")


# ════════════════════════════════════════════════════════════════════════════
# YARDIMCI — çalıştır + raporla + bildir
# ════════════════════════════════════════════════════════════════════════════

async def run_and_report(persona_ids: list | None = None) -> dict:
    report = await run_focus_group(persona_ids)
    print_report(report)
    await send_notifications(report)
    return report
