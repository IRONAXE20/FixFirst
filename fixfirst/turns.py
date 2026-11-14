"""Turns on a phone call, whichever voice platform carries it (Bolna, or our own Pipecat pipeline).

Callers pause mid-sentence and keep talking over the agent, so a reply is sometimes never heard. The next
turn then decides what to do with it: a safety alert, a goodbye or a transfer is said again (it can't be
undone); a ticket line is recapped in one sentence; anything else is undone and the caller's pieces are
answered together, as one sentence.
"""
import copy

from loguru import logger


def checkpoint(session) -> dict:
    """A copy of the session's state, to undo a turn whose reply the caller never heard. The playbook, the
    event log and the live-view subscribers are shared, not copied."""
    memo = {id(session.events): session.events, id(session._subscribers): session._subscribers}
    if session.run:
        memo[id(session.run.playbook)] = session.run.playbook
    return copy.deepcopy(session.__dict__, memo)


def ticket_changed(before: dict, session) -> bool:
    """A new ticket, or a new line on the call's ticket (live call 31 Oct: the fridge joined the AC's ticket, the
    turn was undone as if nothing happened, and its replay read the whole ticket message again)."""
    lines = lambda t: (t or {}).get("summary", {}).get("items")
    return bool(session.ticket) and (not before["ticket"] or lines(session.ticket) != lines(before["ticket"])
                                     or len(session.earlier_tickets) != len(before["earlier_tickets"]))


def side_effects(before: dict, session) -> bool:
    """Did the turn do something that can't be taken back (a ticket, a safety alert, an ending)?"""
    return bool(ticket_changed(before, session) or (session.hazard and not before["hazard"])
                or (session.closed and not before["closed"]) or (session.end_requested and not before["end_requested"]))


class TurnKeeper:
    """One call's turns: answers the caller's words, and handles a previous reply they never heard."""

    def __init__(self, brain):
        self.brain = brain
        self.last_speech = ""      # our last reply
        self.last_words = ""       # the caller's words behind it
        self.checkpoint: dict | None = None   # session state from before that turn

    async def answer(self, words: str, heard: bool = True) -> str:
        """The reply to the caller's new words. `heard`: did they hear all of our last reply?"""
        brain, session = self.brain, self.brain.s
        if self.last_speech and self.checkpoint and not heard:
            before = self.checkpoint
            # Live calls 31 Oct: "मैं किसी इंसान से बात कर सकता हूँ।" … "सकता हूं।" — the tail cut off
            # "connect कर रही हूँ" and Bolna dropped the transfer that came with it.
            transferring = session.transfers_asked > before["transfers_asked"]
            if ((session.hazard and not before["hazard"]) or (session.end_requested and not before["end_requested"])
                    or transferring):
                # A safety alert, goodbye or transfer can't be undone: say it again so it's actually heard
                # (and ask for the transfer again).
                session.say("user", words)
                session.messages.append({"role": "user", "content": words})
                session.publish("interrupted", repeated=True)
                session.transfer_requested = session.transfer_requested or transferring
                return self.last_speech
            if side_effects(before, session):
                # A ticket can't be undone either, but its long message isn't read again (live call 30 Oct:
                # the caller talked over it and heard all of it twice): answer the new words, with a
                # one-line reminder of the ticket.
                recap = brain.ticket_recap() if ticket_changed(before, session) else ""
                session.publish("interrupted", recapped=bool(recap))
                self.checkpoint, self.last_words = checkpoint(session), words
                result = await brain.respond(words)
                self.last_speech = f"{recap} {result.speech}".strip()
                return self.last_speech
            session.__dict__.update(self.checkpoint)
            session.publish("interrupted", merged=self.last_words)
            logger.info(f"[{session.id}] reply not heard; answering the caller's pieces together")
            words = f"{self.last_words} {words}"
        self.checkpoint, self.last_words = checkpoint(session), words
        result = await brain.respond(words)
        self.last_speech = result.speech
        return result.speech


def note_transfer(session, result: str, tickets) -> None:
    """The phone platform reported how the transfer to customer care went: "connected" or "failed: <reason>".
    A failed one goes on the ticket, so the team calls the customer back (live call 31 Oct: the care number was
    busy, the caller heard nothing more and hung up)."""
    if not result or result == session.transfer_result:
        return
    session.transfer_result = result
    session.publish("transfer_result", result=result)
    logger.info(f"[{session.id}] transfer to customer care: {result}")
    if result != "connected" and session.ticket:
        tickets.add_note(session.ticket, f"Transfer to customer care failed ({result.split(': ', 1)[-1]}): "
                                         f"call the customer back.")
