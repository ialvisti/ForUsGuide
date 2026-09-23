"""Lexical reading of what a participant actually asked for.

These predicates are shared by ``ticket_orchestrator`` (which canonicalises the
extractor's inquiry list BEFORE anything is routed) and ``rag_engine`` (which
routes). Keeping one reading in one place is the point: TKT-911797 failed
because an LLM extractor split ``"I am trying to roll my Roth over to my new
401k. How do I go about getting my account number?"`` into two inquiries, and
the motive half was then answered with a full termination-distribution
procedure. A stated motive is not a request to perform a transaction; an
explicit request to perform one still is, and must survive intact.

Pure text in, booleans out: no I/O, no model calls, no heavy imports.
"""

from __future__ import annotations

import re
from typing import List, Optional

__all__ = [
    "sentences",
    "requests_own_account_identifier",
    "requests_identifier_retrieval",
    "requests_transaction_action",
    "mentions_transaction",
    "is_background_statement",
]

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.?!])\s+|\n+")

# Only a possessive that binds the identifier to the participant counts.
_POSSESSIVE = r"(?:my|our|their|his|her|the\s+participant'?s?|participant'?s)"

# Two grammatical positions for the binding modifier:
#   "my new employer's account number"        -> group 1
#   "the account number for my outside IRA"   -> group 2
# Group 2 is GREEDY on purpose: a lazy quantifier in front of a zero-width
# ``\b`` always captures nothing, which would make the destination guard below
# unreachable on that branch. It also uses ``\s*`` rather than ``\s+`` per word,
# because a modifier that ends the sentence ("...for my IRA?") has no trailing
# whitespace and would otherwise never be captured.
_OWN_ACCOUNT_IDENTIFIER_RE = re.compile(
    rf"\b{_POSSESSIVE}\s+((?:[\w'’&-]+\s+){{0,3}}?)account\s*(?:number|id|#)\b"
    rf"|\baccount\s*(?:number|id|#)\s+(?:for|on|of|in|at|with)\s+"
    rf"{_POSSESSIVE}\s+((?:[\w'’&-]+\s*){{0,3}})",
    re.IGNORECASE,
)
# The alternation above is leftmost-first, so "my account number for my new
# rollover IRA" matches branch 1 and its trailing destination clause is never
# inspected. This re-reads that clause after a branch-1 hit.
_TRAILING_DESTINATION_RE = re.compile(
    rf"^\s*(?:for|on|of|in|at|with|to)\s+{_POSSESSIVE}\s+((?:[\w'’&-]+\s*){{0,3}})",
    re.IGNORECASE,
)
# A destination modifier rebinds the identifier to the RECEIVING account at the
# other provider, which is a different fact with its own normal routing.
_RECEIVING_ACCOUNT_MODIFIER_RE = re.compile(
    r"\b(?:new|receiving|destination|other|another|outside|external|ira)\b",
    re.IGNORECASE,
)
# "How do I go about getting my account number?" is procedural wording about
# obtaining the identifier itself, not a request for a distribution procedure.
_IDENTIFIER_RETRIEVAL_RE = re.compile(
    r"\bhow\s+(?:do|can|would|should|to)\b[^.?!]{0,40}"
    r"\b(?:get|getting|obtain|obtaining|find|finding|look\s*up|locate|retrieve|request)\b"
    r"[^.?!]{0,60}\baccount\s*(?:number|id|#)\b",
    re.IGNORECASE,
)

_TRANSACTION_VERB = (
    r"(?:roll(?:s|ed|ing)?(?:\s*-?\s*over)?|rollover|rolling"
    r"|withdraw(?:s|n|ing|al|als)?|distribut(?:e|es|ed|ing|ion|ions)"
    r"|cash(?:\s*-?\s*out|ing\s*out|\s+it\s+out)?|transfer(?:s|red|ring)?"
    r"|payout|pay\s+out)"
)
_TRANSACTION_MENTION_RE = re.compile(rf"\b{_TRANSACTION_VERB}\b", re.IGNORECASE)

