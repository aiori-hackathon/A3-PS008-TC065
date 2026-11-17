"""Plain language -> intent JSON. The result is only a proposal: the operator sees it, and it then goes
through the same schema, semantic checks, twin and human approval as a form. Nothing here touches a router.

1. Offline grammar (instant, deterministic, no network, no credits): English and a few Hinglish verbs.
     "connect R1 to R4" · "R1 should reach R5's LAN" · "make R4 reachable from everyone"
     "add route to 10.10.9.0/24 on R1 via R3" · "remove route 10.10.30.128/25 from R1" · "R2 se R5 jodo"
2. Only if the grammar does not match and an LLM is configured: an OpenAI-compatible chat/completions call
   (any provider or aggregator) translates the sentence, in any language, into those same short English
   commands, which the grammar then parses. Answers are cached per sentence and lab, and calls are capped per day.
   Settings (environment or .env): NMA_LLM_BASE_URL, NMA_LLM_API_KEY, NMA_LLM_MODEL, NMA_LLM_DAILY_LIMIT.
"""
import hashlib
import json
import os
import re
from datetime import date

import httpx
from pydantic import ValidationError

from backend import schema
from backend.topology import ROOT

PREFIX = r"\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}"
IP = r"\d{1,3}(?:\.\d{1,3}){3}"
REMOVE_WORDS = r"\b(remove|delete|drop|withdraw|undo|hatao|hata do|hatado|nikalo)\b"
ADD_WORDS = r"\b(add|create|install|configure|put|set up|setup|send|route|forward|daalo|lagao|bhejo)\b"
CONNECT_WORDS = r"\b(connect|link|reach|reachable|talk|ping|access|pahunch\w*|jod\w*|milao)\b"
ALL_WORDS = r"\b(everyone|everybody|every router|all routers|all|sab|sabhi|sabse)\b"


def _env(key):
    if key in os.environ:
        return os.environ[key]
    try:
        for line in (ROOT / ".env").read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _reason(text):
    clean = re.sub(r"[^A-Za-z0-9 _.,:/()'?!-]", " ", text)
    clean = re.sub(r"\s+", " ", clean).strip()[:200]
    return clean or "requested in plain language"


