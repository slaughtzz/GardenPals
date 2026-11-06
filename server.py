"""Garden Agent backend — pure Python, no installs needed.  Run:  python server.py
The agent loop (observe -> think -> act -> learn) lives here. The HTML page only displays it."""
import json, os, random, threading, time, queue, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

BASE = Path(__file__).parent
PORT = int(os.environ.get("PORT", 3000))
DATA = BASE / "data" / "state.json"

# ---- load .env (KEY=value per line) ----
try:
    for line in (BASE / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
except FileNotFoundError:
    pass
KEY = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_KEY") or ""
LLM_URL = os.environ.get("LLM_URL", "https://api.openai.com/v1/chat/completions") if KEY else ""
MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")

# Each plant: the months it grows well in, how much water it wants, how long it takes,
# its sun/soil needs, how tough it is, and what it earns you. Based on common garden guidance.
SP = {
 "tomato":    {"thirst": 16, "grow": 10, "val": 3, "months": [5, 6, 7, 8, 9],  "sun": "full", "soil": "rich, well-drained", "feed": 14, "hardy": 2, "spacing": "60 cm", "tip": "Water deeply at the base, feed every 2 weeks once flowering. Pinch out side shoots."},
 "lettuce":   {"thirst": 13, "grow": 6,  "val": 1, "months": [3, 4, 5, 9, 10], "sun": "part", "soil": "moist, compost-rich",  "feed": 20, "hardy": 4, "spacing": "25 cm", "tip": "Sow little and often. Bolts (runs to seed) in heat, so shade it in summer."},
 "basil":     {"thirst": 12, "grow": 7,  "val": 1, "months": [5, 6, 7, 8],     "sun": "full", "soil": "light, free-draining", "feed": 18, "hardy": 1, "spacing": "20 cm", "tip": "Pinch the tips often to keep it bushy. Frowns at cold nights."},
 "carrot":    {"thirst": 9,  "grow": 12, "val": 2, "months": [3, 4, 5, 6, 7, 8], "sun": "full", "soil": "deep, stone-free",  "feed": 30, "hardy": 5, "spacing": "8 cm",  "tip": "Thin the seedlings or you get small roots. Keep evenly moist or they split."},
 "cucumber":  {"thirst": 18, "grow": 11, "val": 3, "months": [5, 6, 7, 8],     "sun": "full", "soil": "rich, moist",        "feed": 12, "hardy": 1, "spacing": "90 cm", "tip": "Very thirsty. Train it up a frame so the fruit stays clean."},
 "pea":       {"thirst": 11, "grow": 9,  "val": 2, "months": [3, 4, 5, 6, 10, 11], "sun": "part", "soil": "fertile, limed",  "feed": 22, "hardy": 4, "spacing": "10 cm", "tip": "Climbing varieties need support. Pick pods young for a longer crop."},
 "sunflower": {"thirst": 12, "grow": 14, "val": 2, "months": [4, 5, 6, 7],     "sun": "full", "soil": "any, well-drained",  "feed": 26, "hardy": 3, "spacing": "45 cm", "tip": "Deep roots once established. Stake tall ones against wind."},
 "cactus":    {"thirst": 3,  "grow": 24, "val": 1, "months": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12], "sun": "full", "soil": "gritty, sharp-draining", "feed": 60, "hardy": 5, "spacing": "30 cm", "tip": "Water rarely and let the soil dry right out. Hates sitting wet."},
 "mint":      {"thirst": 15, "grow": 9,  "val": 1, "months": [4, 5, 6, 7, 8, 9], "sun": "part", "soil": "moist, any",       "feed": 16, "hardy": 5, "spacing": "40 cm", "tip": "Spreads hard — keep it in a pot or it takes the bed."},
}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
WX = [{"n": "Sunny", "dry": 1.4, "rain": 0}, {"n": "Cloudy", "dry": 1, "rain": 0},
      {"n": "Rainy", "dry": .3, "rain": 30}, {"n": "Hot", "dry": 2, "rain": 0}]