# A purpose clause states WHY, not WHAT is being requested.
_PURPOSE_CLAUSE_RE = re.compile(
    r"\b(?:so\s+(?:that\s+)?(?:i|we|he|she|they)\s+(?:can|could|may|might|will|would)"
    r"|in\s+order\s+(?:to|for)|so\s+as\s+to)\b[^,;.?!]*",
    re.IGNORECASE,
)
# An infinitive/benefactive adjunct immediately following the requested
# identifier is that request's motive ("...my account number TO TRANSFER it").
_PURPOSE_ADJUNCT_RE = re.compile(
    rf"\b(?:to|for)\s+(?:the\s+|a\s+|an\s+|my\s+|his\s+|her\s+|their\s+)?"
    rf"(?:\w+\s+){{0,1}}?{_TRANSACTION_VERB}\b",
    re.IGNORECASE,
)
# "...and wants to roll it over" is not an adjunct: the infinitive completes a
# request verb, so it states a second thing the participant is asking for.
_GOVERNED_INFINITIVE_RE = re.compile(
    r"\b(?:want|wants|wanted|need|needs|needed|like|likes|liked|wish|wishes|"
    r"intend|intends|plan|plans|planning|hope|hopes|hoping|look|looks|looking|"
    r"ready|request|requests|requesting|asking|ask|asks|able|going|how|help|"
    r"helps|guidance|assistance|process|steps|procedure)\s+\w*\s*$",
    re.IGNORECASE,
)

# An explicit request to PERFORM the transaction, or to be told how to.
# "I want to roll over" counts: a bare intent addressed to support is a real
# request (A6). "I am trying to roll over" does not: the conative frames an
# effort already under way as the background for the question that follows.
_TRANSACTION_ACTION_RES = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\b(?:please|kindly)\s+(?:\w+\s+){0,2}?"
    r"(?:process|submit|send|start|initiate|begin|complete|set\s*up|issue|release)\b",
    # A bare imperative addressed to support is a request too, but only when it
    # governs a transaction: "Process my rollover." Wording that merely names a
    # transaction keeps its motive reading.
    rf"^\s*(?:process|submit|start|initiate|begin|complete|issue|release)\s+"
    rf"(?:\w+\s+){{0,2}}?{_TRANSACTION_VERB}\b",
    rf"\bhow\s+(?:do|can|should|would|to)\b[^.?!]{{0,80}}?{_TRANSACTION_VERB}\b",
    rf"\bcan\s+(?:i|we|you|he|she|they)\b[^.?!]{{0,60}}?{_TRANSACTION_VERB}\b",
    rf"\b(?:what|which)\b[^.?!]{{0,80}}?"
    rf"\b(?:process|steps?|procedure|paperwork|forms?)\b[^.?!]{{0,60}}?{_TRANSACTION_VERB}\b",
    rf"{_TRANSACTION_VERB}\b[^.?!]{{0,60}}?\b(?:process|steps?|procedure|paperwork|forms?)\b",
    rf"\b(?:want|wants|wish|wishes|need|needs|intend|intends|would\s+like|ready|"
    rf"requesting|request|requests|plan|plans|planning|looking|hoping|like)\s+(?:to\s+)?"
    rf"(?:\w+\s+){{0,2}}?{_TRANSACTION_VERB}\b",
    rf"\b(?:help|guidance|assistance)\b[^.?!]{{0,40}}?{_TRANSACTION_VERB}\b",
))


def sentences(text: Optional[str]) -> List[str]:
    """Split on terminal punctuation only; clause commas stay inside."""
    return [part for part in _SENTENCE_SPLIT_RE.split(text or "") if part.strip()]


def _own_account_match(text: Optional[str]) -> Optional["re.Match[str]"]:
    value = text or ""
    for match in _OWN_ACCOUNT_IDENTIFIER_RE.finditer(value):
        modifiers = match.group(1) or match.group(2) or ""
        if _RECEIVING_ACCOUNT_MODIFIER_RE.search(modifiers):
            continue
        if match.group(1) is not None:
            trailing = _TRAILING_DESTINATION_RE.match(value[match.end():])
            if trailing and _RECEIVING_ACCOUNT_MODIFIER_RE.search(trailing.group(1) or ""):
                continue
        return match
    return None


def requests_own_account_identifier(text: Optional[str]) -> bool:
    """True when the text asks for the participant's OWN account number."""
    return _own_account_match(text) is not None


def requests_identifier_retrieval(text: Optional[str]) -> bool:
    """True for 'how do I get my account number' — procedure about the ID."""
    return bool(_IDENTIFIER_RETRIEVAL_RE.search(text or ""))


def mentions_transaction(text: Optional[str]) -> bool:
    """True when the text names a money-movement transaction at all."""
    return bool(_TRANSACTION_MENTION_RE.search(text or ""))


