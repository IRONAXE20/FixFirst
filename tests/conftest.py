from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from fixfirst.brain import Brain
from fixfirst.config import Settings, Tenant
from fixfirst.db import metadata
from fixfirst.llm import ScriptedLLM
from fixfirst.config import KNOWLEDGE_DIR
from fixfirst.playbooks import PlaybookLibrary, get_library
from fixfirst.safety import get_screen
from fixfirst.seed import seed_demo
from fixfirst.session import SessionStore
from fixfirst.tickets import TicketService
from fixfirst.whatsapp import WhatsApp


# The SOP-era playbooks (knowledge/archive/sop) have long multi-step flows with a safety question first; the
# engine tests (look-ahead, quoted answers, unheard steps) run on them. The live KB playbooks are short.
SOP = PlaybookLibrary(playbook_dir=KNOWLEDGE_DIR / "archive" / "sop")


@pytest.fixture
def env(tmp_path):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    engine = create_engine(url)
    metadata.create_all(engine)
    seed_demo(engine)
    settings = Settings(database_url=url, whatsapp_mode="dry_run", public_base_url="http://test")
    tenant = Tenant.load()
    tickets = TicketService(engine, WhatsApp(settings, engine), tenant, settings)
    store = SessionStore(engine, tenant)

    def make(responses, customer="C1", language=None):
        session = store.create(channel="text", customer_id=customer)
        if language:
            session.language = language
        llm = ScriptedLLM(responses)
        brain = Brain(session, llm, tenant=tenant, settings=settings, library=ns.library or get_library(),
                      screen=get_screen(), tickets=tickets)
        return brain, session, llm

    ns = SimpleNamespace(engine=engine, make=make, tenant=tenant, settings=settings, store=store, tickets=tickets,
                         library=None)   # set env.library = SOP to run a test on the archived SOP playbooks
    return ns
