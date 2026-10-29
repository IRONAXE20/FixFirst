"""Set up the Bolna agent that puts FixFirst on the phone, and place test calls.

  python -m fixfirst bolna-setup --public-url https://<your-tunnel>   register FixFirst as Bolna's custom LLM
                                                                      and create (or update) the agent
  python -m fixfirst bolna-call +9198xxxxxxxx                         have the agent call a number
"""
import json
import re
import secrets

import httpx

from .bolna import MARKER, webhook_token
from .config import ROOT, Settings, Tenant

API = "https://api.bolna.ai"

# Bolna fills {execution_id}, {from_number} and {to_number} in; bolna.py reads them back to find the call.
# {customer_phone} only exists when bolna-call passes it.
SYSTEM_PROMPT = (f"{MARKER} call_ref={{execution_id}} from={{from_number}} to={{to_number}} "
                 "customer_phone={customer_phone}\n"
                 "Replies come from the FixFirst service (playbooks, safety screen, tickets); this prompt only "
                 "carries the call details.")


# Bolna's "hangup using a prompt" check runs on Bolna's own model after each reply. Its default rule hung up
# right after "क्या मैं आपकी और कोई मदद कर सकती हूँ?" on a live call; a rule keyed only on the assistant's
# goodbye never fired (26 Oct), apparently because Bolna's check reads mainly the caller's lines. So it may
# end the call on either sign. Only FixFirst's scripted goodbyes (brain.PHRASES goodbye / safety_goodbye /
# goodbye_again) contain the goodbye phrases, and the model is told never to say them. When Bolna offers its
# end_call tool, FixFirst calls it itself after the goodbye (server.py), which doesn't depend on this check.
HANGUP_PROMPT = (
    "You decide whether to end this phone call now. Answer Yes if EITHER is true:\n"
    "1. The assistant's most recent message is a goodbye containing \"Have a good day\", \"आपका दिन शुभ हो\", "
    "\"अलविदा\", \"Have a nice day\" or \"goodbye\".\n"
    "2. The user's last line is a clear sign-off that ends the conversation: thanks plus goodbye, or declining "
    "more help, such as \"नहीं बस\", \"नहीं, धन्यवाद\", \"बस इतना ही\", \"ओके, थैंक यू, बाय\", \"bye\", "
    "\"no that's all, thanks\".\n"
    "Otherwise answer No. A plain \"नहीं\" / \"no\" that answers a question about the appliance (for example "
    "\"नहीं, ऐसा कुछ नहीं है\" to a safety question, or \"नहीं हुआ\" to whether something worked) is NOT a "
    "sign-off. Never end the call while the user is describing a problem, asking something, or in the middle "
    "of a troubleshooting step. If unsure, answer No."
)


def _client(settings: Settings) -> httpx.Client:
    if not settings.bolna_api_key:
        raise SystemExit("BOLNA_API_KEY is not set in .env")
    return httpx.Client(base_url=API, timeout=30, headers={"Authorization": f"Bearer {settings.bolna_api_key}"})


def _set_env(name: str, value: str) -> None:
    path = ROOT / ".env"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    line = f"{name}={value}"
    text = re.sub(rf"^{name}=.*$", line, text, flags=re.M) if re.search(rf"^{name}=", text, re.M) \
        else text.rstrip("\n") + f"\n{line}\n"
    path.write_text(text, encoding="utf-8")


def welcome(tenant: Tenant) -> str:
    # RentBasket's call script, 1. Greeting ("Namaste! Main Rentbasket ki FixFirst service assistant bol rahi hoon.
    # Aapki kya madad kar sakti hoon?"); the caller's language decides the rest of the call.
    return f"नमस्ते! मैं {tenant.name} की FixFirst service assistant बोल रही हूँ। आपकी क्या मदद कर सकती हूँ?"


TRANSFER_TOOL = "transfer_call_care"


def transfer_tools(tenant: Tenant) -> dict:
    """Bolna's built-in transfer tool, to the customer care number (9am-8pm handoff). FixFirst calls it after
    saying "Main aapko hamari service team se connect kar rahi hoon" (server.py); Bolna dials and bridges."""
    return {
        "tools": [{
            "name": TRANSFER_TOOL, "key": "transfer_call",
            "description": "Transfer the call to RentBasket's customer care team when the caller needs a person.",
            "parameters": {"type": "object", "properties": {"call_sid": {"type": "string", "description": "unique call id"}},
                           "required": ["call_sid"]},
        }],
        "tools_params": {
            TRANSFER_TOOL: {"method": "POST", "url": None, "api_token": None,
                            "param": json.dumps({"call_transfer_number": _e164(tenant.care_number()),
                                                 "call_sid": "%(call_sid)s"})},
        },
    }