def grammar(text, topo):
    """Returns a list of intent dicts (without requested_by), or [] if the sentence does not match."""
    t = " " + text.strip() + " "
    names = sorted(topo.names, key=len, reverse=True)
    rx = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(map(re.escape, names)) + r")(?![A-Za-z0-9])", re.I)
    canon = {n.lower(): n for n in topo.names}
    found = [(m.start(), canon[m.group(1).lower()], m) for m in rx.finditer(t)]

    def lan_of(router):
        return str(topo.lans(router)[0].network)

    def lan_ref():
        m = re.search(rf"(?<![A-Za-z0-9])({'|'.join(map(re.escape, names))})(?:'s|s)?\s+(?:lan|network|subnet)", t, re.I) \
            or re.search(rf"(?:lan|network|subnet)\s+(?:of|behind|on|at)\s+({'|'.join(map(re.escape, names))})(?![A-Za-z0-9])", t, re.I)
        return (canon[m.group(1).lower()], m) if m else (None, None)

    prefix = re.search(PREFIX, t)
    reason = _reason(text)

    if re.search(REMOVE_WORDS, t, re.I):
        router_m = re.search(rf"\b(?:on|from|at|par|se)\s+({'|'.join(map(re.escape, names))})(?![A-Za-z0-9])", t, re.I)
        lan_router, _ = lan_ref()
        dest = prefix.group(0) if prefix else (lan_of(lan_router) if lan_router else None)
        router = canon[router_m.group(1).lower()] if router_m else next((r for _, r, _ in found if r != lan_router), None)
        if dest and router:
            return [{"intent": "REMOVE_ROUTE", "router": router, "destination": dest, "reason": reason}]
        return []

    via = re.search(rf"\b(?:via|through|thru|next[- ]?hop|using|se hokar|ke through)\s+({IP}|{'|'.join(map(re.escape, names))})(?![A-Za-z0-9])", t, re.I)
    if via and (prefix or re.search(ADD_WORDS, t, re.I)):
        lan_router, lan_m = lan_ref()
        dest = prefix.group(0) if prefix else (lan_of(lan_router) if lan_router else None)
        on = re.search(rf"\b(?:on|at|from|par)\s+({'|'.join(map(re.escape, names))})(?![A-Za-z0-9])", t, re.I)
        nh = via.group(1)
        router = canon[on.group(1).lower()] if on else next(
            (r for pos, r, m in found if m.start() != via.start(1) and (not lan_m or m.start() != lan_m.start(1))), None)
        if dest and router:
            return [{"intent": "ADD_ROUTE", "router": router, "destination": dest,
                     "next_hop": canon.get(nh.lower(), nh), "reason": reason}]

    if re.search(CONNECT_WORDS, t, re.I) or re.search(r"\b(to|tak|se)\b", t, re.I):
        one_way = bool(re.search(r"\b(one[- ]way|one direction|only from)\b", t, re.I))
        routers = list(dict.fromkeys(r for _, r, _ in found))
        target = prefix.group(0) if prefix else None
        everyone = re.search(ALL_WORDS, t, re.I)
        if everyone and (routers or target):
            dest = target or routers[0]
            return [{"intent": "CONNECT", "source": s, "destination": dest, "both_ways": not one_way, "reason": reason}
                    for s in topo.names if s != dest and (target is None or not topo.is_connected(s, target))]
        # "make R4 reachable from R1": destination first, source after "from"
        frm = re.search(rf"\bfrom\s+({'|'.join(map(re.escape, names))})(?![A-Za-z0-9])", t, re.I)
        if frm and len(routers) >= 2 and re.search(r"\breachable\b", t, re.I):
            src = canon[frm.group(1).lower()]
            dest = target or next(r for r in routers if r != src)
            return [{"intent": "CONNECT", "source": src, "destination": dest, "both_ways": not one_way, "reason": reason}]
        if target and routers:
            return [{"intent": "CONNECT", "source": routers[0], "destination": target, "both_ways": not one_way, "reason": reason}]
        if len(routers) >= 2:
            return [{"intent": "CONNECT", "source": routers[0], "destination": routers[1], "both_ways": not one_way, "reason": reason}]
    return []


# The language model only translates: any language -> the short English command forms below, which the
# grammar above then parses. It never sees the schema or the lab's addresses and never produces intents
# itself, so a request costs about 100-150 tokens, and the same request on the same lab is free (cache).
TRANSLATE = """Rewrite the user's network request as English commands, one per line, using only these forms:
connect A to B
connect A to B one way
make A reachable from everyone
add route to P on A via B
remove route P from A
A and B are router names from: {routers}. P is a prefix like 192.0.2.0/24, or A's LAN.
Reply with the commands only, or NONE if the request does not fit these forms."""
CACHE = ROOT / "data" / "llm_cache.json"
DEFAULT_DAILY_LIMIT = 100


def _cache():
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def _today(cache):
    days = cache.setdefault("days", {})
    for old in sorted(days)[:-30]:  # keep a month of usage counts
        days.pop(old)
    return days.setdefault(date.today().isoformat(), {"calls": 0, "tokens": 0})


def usage():
    """Today's language-model calls and tokens (cache hits cost nothing and are not counted)."""
    return dict(_today(_cache()))


