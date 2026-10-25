import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = ROOT / "knowledge"
WEB_DIR = ROOT / "web"
DATA_DIR = ROOT / "data"

load_dotenv(ROOT / ".env")


def now_utc() -> datetime:
    """All timestamps are stored as naive UTC datetimes."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    sarvam_api_key: str = ""
    llm_provider: str = "auto"          # auto | anthropic | sarvam (auto: Claude when ANTHROPIC_API_KEY is set)
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-5-5"
    llm_model: str = "sarvam-105b-conversations"
    stt_model: str = "saaras:v4"
    stt_mode: str = "codemix"
    tts_model: str = "bulbul:v3"
    llm_timeout: float = 8.0
    database_url: str = f"sqlite:///{(DATA_DIR / 'fixfirst.db').as_posix()}"
    whatsapp_mode: str = "dry_run"
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_template_customer: str = "fixfirst_customer_update"
    whatsapp_template_team: str = "fixfirst_team_ticket"
    whatsapp_template_lang: str = "en_IN"   # the templates are approved as English (India)
    whatsapp_business_account_id: str = ""
    public_base_url: str = "http://localhost:7860"
    port: int = 7860
    enable_doc_qa: bool = False
    customer_source: str = "rentbasket"   # rentbasket | demo
    rentbasket_api_url: str = "https://acapi.rentbasket.com/api/get-customer-details"
    bolna_api_key: str = ""
    bolna_agent_id: str = ""
    fixfirst_llm_key: str = ""            # the key Bolna sends when it calls our brain as its custom LLM
    admin_key: str = ""                   # needed to edit playbooks on /knowledge (editing is off without it)
    photo_retention_days: int = 7         # photos are deleted this many days after their ticket is closed

    @classmethod
    def from_env(cls) -> "Settings":
        db = _env("DATABASE_URL", cls.database_url)
        for prefix in ("postgresql://", "postgres://"):   # Supabase's copy-paste URL; we ship psycopg 3
            if db.startswith(prefix):
                db = "postgresql+psycopg://" + db[len(prefix):]
        if db.startswith("sqlite:///") and not db.startswith("sqlite:////") and ":" not in db[10:12]:
            # Resolve relative SQLite paths against the project root, not the cwd.
            db = f"sqlite:///{(ROOT / db[10:]).as_posix()}"
        return cls(
            sarvam_api_key=_env("SARVAM_API_KEY"),
            llm_provider=_env("LLM_PROVIDER", "auto").lower(),
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            anthropic_model=_env("ANTHROPIC_MODEL", cls.anthropic_model),
            llm_model=_env("SARVAM_LLM_MODEL", cls.llm_model),
            stt_model=_env("SARVAM_STT_MODEL", cls.stt_model),
            stt_mode=_env("SARVAM_STT_MODE", cls.stt_mode),
            tts_model=_env("SARVAM_TTS_MODEL", cls.tts_model),
            llm_timeout=float(_env("LLM_TIMEOUT_SECONDS", "8")),
            database_url=db,
            whatsapp_mode=_env("WHATSAPP_MODE", "dry_run").lower(),
            whatsapp_token=_env("WHATSAPP_TOKEN"),
            whatsapp_phone_number_id=_env("WHATSAPP_PHONE_NUMBER_ID"),
            whatsapp_template_customer=_env("WHATSAPP_TEMPLATE_CUSTOMER", cls.whatsapp_template_customer),
            whatsapp_template_team=_env("WHATSAPP_TEMPLATE_TEAM", cls.whatsapp_template_team),
            whatsapp_template_lang=_env("WHATSAPP_TEMPLATE_LANG", cls.whatsapp_template_lang),
            # On Render, RENDER_EXTERNAL_URL is the service's public https address.
            public_base_url=(_env("PUBLIC_BASE_URL") or _env("RENDER_EXTERNAL_URL") or cls.public_base_url).rstrip("/"),
            port=int(_env("PORT", "7860")),
            enable_doc_qa=_env("ENABLE_DOC_QA", "false").lower() in ("1", "true", "yes"),
            whatsapp_business_account_id=_env("WHATSAPP_BUSINESS_ACCOUNT_ID"),
            customer_source=_env("CUSTOMER_SOURCE", cls.customer_source).lower(),
            rentbasket_api_url=_env("RENTBASKET_API_URL", cls.rentbasket_api_url),
            bolna_api_key=_env("BOLNA_API_KEY"),
            bolna_agent_id=_env("BOLNA_AGENT_ID"),
            fixfirst_llm_key=_env("FIXFIRST_LLM_KEY"),
            admin_key=_env("ADMIN_KEY"),
            photo_retention_days=int(_env("PHOTO_RETENTION_DAYS", "7")),
        )

    @property
    def sarvam_ready(self) -> bool:
        return bool(self.sarvam_api_key) and not self.sarvam_api_key.startswith("bn-")

    @property
    def brain_provider(self) -> str | None:
        """Which model runs the brain: "anthropic", "sarvam", or None (no key; typed demo brain only)."""
        if self.llm_provider == "anthropic" or (self.llm_provider == "auto" and self.anthropic_api_key):
            return "anthropic" if self.anthropic_api_key else None
        return "sarvam" if self.sarvam_ready else None


@dataclass
class Tenant:
    """The company FixFirst answers for (knowledge/tenant.yaml)."""

    id: str
    name: str
    agent_name: str
    service_area: str
    support_phone: str
    default_language: str
    tts_speaker: str
    team_whatsapp: list[str] = field(default_factory=list)
    on_call_name: str = ""
    urgent_callback_minutes: int = 15
    normal_visit_promise: dict = field(default_factory=dict)
    emergency_numbers: dict = field(default_factory=dict)
    followup_hours: int = 24
    technician_visit_cost_inr: int = 0

    @classmethod
    def load(cls, path: Path | None = None) -> "Tenant":
        raw = yaml.safe_load((path or KNOWLEDGE_DIR / "tenant.yaml").read_text(encoding="utf-8"))
        return cls(
            id=raw["id"],
            name=raw["name"],
            agent_name=raw.get("agent_name", "Priya"),
            service_area=raw.get("service_area", ""),
            support_phone=raw.get("support_phone", ""),
            default_language=raw.get("default_language", "hi"),
            tts_speaker=raw.get("tts_speaker", "priya"),
            team_whatsapp=[str(n) for n in raw.get("team_whatsapp") or []],
            on_call_name=raw.get("on_call_name", ""),
            urgent_callback_minutes=int(raw.get("urgent_callback_minutes", 15)),
            normal_visit_promise=raw.get("normal_visit_promise") or {},
            emergency_numbers=raw.get("emergency_numbers") or {},
            followup_hours=int(raw.get("followup_hours", 24)),
            technician_visit_cost_inr=int(raw.get("technician_visit_cost_inr", 0)),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()


@lru_cache
def get_tenant() -> Tenant:
    return Tenant.load()