def season(day):
    """Days map to months: 30 days each, starting in April (a sensible sowing month)."""
    return MONTHS[(3 + (day - 1) // 30) % 12]
def in_season(t, day):
    return ((3 + (day - 1) // 30) % 12 + 1) in SP[t]["months"]
def season_list(t):
    return ", ".join(MONTHS[m - 1][:3] for m in SP[t]["months"])
LOCK = threading.RLock()

def fresh():
    return {"day": 1, "wx": WX[0], "fc": WX[1], "tank": 30, "spray": 4, "feed": 6, "harv": 0, "waste": 0, "lost": 0,
            "score": 0, "hist": [], "slots": [None] * 12, "tasks": [], "tid": 1, "pid": 1, "log": [],
            "mem": {"lessons": [], "skip": 0},
            "agent": {"phase": "idle", "briefing": "", "brain": "rules", "thought": ""},
            "settings": {"autonomy": "ask", "gSave": 40, "gYield": 50, "order": "", "speed": 6, "paused": False}}

S = fresh()
try:
    S.update(json.loads(DATA.read_text()))
except Exception:
    pass

def save():
    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(json.dumps(S))

# ---- realtime push (Server-Sent Events) ----
CLIENTS = set()
def snap():
    return {**S, "species": SP, "months": MONTHS, "month": season(S["day"]), "buddy": buddy_report({}),
            "sowNow": [t for t in SP if in_season(t, S["day"])], "llm": bool(LLM_URL), "model": MODEL}
def push():
    with LOCK:
        msg = "data:" + json.dumps(snap()) + "\n\n"
    for q in list(CLIENTS):
        q.put(msg)
def log(kind, msg):
    S["log"].insert(0, {"day": S["day"], "k": kind, "m": msg})
    del S["log"][120:]

# ---- tools: the only things the agent is allowed to do ----
def find(pid):
    return next((p for p in S["slots"] if p and p["id"] == pid and p["alive"]), None)
def new_plant(t):
    p = {"id": S["pid"], "type": t, "water": 70, "growth": 0, "health": 100, "alive": True, "pest": False, "dryDays": 0,
         "sown": S["day"], "lastFeed": S["day"], "thr": 40}
    S["pid"] += 1
    return p
def t_water(a):
    p = find(a.get("id"))
    if not p: return False, "no such plant"
    if S["tank"] < 1: return False, "tank empty"
    S["tank"] -= 1
    if p["water"] > 60: S["waste"] += 1
    p["water"] = min(100, p["water"] + 30)
    return True, f"watered #{p['id']} → {round(p['water'])}%"
def t_treat(a):
    p = find(a.get("id"))
    if not p or not p["pest"]: return False, "no pest"
    if S["spray"] < 1: return False, "no spray left"
    S["spray"] -= 1; p["pest"] = False
    return True, f"treated #{p['id']}"
def t_harvest(a):
    p = find(a.get("id"))
    if not p or p["growth"] < SP[p["type"]]["grow"]: return False, "not ripe"
    S["slots"][S["slots"].index(p)] = None
    S["harv"] += 1
    S["score"] += SP[p["type"]]["val"] * 5
    return True, f"harvested {p['type']}"
def t_remove(a):
    for i, p in enumerate(S["slots"]):
        if p and p["id"] == a.get("id"):
            S["slots"][i] = None
            return True, f"cleared bed {i+1}"
    return False, "already gone"
def t_refill(a): S["tank"] = 30; return True, "tank refilled"
def t_restock(a): S["spray"] = 4; return True, "spray restocked"
def t_sow(a):
    i, t = a.get("slot"), a.get("type")
    if t not in SP or not isinstance(i, int) or not 0 <= i < 12 or S["slots"][i] is not None:
        return False, "cannot sow there"
    if not in_season(t, S["day"]):
        return False, f"out of season: {t} grows in {season_list(t)} (now {season(S['day'])})"
    S["slots"][i] = new_plant(t)
    return True, f"planted {t} in bed {i+1}"
def t_feed(a):
    p = find(a.get("id"))
    if not p: return False, "no such plant"
    if S["feed"] < 1: return False, "no fertiliser left"
    p["lastFeed"] = S["day"]; S["feed"] -= 1
    p["health"] = min(100, p["health"] + 12)
    return True, f"fed #{p['id']} {p['type']}"
def t_shade(a):
    p = find(a.get("id"))
    if not p: return False, "no such plant"
    if S["wx"]["n"] != "Hot" or p["type"] == "cactus":
        return False, "no hot spell right now"
    p["shaded"] = True
    return True, f"shaded #{p['id']} from the heat"
def t_noop(a): return True, "skipped"
TOOLS = {"water": (0, t_water), "treat": (0, t_treat), "harvest": (1, t_harvest), "remove": (1, t_remove),
         "refill": (0, t_refill), "restock": (0, t_restock), "sow": (1, t_sow), "feed": (0, t_feed),
         "shade": (0, t_shade), "noop": (0, t_noop)}

def heat_stress(p):
    """A plant is heat-stressed only while a hot spell is actually running and the plant dislikes heat."""
    if S["wx"]["n"] != "Hot" or p["type"] == "cactus" or SP[p["type"]]["thirst"] >= 16:
        return False
    return p["water"] < 40 or p["health"] < 85

# ---- the agent ----

BUDDY_NAMES = {"thriving": "Thriving", "happy": "Content", "thirsty": "Thirsty", "hungry": "Hungry",
               "hot": "Too hot", "pest": "Under attack", "offseason": "Out of season", "dying": "Struggling"}
def plant_mood(p):
    """A plain-English read on one plant, and what a gardener would do about it."""
    if not p["alive"]:
        return {"key": "dead", "label": "Gone", "face": "🥀", "line": f"{p['type']} #{p['id']} died", "fix": SP[p["type"]]["tip"]}
    sp = SP[p["type"]]
    if p["pest"]:
        return {"key": "pest", "label": "Under attack", "face": "🐛",
                "line": f"Pests are on {p['type']} #{p['id']} and it's losing health fast",
                "fix": "Spray it today — the pest damage stacks up every day it's left.", "id": p["id"], "type": p["type"]}
    if heat_stress(p):
        return {"key": "hot", "label": "Too hot", "face": "🥵",
                "line": f"{p['type']} #{p['id']} is wilting in this hot spell",
                "fix": "Shade it and give it a drink — it dislikes this heat.", "id": p["id"], "type": p["type"]}
    if p["water"] < p["thr"]:
        return {"key": "thirsty", "label": "Thirsty", "face": "💧",
                "line": f"{p['type']} #{p['id']} is at {round(p['water'])}% water (it wants {p['thr']}%+)",
                "fix": "Water it at the base. " + sp["tip"], "id": p["id"], "type": p["type"]}
    if S["day"] - p["lastFeed"] >= sp["feed"]:
        return {"key": "hungry", "label": "Hungry", "face": "🍽️",
                "line": f"{p['type']} #{p['id']} hasn't been fed in {S['day'] - p['lastFeed']} days",
                "fix": f"It wants feeding roughly every {sp['feed']} days, or growth slows.", "id": p["id"], "type": p["type"]}
    if not in_season(p["type"], S["day"]):
        return {"key": "offseason", "label": "Out of season", "face": "😴",
                "line": f"{p['type']} #{p['id']} is out of season in {season(S['day'])}",
                "fix": f"Nothing's wrong — it just grows in {season_list(p['type'])}. It'll barely move until then.", "id": p["id"], "type": p["type"]}
    if p["growth"] >= sp["grow"]:
        return {"key": "ripe", "label": "Ready to pick", "face": "✨",
                "line": f"{p['type']} #{p['id']} is ripe and ready",
                "fix": "Harvest it while it's at its best. " + sp["tip"], "id": p["id"], "type": p["type"]}
    pct = int(100 * p["growth"] / sp["grow"])
    return {"key": "happy", "label": "Growing well", "face": "😊",
            "line": f"{p['type']} #{p['id']} is {pct}% grown and healthy",
            "fix": sp["tip"], "id": p["id"], "type": p["type"]}

def buddy_report(o):
    """Everything the buddy knows, in one place."""
    moods = [plant_mood(p) for p in S["slots"] if p]
    counts = {}
    for m in moods: counts[m["key"]] = counts.get(m["key"], 0) + 1
    if not moods:
        state, headline = "calm", "Nothing planted yet — pick a seed and I will take it from there."
    elif counts.get("dead") or counts.get("dying"):
        state, headline = "worried", "Something's gone wrong out there."
    elif counts.get("pest") or counts.get("hot"):
        state, headline = "alert", "I need to act today."
    elif counts.get("thirsty") or counts.get("hungry"):
        state, headline = "busy", "A few jobs to do, nothing serious."
    else:
        state, headline = "calm", "Everything looks after itself right now."
    best = next((p for p in S["slots"] if p and p["alive"] and p["growth"] >= SP[p["type"]]["grow"]), None)
    if best and state == "calm":
        headline = f"{best['type']} #{best['id']} is ready when you are."
    ok_plants = [p for p in S["slots"] if p and p["alive"]]
    return {"state": state, "headline": headline, "moods": moods, "counts": counts,
            "inSeason": [t for t in SP if in_season(t, S["day"])],
            "watch": [m for m in moods if m["key"] in ("pest", "hot", "dying", "dead")][:3],
            "sum": {
                "plants": len(ok_plants),
                "avgWater": round(sum(p["water"] for p in ok_plants) / len(ok_plants)) if ok_plants else 0,
                "avgHealth": round(sum(p["health"] for p in ok_plants) / len(ok_plants)) if ok_plants else 0,
                "ripe": counts.get("ripe", 0), "thirsty": counts.get("thirsty", 0),
                "hungry": counts.get("hungry", 0), "pests": counts.get("pest", 0),
                "emptyBeds": len([s for s in S["slots"] if not s]),
                "tank": S["tank"], "spray": S["spray"], "feed": S["feed"],
                "month": season(S["day"]), "weather": S["wx"]["n"], "tomorrow": S["fc"]["n"]}}

def say(mood_key, name):
    """What the buddy actually says, in its own voice."""
    LINES = {
        "pest":   [f"Oh no — pests on {name}. Say the word and I'll deal with it.", f"{name} is being eaten. That needs spraying today."],
        "hot":    [f"{name} is baking out there. I'd shade it and water it.", f"This heat is hurting {name}. Let me shade it."],
        "thirsty":[f"{name} is getting thirsty — it's down to {0}%.", f"I'd water {name} soon."],
        "hungry": [f"{name} is looking peckish. A feed would sort it.", f"{name} hasn't eaten in a while."],
        "offseason":[f"{name} is just asleep for the season. Nothing to do.", f"Nothing wrong with {name} — wrong month for it, that's all."],
        "ripe":   [f"{name} is ready to pick! Lovely.", f"{name} is at its peak — harvest it."],
        "happy":  [f"{name} is coming along nicely.", f"{name} looks content."],
        "dead":   [f"Sadly {name} is gone. I'll clear the bed when you like.", f"{name} didn't make it."],
    }
    import random as _r
    return _r.choice(LINES.get(mood_key, [f"{name} looks fine."]))

def perceive():
    al = [p for p in S["slots"] if p and p["alive"]]; st = S["settings"]
    return {"day": S["day"], "month": season(S["day"]), "weather": S["wx"]["n"], "forecast": S["fc"]["n"],
            "tank": S["tank"], "spray": S["spray"], "feed": S["feed"], "rainSoon": S["fc"]["rain"] > 0,
            "gardenNote": "Plants only thrive in their own months; a plant outside its season grows slowly and sulks.",
            "buddy": {k: v for k, v in buddy_report({}).items() if k in ("state", "headline", "watch")},
            "plants": [{"id": p["id"], "type": p["type"], "water": round(p["water"]), "health": p["health"],
                        "ageDays": S["day"] - p["sown"], "ripe": p["growth"] >= SP[p["type"]]["grow"],
                        "pest": p["pest"], "inSeason": in_season(p["type"], S["day"]),
                        "daysSinceFeed": S["day"] - p["lastFeed"], "needsFeedEvery": SP[p["type"]]["feed"],
                        "sun": SP[p["type"]]["sun"], "soil": SP[p["type"]]["soil"],
                        "heatStressed": heat_stress(p), "care": SP[p["type"]]["tip"]} for p in al],
            "dead": [p["id"] for p in S["slots"] if p and not p["alive"]],
            "emptyBeds": [i for i, p in enumerate(S["slots"]) if not p],
            "sowNow": [t for t in SP if in_season(t, S["day"])],
            "goals": {"save": st["gSave"], "yield": st["gYield"]}, "order": st["order"].lower()}

def rule_plan(o):
    T = []
    def add(tool, args, why, pri): T.append({"tool": tool, "args": args, "why": why, "pri": pri, "src": "rules"})
    for i in o["dead"]: add("remove", {"id": i}, f"#{i} died — free up the bed", 2)
    for p in o["plants"]:
        if p["pest"]: add("treat", {"id": p["id"]}, f"pests on #{p['id']} {p['type']}", 9)
        if p["ripe"] and not ("never harvest" in o["order"] and p["type"] in o["order"]):
            add("harvest", {"id": p["id"]}, f"#{p['id']} {p['type']} is ripe", 6)
        thr = 40 - round(o["goals"]["save"] / 10) + (10 if not p["inSeason"] else 0)
        if p["water"] < thr:
            if o["rainSoon"] and p["water"] > 20 and p["health"] > 60:
                S["mem"]["skip"] += 1
                add("noop", {}, f"skip #{p['id']}: rain tomorrow, {p['water']}% is enough", 1)
            else:
                add("water", {"id": p["id"]}, f"#{p['id']} {p['type']} is at {p['water']}% (my limit: {thr}%)", 10 if p["water"] < 20 else 7)
        if p["heatStressed"] and S["weather"] == "Hot" and not p.get("shaded"):
            add("shade", {"id": p["id"]}, f"#{p['id']} {p['type']} hates heat and needs shade today", 9)
            add("water", {"id": p["id"]}, f"#{p['id']} {p['type']} is heat-stressed and needs water", 9)
        if p["daysSinceFeed"] >= p["needsFeedEvery"] and p["health"] < 95:
            add("feed", {"id": p["id"]}, f"#{p['id']} {p['type']} last fed {p['daysSinceFeed']} days ago (needs feeding every {p['needsFeedEvery']})", 5)
        if not p["inSeason"]:
            add("noop", {}, f"#{p['id']} {p['type']} is out of season this month — expect slow growth", 1)
    if o["tank"] <= 6: add("refill", {}, f"water tank low ({o['tank']} L)", 8)
    if o["spray"] <= 1: add("restock", {}, "pest spray almost gone", 4)
    if o["feed"] <= 1: add("refill", {}, f"fertiliser almost gone ({o['feed']} left)", 3)
    if o["emptyBeds"] and o["day"] % 3 == 0 and o["sowNow"]:
        have = [p["type"] for p in o["plants"]]
        ripe = sorted(o["sowNow"], key=lambda t: -SP[t]["val"])[0] if o["goals"]["yield"] > 50 else next((k for k in o["sowNow"] if k not in have), o["sowNow"][0])
        add("sow", {"slot": o["emptyBeds"][0], "type": ripe},
            f"bed {o['emptyBeds'][0]+1} is empty and {ripe} is in season in {o['month']} ({SP[ripe]['sun']} sun, {SP[ripe]['soil']} soil)", 3)
    return sorted(T, key=lambda t: -t["pri"]), ""

def llm_plan(o):
    system = ("You are an autonomous, knowledgeable garden-keeping agent. Look at the garden state and decide what to do today. "
              "Every plant has a season, a sun and soil preference and a feeding interval; an out-of-season plant grows slowly, "
              "a heat-stressed plant needs shade, and a hungry one needs feed. Prefer actions that follow real gardening practice. "
              'Reply ONLY with JSON: {"summary":"one friendly sentence about your reasoning","tasks":[{"tool":"...","args":{...},"why":"short reason"}]}. '
              f"Tools: water{{id}}, treat{{id}}, harvest{{id}}, remove{{id}}, refill{{}}, restock{{}}, feed{{id}}, shade{{id}}, sow{{slot,type}} with type one of {list(SP)}. "
              "gSave/gYield are 0-100 user preferences (water saving, yield). Respect the user's standing order. Only use ids that exist.")
    body = json.dumps({"model": MODEL, "response_format": {"type": "json_object"},
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(o)}]}).encode()
    req = urllib.request.Request(LLM_URL, body, {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=40) as r:
        j = json.load(r)
    d = json.loads(j["choices"][0]["message"]["content"])
    tasks = [{"tool": t["tool"], "args": t.get("args") or {}, "why": str(t.get("why", "AI decision"))[:140], "pri": 5, "src": "ai"}
             for t in d.get("tasks", []) if isinstance(t, dict) and t.get("tool") in TOOLS]
    return tasks, str(d.get("summary", ""))[:300]

def make_plan(o):
    if LLM_URL:
        try:
            tasks, summary = llm_plan(o)
            log("think", f"AI ({MODEL}) proposed {len(tasks)} task(s)")
            return tasks, summary, "ai"
        except Exception as e:
            log("think", f"AI unavailable ({type(e).__name__}) — using built-in rules instead")
    tasks, summary = rule_plan(o)
    return tasks, summary, "rules"

def execute(t):
    ok, msg = TOOLS[t["tool"]][1](t["args"])
    t["status"] = "done" if ok else "failed"; t["result"] = msg
    log("act", f"{t['tool']} → {msg}")
    return ok

BUSY = threading.Lock()
def phase(p, ms=0.5):
    with LOCK: S["agent"]["phase"] = p
    push(); time.sleep(ms)

def agent_cycle():
    if not BUSY.acquire(blocking=False): return
    try:
        with LOCK:
            keep = [t for t in S["tasks"] if t["status"] == "approval"]
            S["tasks"] = keep + [t for t in S["tasks"] if t["status"] != "approval"][-8:]
        phase("observe")
        with LOCK:
            o = perceive()
            log("observe", f"{len(o['plants'])} plants · {sum(p['pest'] for p in o['plants'])} pests · {sum(p['ripe'] for p in o['plants'])} ripe · tomorrow: {o['forecast']}")
        phase("think", 0.3)
        tasks, summary, brain = make_plan(o)
        phase("act", 0.3)
        counts, failed, waiting = {}, 0, 0
        with LOCK:
            S["agent"]["brain"] = brain
            mode = S["settings"]["autonomy"]
            for x in tasks:
                if any(a["status"] == "approval" and a["tool"] == x["tool"] and a["args"] == x["args"] for a in S["tasks"]): continue
                t = {**x, "id": S["tid"], "status": "pending"}; S["tid"] += 1
                S["tasks"].append(t); log("think", t["why"])
                risky = TOOLS[t["tool"]][0] > 0 and t["tool"] != "noop"
                if (mode == "watch" and t["tool"] != "noop") or (mode == "ask" and risky):
                    t["status"] = "approval"; waiting += 1; continue
                if execute(t):
                    if t["tool"] != "noop": counts[t["tool"]] = counts.get(t["tool"], 0) + 1
                else: failed += 1
        phase("learn", 0.3)
        with LOCK:
            if S["tank"] < 8:
                log("learn", "tank ran low — refilling earlier next time"); t_refill({}); counts["refill"] = 1
            V = {"water": "watered", "treat": "treated", "harvest": "harvested", "remove": "cleared", "sow": "planted"}
            parts = [f"{V[k]} {n}" for k, n in counts.items() if k in V]
            if "refill" in counts: parts.append("refilled the tank")
            if "restock" in counts: parts.append("restocked spray")
            if "feed" in counts: parts.append("fed the plants")
            if "shade" in counts: parts.append("shaded a heat-stressed plant")
            V.update({"feed": "fed", "shade": "shaded"})
            b = ("Today I " + ", ".join(parts) + ".") if parts else "Everything looks fine, so I did nothing."
            if waiting: b += f" {waiting} task{'s' if waiting > 1 else ''} waiting for your approval."
            if failed: b += f" {failed} failed."
            S["agent"]["briefing"] = b
            S["agent"]["thought"] = summary
            log("reflect", b)
    except Exception as e:
        log("reflect", f"agent error: {e}")
    finally:
        with LOCK:
            S["agent"]["phase"] = "idle"; save()
        push(); BUSY.release()

# ---- world simulation ----
LAST = [time.time()]
def tick():
    with LOCK:
        S["day"] += 1; S["wx"] = S["fc"]; S["fc"] = random.choice(WX)
        for p in S["slots"]:
            if not p or not p["alive"]: continue
            sp = SP[p["type"]]
            p["shaded"] = False
            p["water"] = min(100, max(0, p["water"] - sp["thirst"] * S["wx"]["dry"] + S["wx"]["rain"]))
            if p["water"] < 15:
                p["health"] -= 15; p["dryDays"] += 1
                if p["dryDays"] == 2 and p["thr"] < 60:
                    p["thr"] += 5
                    m = f"{p['type']} #{p['id']} wilted → I now water it from {p['thr']}% instead of later"
                    log("learn", m); S["mem"]["lessons"].insert(0, m)
            else:
                p["dryDays"] = 0
                if p["health"] < 100: p["health"] += 5
            if p["pest"]: p["health"] -= 10
            elif random.random() < .06: p["pest"] = True; log("observe", f"pests appeared on #{p['id']} ({p['type']})")
            # growing: needs water, health, the right season and enough food
            if not in_season(p["type"], S["day"]):
                if random.random() < .25: log("observe", f"#{p['id']} {p['type']} is out of season ({season(S['day'])}) — barely growing")
            elif p["water"] > 20 and not p["pest"]:
                if p["health"] >= 80: p["growth"] += 1
                elif p["health"] >= 50: p["growth"] += 0.5
            if S["day"] - p["lastFeed"] >= sp["feed"]:
                p["health"] -= 3   # hunger: it has gone too long without feeding
            # a plant that dislikes heat suffers in a hot spell unless shaded
            if S["wx"]["n"] == "Hot" and not p["shaded"] and p["type"] != "cactus":
                p["health"] -= 12
                log("observe", f"#{p['id']} {p['type']} is wilting in the heat")
            if p["health"] <= 0:
                p["alive"] = False; S["lost"] += 1; S["score"] -= 10
                log("observe", f"#{p['id']} {p['type']} died — {sp['tip']}")
        S["score"] += sum(1 for p in S["slots"] if p and p["alive"])
        S["hist"] = (S["hist"] + [S["score"]])[-60:]
    LAST[0] = time.time(); push()
    threading.Thread(target=agent_cycle, daemon=True).start()

def clock():
    while True:
        time.sleep(0.5)
        if not S["settings"]["paused"] and time.time() - LAST[0] >= S["settings"]["speed"]:
            tick()

# ---- HTTP ----
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/events":
            self.send_response(200)
            for k, v in (("Content-Type", "text/event-stream"), ("Cache-Control", "no-cache"), ("Connection", "keep-alive")):
                self.send_header(k, v)
            self.end_headers()
            q = queue.Queue(); CLIENTS.add(q)
            try:
                with LOCK: first = "data:" + json.dumps(snap()) + "\n\n"
                self.wfile.write(first.encode()); self.wfile.flush()
                while True:
                    try: msg = q.get(timeout=15)
                    except queue.Empty: msg = ": ping\n\n"
                    self.wfile.write(msg.encode()); self.wfile.flush()
            except Exception:
                pass
            finally:
                CLIENTS.discard(q)
            return
        if path == "/api/state":
            with LOCK: return self._json(200, snap())
        if path == "/api/buddy":
            with LOCK:
                r = buddy_report({})
                if r["watch"]:
                    lead, nm = r["watch"][0], f"{r['watch'][0]['type']} #{r['watch'][0]['id']}"
                elif r["moods"]:
                    lead, nm = r["moods"][0], f"{r['moods'][0]['type']} #{r['moods'][0]['id']}"
                else:
                    lead, nm = {"key": "happy", "label": "quiet"}, "the garden"
                r["speech"] = say(lead["key"], nm)
                return self._json(200, r)
        if path == "/api/plants":
            with LOCK:
                return self._json(200, {"species": SP, "sowNow": [t for t in SP if in_season(t, S["day"])],
                                        "month": season(S["day"]), "day": S["day"]})
        pub = (BASE / "public").resolve()
        f = (pub / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
        if pub not in f.parents and f != pub or not f.is_file():
            self.send_response(404); self.end_headers(); return
        types = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}
        b = f.read_bytes()
        self.send_response(200); self.send_header("Content-Type", types.get(f.suffix, "application/octet-stream") + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        path = self.path.split("?")[0]
        try: b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except Exception: b = {}
        with LOCK:
            if path == "/api/plant":
                i = b.get("slot")
                if b.get("type") not in SP or not isinstance(i, int) or not 0 <= i < 12 or S["slots"][i]:
                    return self._json(400, {"error": "invalid"})
                if not in_season(b["type"], S["day"]):
                    return self._json(400, {"error": f"{b['type']} is out of season in {season(S['day'])} — it grows in {season_list(b['type'])}"})
                S["slots"][i] = new_plant(b["type"])
            elif path == "/api/water": t_water({"id": b.get("id")})
            elif path == "/api/feed": t_feed({"id": b.get("id")})
            elif path == "/api/shade": t_shade({"id": b.get("id")})
            elif path == "/api/tick":
                threading.Thread(target=tick, daemon=True).start(); return self._json(200, {"ok": 1})
            elif path == "/api/settings":
                s = S["settings"]
                if b.get("autonomy") in ("watch", "ask", "auto"): s["autonomy"] = b["autonomy"]
                for k in ("gSave", "gYield", "speed"):
                    if isinstance(b.get(k), (int, float)): s[k] = b[k]
                if isinstance(b.get("order"), str): s["order"] = b["order"][:200]
                if isinstance(b.get("paused"), bool): s["paused"] = b["paused"]
            elif path in ("/api/approve", "/api/reject"):
                t = next((t for t in S["tasks"] if t["id"] == b.get("id") and t["status"] == "approval"), None)
                if not t: return self._json(404, {"error": "no such task"})
                if path == "/api/approve": execute(t)
                else:
                    t["status"] = "rejected"; m = f"You rejected \"{t['tool']}\" ({t['why']})"
                    log("learn", m); S["mem"]["lessons"].insert(0, m)
            elif path == "/api/reset":
                keep = S["settings"]; S.clear(); S.update(fresh()); S["settings"] = keep
                for i, t in enumerate(("lettuce", "carrot", "pea")): S["slots"][i] = new_plant(t)
            else: return self._json(404, {"error": "unknown"})
            save()
        push(); self._json(200, {"ok": 1})

if __name__ == "__main__":
    with LOCK:
        if not any(S["slots"]):
            for i, t in enumerate(("lettuce", "carrot", "pea")): S["slots"][i] = new_plant(t)
        log("think", "Buddy online.")
    threading.Thread(target=clock, daemon=True).start()
    threading.Thread(target=agent_cycle, daemon=True).start()
    print(f"Garden Agent → http://localhost:{PORT}   (AI brain: {'ON — ' + MODEL if LLM_URL else 'OFF, using built-in rules'})")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