def translate(text, topo, client=None):
    """Returns (lines, info). lines is None when no language model is configured."""
    base, key, model = _env("NMA_LLM_BASE_URL").rstrip("/"), _env("NMA_LLM_API_KEY"), _env("NMA_LLM_MODEL")
    if not (base and key and model):
        return None, {"note": "no language model is configured (set NMA_LLM_BASE_URL, NMA_LLM_API_KEY, NMA_LLM_MODEL in .env)"}
    cache = _cache()
    today = _today(cache)
    k = hashlib.sha256(f"{model}|{','.join(topo.names)}|{' '.join(text.lower().split())}".encode()).hexdigest()[:24]
    answers = cache.setdefault("answers", {})
    if k in answers:
        return [l for l in answers[k].splitlines() if l], {"cached": True, "tokens": 0, "today": today}
    limit = int(_env("NMA_LLM_DAILY_LIMIT") or DEFAULT_DAILY_LIMIT)
    if today["calls"] >= limit:
        return [], {"note": f"today's limit of {limit} language-model calls is used up (NMA_LLM_DAILY_LIMIT in .env)"}
    req = {"model": model, "temperature": 0, "max_tokens": 60,
           "messages": [{"role": "system", "content": TRANSLATE.format(routers=", ".join(topo.names))},
                        {"role": "user", "content": text}]}
    try:
        r = (client or httpx.Client(timeout=15)).post(f"{base}/chat/completions", json=req,
                                                     headers={"Authorization": f"Bearer {key}"})
        r.raise_for_status()
        data = r.json()
        out = data["choices"][0]["message"]["content"] or ""
    except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as e:
        return [], {"note": f"the language model did not answer ({type(e).__name__}: {str(e)[:120]})"}
    tokens = int((data.get("usage") or {}).get("total_tokens") or 0)
    today["calls"] += 1
    today["tokens"] += tokens
    lines = [l.strip(" -*`.\t") for l in out.strip().splitlines() if l.strip()][:10]
    lines = [] if [l.upper() for l in lines] == ["NONE"] else lines
    answers[k] = "\n".join(lines)  # NONE is cached too, so the same nonsense is never paid for twice
    for old in list(answers)[:-500]:
        answers.pop(old)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache))  # ponytail: last writer wins, fine for one dashboard
    return lines, {"cached": False, "tokens": tokens, "today": today}


def understand(text, topo, client=None):
    """{"intents": [...], "how": "...", "note": "...", "usage": {...}}. Every intent is checked against the
    schema here; anything that does not validate is dropped and explained in the note."""
    text = (text or "").strip()[:300]
    if not text:
        return {"intents": [], "how": "", "note": "Type what you want the network to do."}
    intents, how, note, info = grammar(text, topo), "the built-in grammar (offline, no credits used)", "", {}
    a, z = topo.names[0], topo.names[-1]
    try_this = f"Try: 'connect {a} to {z}', 'add route to 192.0.2.0/24 on {a} via {next(iter(topo.neighbours(a).values()), z)}', 'remove route 192.0.2.0/24 from {a}'."
    if not intents:
        # every allowed change names a router or a network, so a sentence with neither is not sent to the model (no credits)
        if not re.search(PREFIX, text) and not any(re.search(rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])", text, re.I) for n in topo.names):
            return {"intents": [], "how": "", "note": f"That does not name a router or a network, so there is nothing to change. {try_this}"}
        lines, info = translate(text, topo, client)
        if lines is None:
            return {"intents": [], "how": "", "note": f"Not understood offline. {try_this} " + info["note"] + "."}
        intents = [i for line in lines for i in grammar(line, topo)]
        if lines:
            cost = "from the cache, no credits used" if info.get("cached") else (
                f"{info['tokens']} tokens; today {info['today']['calls']} calls, {info['today']['tokens']} tokens")
            how = f"{_env('NMA_LLM_MODEL')}, read as “{' / '.join(lines)}” ({cost})"
        note = info.get("note") or ("" if lines else f"That is not a change this agent can make. {try_this}")
    valid, problems = [], []
    for i in intents:
        i = {**i, "requested_by": "check", "reason": _reason(str(i.get("reason") or text))}
        try:
            schema.parse(i, topo)
            i.pop("requested_by")
            valid.append(i)
        except ValidationError as e:
            problems.append(schema.errors(e))
    if problems:
        note = (note + " Dropped: " + "; ".join(problems)).strip()
    if not valid and not note:
        note = "Nothing in that request maps to an allowed change."
    return {"intents": valid, "how": how if valid else "", "note": note,
            "usage": {"tokens": info.get("tokens", 0), "cached": bool(info.get("cached")), "today": usage()}}