def agent_payload(settings: Settings, tenant: Tenant, public_url: str, telephony: str) -> dict:
    base = public_url.rstrip("/")
    return {
        "agent_config": {
            "agent_name": f"{tenant.name} FixFirst",
            "agent_welcome_message": welcome(tenant),
            "webhook_url": f"{base}/bolna/webhook/{webhook_token(settings.fixfirst_llm_key)}",
            "call_summary_enabled": False,
            "tasks": [{
                "task_type": "conversation",
                "tools_config": {
                    "llm_agent": {
                        "agent_type": "simple_llm_agent",
                        "agent_flow_type": "streaming",
                        "llm_config": {"agent_flow_type": "streaming", "provider": "custom", "model": "fixfirst",
                                       "base_url": f"{base}/bolna/v1", "family": "openai",
                                       "max_tokens": 300, "temperature": 0.2},
                    },
                    "synthesizer": {
                        "provider": "sarvam",
                        "provider_config": {"voice": tenant.tts_speaker.title(), "voice_id": tenant.tts_speaker,
                                            "model": settings.tts_model, "language": "hi", "speed": 1.0},
                        "stream": True, "buffer_size": 150, "audio_format": "wav",
                    },
                    "transcriber": {"provider": "sarvam", "model": settings.stt_model, "language": "hi",
                                    "stream": True, "endpointing": 800},
                    "input": {"provider": telephony, "format": "wav"},
                    "output": {"provider": telephony, "format": "wav"},
                    "api_tools": transfer_tools(tenant),
                },
                "toolchain": {"execution": "parallel", "pipelines": [["transcriber", "llm", "synthesizer"]]},
                "task_config": {
                    # Long enough for a caller who goes quiet while cleaning filters; the call normally
                    # ends through the hangup check once the caller has said goodbye.
                    "hangup_after_silence": 90,
                    "call_terminate": 900,
                    # Bolna asks our endpoint after each reply whether to hang up; bolna.py side_check answers
                    # "Yes" only after the caller declined "anything else?" and heard the goodbye.
                    "hangup_after_LLMCall": True,
                    "call_cancellation_prompt": HANGUP_PROMPT,
                    "number_of_words_for_interruption": 2,
                    "backchanneling": False,
                    "ambient_noise": False,
                    "voicemail": False,
                    "check_if_user_online": True,
                    "trigger_user_online_message_after": 20,
                },
            }],
        },
        "agent_prompts": {"task_1": {"system_prompt": SYSTEM_PROMPT}},
    }


def setup(settings: Settings, tenant: Tenant, public_url: str, telephony: str = "plivo") -> str:
    if not public_url.startswith("https://"):
        raise SystemExit("--public-url must be the public https address of this server (for example an ngrok URL)")
    if not settings.fixfirst_llm_key:
        settings.fixfirst_llm_key = "ff_" + secrets.token_urlsafe(24)
        _set_env("FIXFIRST_LLM_KEY", settings.fixfirst_llm_key)
    base = public_url.rstrip("/")
    with _client(settings) as c:
        # Bolna checks the endpoint (GET /models) here, so `python -m fixfirst serve` must be reachable.
        r = c.post("/user/model/custom", json={"custom_model_name": "fixfirst", "custom_model_url": f"{base}/bolna/v1",
                                               "custom_model_key": settings.fixfirst_llm_key,
                                               "custom_model_family": "openai"})
        if r.status_code >= 400 and "exist" not in r.text.lower():
            raise SystemExit(f"Registering the custom LLM failed: HTTP {r.status_code} {r.text[:300]}")
        payload = agent_payload(settings, tenant, public_url, telephony)
        if settings.bolna_agent_id:
            r = c.put(f"/v2/agent/{settings.bolna_agent_id}", json=payload)
        else:
            r = c.post("/v2/agent", json=payload)
        if r.status_code >= 400:
            raise SystemExit(f"Saving the agent failed: HTTP {r.status_code} {r.text[:500]}")
        agent_id = settings.bolna_agent_id or r.json().get("agent_id")
    if agent_id and not settings.bolna_agent_id:
        _set_env("BOLNA_AGENT_ID", agent_id)
    return agent_id


def _e164(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    return f"+91{digits}" if len(digits) == 10 else f"+{digits}"


def place_call(settings: Settings, phone: str, as_customer: str | None = None) -> dict:
    """Ring `phone`. With `as_customer`, the agent looks the caller up as that RentBasket number instead
    (to test customer recognition from your own phone); the call is marked as a test, so no customer
    WhatsApp messages or follow-ups go to that real customer."""
    if not settings.bolna_agent_id:
        raise SystemExit("BOLNA_AGENT_ID is not set; run bolna-setup first")
    user_data = {"customer_phone": _e164(phone)}
    if as_customer:
        user_data = {"test_customer_phone": _e164(as_customer)}
    with _client(settings) as c:
        r = c.post("/call", json={"agent_id": settings.bolna_agent_id, "recipient_phone_number": _e164(phone),
                                  "user_data": user_data})
    if r.status_code >= 400:
        raise SystemExit(f"Call failed: HTTP {r.status_code} {r.text[:300]}")
    return r.json()
