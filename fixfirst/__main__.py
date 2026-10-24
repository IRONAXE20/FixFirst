"""FixFirst command line.

  python -m fixfirst serve            start the web app (call page, /ops, /knowledge)
  python -m fixfirst eval             play the 36 test scenarios against the real agent (needs a model key)
  python -m fixfirst check            validate playbooks, safety lexicon and config
  python -m fixfirst bolna-setup --public-url https://...   put FixFirst on the phone through Bolna
  python -m fixfirst bolna-call +9198xxxxxxxx               have the Bolna agent call a number
"""
import argparse
import asyncio
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="fixfirst")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="Start the web app")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=None)
    ev = sub.add_parser("eval", help="Run the scenario scorecard against the real model (Claude or Sarvam)")
    ev.add_argument("--only", help="Comma-separated scenario ids, e.g. F01,S03")
    ev.add_argument("--scripted", action="store_true", help="Replay the fixed caller turns instead of a simulated caller")
    sub.add_parser("check", help="Validate playbooks and config")
    bs = sub.add_parser("bolna-setup", help="Register FixFirst as Bolna's custom LLM and create the phone agent")
    bs.add_argument("--public-url", required=True, help="Public https URL of this server, e.g. an ngrok address")
    bs.add_argument("--telephony", default="plivo", help="Bolna telephony provider: plivo, vobiz, exotel, twilio")
    bc = sub.add_parser("bolna-call", help="Have the Bolna agent call a phone number")
    bc.add_argument("phone")
    args = parser.parse_args()

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    if args.cmd == "serve":
        import uvicorn

        from .config import get_settings
        from .server import create_app

        settings = get_settings()
        uvicorn.run(create_app(settings), host=args.host, port=args.port or settings.port)
    elif args.cmd == "eval":
        from .eval import run_eval

        sys.exit(asyncio.run(run_eval(args.only.split(",") if args.only else None, scripted=args.scripted)))
    elif args.cmd == "check":
        from .config import get_settings, get_tenant
        from .playbooks import get_library
        from .safety import get_screen

        lib = get_library()
        get_screen()
        s, t = get_settings(), get_tenant()
        print(f"OK: {len(lib.playbooks)} playbooks ({', '.join(lib.playbooks)}), "
              f"{sum(len(p.nodes) for p in lib.playbooks.values())} steps, tenant {t.name}")
        unreviewed = [p.id for p in lib.playbooks.values() if p.status != "reviewed"]
        if unreviewed:
            print(f"WARNING: not yet reviewed by a technician: {', '.join(unreviewed)}")
        print(f"Brain: {s.brain_provider or 'MISSING (offline demo only)'} · Sarvam voice key: "
              f"{'set' if s.sarvam_ready else 'missing'} · Bolna: {'set' if s.bolna_api_key else 'missing'} · "
              f"WhatsApp: {s.whatsapp_mode} · DB: {s.database_url}")
    elif args.cmd == "bolna-setup":
        from .bolna_admin import setup
        from .config import get_settings, get_tenant

        agent_id = setup(get_settings(), get_tenant(), args.public_url, args.telephony)
        print(f"Bolna agent ready: {agent_id} (saved to .env as BOLNA_AGENT_ID). Test it from the Bolna dashboard "
              f"or with: python -m fixfirst bolna-call <your number>")
    elif args.cmd == "bolna-call":
        from .bolna_admin import place_call
        from .config import get_settings

        print(place_call(get_settings(), args.phone))


if __name__ == "__main__":
    main()
