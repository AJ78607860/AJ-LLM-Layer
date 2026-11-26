"""Read the question: what kind of task is it, and how hard? (no model call)

The Layer is a zero-router-LLM service: deciding where a question goes must
not itself cost a model call, add latency, or send content anywhere. This
module reads the request locally, in microseconds, and returns:

* `task_family` — the routing taxonomy family (coding, mathematics, writing,
  translation, extraction, research, ...). Evidence of which model is best is
  kept per family, so the right family means the right track record.
* `tier` — how capable a model the question needs: `fast`, `standard` or `smart`.
* `confidence` and `signals` — which rules fired (rule names only, never text).

Vocabulary follows AJ Assistant's existing analysers (core/router/analysis.py:
risk and precision words; universal_ai_gateway/classifier.py: task patterns),
generalised so any app (AJ, Calibre, ...) gets the same behaviour.

It is deliberately conservative: an ambiguous question becomes
`conversation`, and an unclear difficulty becomes `standard`. Labels an app
sends itself always win over this guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

VERSION = "aj.understand.v1"
_SCAN = 8000  # characters of the latest user message examined


def _w(*words: str) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.I)


# family -> list of (rule name, pattern, weight)
_FAMILY_RULES: dict[str, list[tuple[str, re.Pattern, float]]] = {
    "coding": [
        ("code_block", re.compile(r"```|^\s{4,}\S", re.M), 2.0),
        ("code_syntax", re.compile(r"\bdef \w+\(|\bfunction \w*\(|=>|\bclass \w+[:({]|#include|\bimport \w+|"
                                   r"\bconst \w+ =|\bSELECT\b.+\bFROM\b|</?\w+[^>]*>", re.I), 2.0),
        ("code_words", _w(r"python", r"javascript", r"typescript", r"java", r"rust", r"golang", r"c\+\+", r"sql",
                          r"regex", r"api", r"endpoint", r"function", r"bug", r"debug", r"stack ?trace", r"exception",
                          r"compile", r"refactor", r"unit tests?", r"repo(?:sitory)?", r"git", r"docker",
                          r"kubernetes", r"script", r"code", r"architecture", r"system design", r"saas", r"microservices?",
                          r"database", r"backend", r"frontend", r"server"), 1.0),
    ],
    "mathematics": [
        ("math_expr", re.compile(r"\d\s*[-+*/^×÷]\s*\d|\b\d+\s*%|∫|∑|√|\bx\^?2\b"), 1.5),
        ("math_words", _w(r"calculate", r"equation", r"solve for", r"derivative", r"integral", r"probability",
                          r"algebra", r"arithmetic", r"percentage", r"compound interest", r"prove that",
                          r"theorem", r"matrix", r"statistics?", r"mean|median|variance"), 1.5),
    ],
    "translation": [
        ("translate", _w(r"translate", r"translation", r"in (?:french|spanish|german|italian|urdu|hindi|arabic|"
                         r"chinese|japanese|korean|portuguese|turkish|dutch|polish)", r"how do (?:you|i) say"), 3.0),
    ],
    "extraction": [
        ("extract", _w(r"extract", r"pull out", r"list (?:all|every) the", r"find (?:all|every)", r"parse",
                       r"which (?:dates|names|amounts)", r"key details"), 2.0),
    ],
    "structured_data": [
        ("structured", _w(r"json", r"csv", r"yaml", r"table", r"schema", r"spreadsheet", r"columns?",
                          r"classify", r"categori[sz]e", r"label", r"sentiment"), 1.5),
    ],
    "writing": [
        ("write", _w(r"write", r"draft", r"rewrite", r"reword", r"proofread", r"edit", r"polish", r"essay",
                     r"email", r"letter", r"blog", r"post", r"caption", r"cover letter", r"speech", r"poem",
                     r"story", r"tone", r"summari[sz]e", r"summary", r"tl;?dr", r"shorten"), 1.5),
    ],
    "research": [
        ("research", _w(r"research", r"compare", r"comparison", r"pros and cons", r"latest", r"sources?",
                        r"evidence", r"what (?:is|are) the (?:best|top)", r"market", r"review of",
                        r"literature", r"overview of"), 1.2),
    ],
    "reasoning": [
        ("reasoning", _w(r"why", r"explain\w*", r"reason", r"logic", r"trade-?offs?", r"should i", r"decide",
                         r"evaluate", r"analy[sz]e", r"implications?", r"cause", r"argument"), 1.0),
        ("how_works", re.compile(r"\bhow (?:does|do|did|is|are) .{0,60}\bwork", re.I), 1.0),
    ],
    "planning": [
        ("plan", _w(r"plan", r"schedule", r"roadmap", r"steps to", r"strategy", r"timeline", r"organi[sz]e",
                    r"prioriti[sz]e", r"to-?do"), 1.2),
    ],
    "travel_planning": [
        ("travel", _w(r"itinerary", r"trip", r"flights?", r"hotel", r"holiday", r"vacation", r"travel",
                      r"things to do in", r"visit(?:ing)?"), 2.0),
    ],
    "financial_analysis": [
        ("finance", _w(r"portfolio", r"invest(?:ment|ing)?", r"stocks?", r"valuation", r"cash ?flow",
                       r"balance sheet", r"p&l", r"profit and loss", r"tax return", r"mortgage", r"pension",
                       r"dividend", r"roi", r"budget forecast", r"financial model"), 2.0),
    ],
    "document_understanding": [
        ("document", _w(r"(?:this|the|attached) (?:document|contract|pdf|report|paper|policy|agreement)",
                        r"clause", r"terms and conditions", r"section \d"), 2.0),
    ],
}

# Difficulty signals.
_DEPTH = _w(r"step by step", r"in depth", r"in detail", r"thorough(?:ly)?", r"comprehensive", r"rigorous",
            r"prove", r"derive", r"proof", r"architecture", r"design (?:a|the) system", r"trade-?offs?",
            r"edge cases?", r"optimi[sz]e", r"complex", r"multi-?step", r"root cause", r"concurren\w+",
            r"race condition", r"security (?:review|vulnerabilit\w+)", r"critically", r"nuanced")
# High-stakes topics: a wrong or shallow answer can hurt someone's health, money or legal position.
# Matching any of these forces the smart tier. Kept broad on purpose: a false alarm costs a few
# pence, a miss can cost far more (review of PR #23, 25 Nov).
_HIGH_RISK = re.compile(r"\b(?:" + "|".join([
    # health and emergencies
    r"symptoms?", r"diagnos\w*", r"dosage", r"dose", r"overdos\w*", r"medications?", r"medicines?",
    r"pills?", r"tablets?", r"prescri\w+", r"side effects?", r"treatment", r"chest pain", r"pain",
    r"numb\w*", r"bleed\w*", r"blood", r"breath(?:e|ing|less)?", r"faint\w*", r"dizz\w+", r"seizure",
    r"unconscious", r"stroke", r"heart attack", r"fever", r"temperature of", r"rash", r"allerg\w+",
    r"swell\w*", r"vomit\w*", r"injur\w+", r"burns?", r"poison\w*", r"pregnan\w+", r"baby", r"infant",
    r"newborn", r"toddler", r"suicid\w*", r"self[- ]harm", r"kill myself", r"hurt myself", r"emergency",
    r"a&e", r"999", r"911", r"ambulance", r"doctor", r"gp", r"hospital", r"antidepressants?",
    r"insulin", r"antibiotics?", r"paracetamol", r"ibuprofen", r"alcohol",
    # money
    r"mortgage", r"remortgage", r"loans?", r"borrow\w*", r"debts?", r"credit (?:card|score|rating)",
    r"overdraft", r"bankrupt\w*", r"insolven\w+", r"pension", r"invest\w*", r"savings", r"isa",
    r"tax(?:es)?", r"hmrc", r"irs", r"benefits", r"universal credit", r"interest rate",
    # legal and rights
    r"legal(?:ly)?", r"lawyer", r"solicitor", r"lawsuit", r"su(?:e|ed|ing)", r"court", r"tribunal",
    r"liabilit\w+", r"contract law", r"regulat\w+", r"compliance", r"evict\w*", r"tenan\w+",
    r"landlord", r"lease", r"my rights", r"employment rights", r"fired", r"dismiss\w*", r"redundan\w+",
    r"discriminat\w+", r"visa", r"immigration", r"deport\w*", r"asylum", r"police", r"arrest\w*",
    r"divorce", r"custody", r"will and testament", r"power of attorney",
]) + r")\b", re.I)
# Irreversible actions: deleting or destroying things, and moving or committing money. A
# wrong answer here can't be undone, so these force the smart tier too (review of #23, 25 Nov).
_IRREVERSIBLE = re.compile(r"\b(?:" + "|".join([
    r"delet\w*", r"eras\w*", r"wip(?:e|es|ed|ing)", r"purg\w*", r"destroy\w*", r"shred\w*",
    r"drop (?:the |a |my |all )?(?:\w+ )?(?:table|database|db|collection|schema)s?", r"truncate",
    r"rm -rf", r"format (?:the |my )?(?:disk|drive|phone|laptop)", r"factory reset", r"overwrit\w*",
    r"cancel\w*", r"terminat\w*", r"revok\w*", r"close (?:my |the |our )?(?:\w+ )?accounts?",
    r"unsubscribe", r"deactivat\w*",
    r"wir(?:e|es|ed|ing)", r"transfer\w*", r"send(?:ing)? (?:money|funds|a payment|payment|cash|£|\$|€)",
    r"pay(?:s|ing|ment|ments)?", r"refund\w*", r"withdraw\w*", r"sell(?:ing)?", r"buy(?:ing)? (?:shares|stocks?|crypto\w*|bitcoin)",
    r"trad(?:e|es|ing)(?!-?\s?offs?)", r"crypto\w*", r"bitcoin", r"bank (?:details|account|transfer)", r"password",
]) + r")\b", re.I)

_PRECISION = _w(r"exact(?:ly)?", r"precise(?:ly)?", r"how much", r"how many", r"percentage", r"figure",
                r"deadline", r"dosage", r"accurate(?:ly)?")
_SIMPLE = re.compile(r"^\W*(?:hi|hello|hey|thanks?|thank you|ok(?:ay)?|yes|no|sure|cheers|good (?:morning|"
                     r"night|evening))\b", re.I)
_LIST_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+\S", re.M)


@dataclass(frozen=True)
class Understanding:
    task_family: str
    tier: str
    confidence: float
    risk: str
    signals: tuple[str, ...]
    version: str = VERSION


def understand(messages, system: str | None = None, *, json_mode: bool = False) -> Understanding:
    """Classify a conversation. `messages` is a sequence of objects with .role/.content."""
    latest = next((m.content for m in reversed(messages) if m.role == "user"), "")
    text = latest[:_SCAN]
    total_chars = sum(len(m.content) for m in messages) + len(system or "")
    signals: list[str] = []

    scores: dict[str, float] = {}
    for family, rules in _FAMILY_RULES.items():
        score = 0.0
        for name, pattern, weight in rules:
            hits = len(pattern.findall(text))
            if hits:
                score += weight * min(hits, 3)
                signals.append(f"{family}:{name}")
        if score:
            scores[family] = score
    if json_mode:
        scores["structured_data"] = scores.get("structured_data", 0) + 2.0
        signals.append("json_mode")
    # Travel and finance are specialisations of planning / reasoning.
    if "travel_planning" in scores and "planning" in scores:
        scores["travel_planning"] += scores.pop("planning")

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    if not ranked or ranked[0][1] < 1.5 or _SIMPLE.match(text) and len(text) < 60:
        family, confidence = "conversation", 0.3 if ranked else 0.5
        signals.append("family:default_conversation")
    else:
        top = ranked[0][1]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        family = ranked[0][0]
        confidence = round(min(0.95, 0.5 + (top - second) / (2 * top)), 2)

    irreversible = bool(_IRREVERSIBLE.search(text))
    if irreversible:
        signals.append("risk:irreversible_action")
    risk = "high" if irreversible or _HIGH_RISK.search(text) or family == "financial_analysis" else "low"
    if risk == "high" and family == "conversation":
        family = "reasoning"  # health / legal questions need careful reasoning, not chit-chat
        signals.append("family:high_risk_reasoning")
    if risk == "high":
        signals.append("risk:high")
    depth = len(_DEPTH.findall(text))
    requirements = len(_LIST_ITEM.findall(text))
    code_chars = sum(len(block) for block in re.findall(r"```.*?```", text, re.S))
    approx_tokens = total_chars // 4

    if (risk == "high" or depth >= 2 or requirements >= 5
            or (family == "mathematics" and re.search(r"\b(?:prove|proof|derive)\b", text, re.I))
            or (family == "coding" and (code_chars > 3000 or depth >= 1))
            or approx_tokens > 30_000):
        tier = "smart"
        signals.append("tier:smart")
    elif (len(text) < 400 and depth == 0 and requirements < 3 and not _PRECISION.search(text)
          and (family in {"translation", "extraction", "structured_data", "writing"}
               or (family == "conversation" and _SIMPLE.match(text) and len(text) < 60))):
        tier = "fast"
        signals.append("tier:fast")
    else:
        tier = "standard"
        signals.append("tier:standard")
    if _PRECISION.search(text):
        signals.append("precision:high")
    return Understanding(task_family=family, tier=tier, confidence=confidence, risk=risk,
                         signals=tuple(dict.fromkeys(signals)))


def resolve(request):
    """Return (request with task_family/tier filled in, how they were decided)."""
    info = {"family_source": "app", "tier_source": "app"}
    if request.task_family != "auto" and request.tier != "auto":
        return request, info
    u = understand(request.messages, request.system, json_mode=request.json_mode)
    update = {}
    if request.task_family == "auto":
        update["task_family"] = u.task_family
        info["family_source"] = "auto"
    if request.tier == "auto":
        update["tier"] = u.tier
        info["tier_source"] = "auto"
    info.update(confidence=u.confidence, risk=u.risk, signals=list(u.signals), version=u.version)
    return request.model_copy(update=update), info