# A coordinated clause that opens with a request subject ("and please send...",
# "and I also need...") ends the purpose adjunct. Punctuation is not required:
# "...to roll this over and please send me the forms" carries a real second
# request that must not be swallowed with the motive. A coordinator followed by
# anything else ("...and is seeking guidance") stays inside the adjunct.
# The extractor is REQUIRED to paraphrase in the third person
# (agent_prompts/extract_inquiries.md), so the coordinator that opens a second
# real request usually reads "and wants to ...", "and is requesting ..." rather
# than "and I need ...". Recognising only the first-person/imperative form let
# the adjunct run to the end of the sentence and swallow that request. Only a
# REQUEST verb closes the adjunct, so "...and is seeking guidance on the
# process" still stays inside it.
_REQUEST_VERB = (
    r"(?:want(?:s|ed)?|need(?:s|ed)?|wish(?:es|ed)?|would\s+like|intend(?:s|ed)?"
    r"|plan(?:s|ned|ning)?|hop(?:e|es|ing)|request(?:s|ed|ing)?|ask(?:s|ed|ing)?"
    r"|look(?:s|ing)?|ready)"
)
_ADJUNCT_BOUNDARY_RE = re.compile(
    r"[,;]"
    r"|\b(?:and|also|plus|but|then)\s+"
    r"(?:please|kindly|can|could|would|will|i|we|you)\b"
    rf"|\b(?:and|also|plus|but|then)\s+(?:the\s+)?"
    rf"(?:participant\s+|they\s+|he\s+|she\s+)?(?:is\s+|has\s+|also\s+)?"
    rf"{_REQUEST_VERB}\b",
    re.IGNORECASE,
)


def _strip_motive(sentence: str) -> str:
    """Remove purpose wording so only genuine requests are left to match.

    The adjunct is cut only up to the next clause boundary, so a real second
    request in the same sentence ("..., and please send me the forms", or the
    same thing without the comma) is never swallowed with it.
    """
    text = _PURPOSE_CLAUSE_RE.sub(" ", sentence)
    match = _own_account_match(text)
    if match is None:
        return text
    adjunct = None
    for candidate in _PURPOSE_ADJUNCT_RE.finditer(text, match.end()):
        if _GOVERNED_INFINITIVE_RE.search(text[:candidate.start()]):
            continue
        adjunct = candidate
        break
    if adjunct is None:
        return text
    boundary = _ADJUNCT_BOUNDARY_RE.search(text, adjunct.end())
    end = len(text) if boundary is None else boundary.start()
    return text[:adjunct.start()] + " " + text[end:]


def requests_transaction_action(text: Optional[str]) -> bool:
    """True when the participant asks for a transaction to happen, or how.

    False for a motive ("...so I can roll it over") and for a conative
    background statement ("I am trying to roll my Roth over") that only
    explains the situation behind another question.
    """
    for sentence in sentences(text):
        stripped = _strip_motive(sentence)
        if any(pattern.search(stripped) for pattern in _TRANSACTION_ACTION_RES):
            return True
    return False


# The same third-person requirement means a QUESTION reaches this module as
# reported speech, with no question mark and often no wh-word: "Participant
# wants to know if their match is vested", "Participant is asking whether ...".
# That is a request for information, not context, and must not be folded away
# as a motive nor dropped from the routing decision.
_REPORTED_QUESTION_RE = re.compile(
    r"\b(?:want(?:s|ed)?|need(?:s|ed)?|would\s+like|wish(?:es|ed)?)\s+(?:to\s+)?"
    r"(?:know|find\s+out|understand|confirm|verify|check)\b"
    r"|\b(?:ask(?:s|ed|ing)?|inquir(?:e|es|ed|ing|y|ies)|wonder(?:s|ed|ing)?"
    r"|unsure|unclear|uncertain)\b"
    r"|\b(?:seeking|requesting|requests?)\s+(?:\w+\s+){0,2}?"
    r"(?:clarification|confirmation|information|details|guidance|an\s+answer)\b",
    re.IGNORECASE,
)


def is_background_statement(text: Optional[str]) -> bool:
    """True for wording that neither asks anything nor requests anything.

    Such a quote is context. It cannot satisfy a routing predicate, and it
    also must not veto one — but the caller is expected to keep the case under
    human review rather than treat the statement as handled.
    """
    value = (text or "").strip()
    if not value or "?" in value:
        return False
    if requests_own_account_identifier(value) or requests_transaction_action(value):
        return False
    if _REPORTED_QUESTION_RE.search(value):
        return False
    return not re.search(
        r"\b(?:please|what|which|when|where|who|whom|whose|why|how|can you|could you|"
        r"would you|tell me|let me know|send me|confirm|provide)\b",
        value, re.IGNORECASE,
    )
