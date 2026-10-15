#!/usr/bin/env python3
"""
Сборщик статистики клана Clash of Clans.

Запускается по расписанию (GitHub Actions), ходит в официальный API Supercell
(через прокси RoyaleAPI) и складывает данные в папку data/.

Главный принцип: вся статистика хранится по ТЕГУ игрока (#ABC123), а не по
нику и не по факту членства в клане. Поэтому если игрок вышел и вернулся,
его войны, ЛВК и рейды никуда не пропадают, а в истории появляется отметка
«вышел / вернулся».

Только стандартная библиотека Python, ничего ставить не нужно.

Переменные окружения:
  COC_TOKEN   — ключ API с developer.clashofclans.com (обязательно)
  CLAN_TAG    — тег основного клана (по умолчанию #280L809U9); остальные кланы
                берутся из data/clans.json
  API_BASE    — адрес API, по умолчанию прокси RoyaleAPI
  MOCK_DIR    — (для тестов) папка с заготовленными ответами API вместо сети
  NOW         — (для тестов) подменить текущее время, ISO-формат
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_TAG = os.environ.get("CLAN_TAG", "#280L809U9").strip().upper()
if not DEFAULT_TAG.startswith("#"):
    DEFAULT_TAG = "#" + DEFAULT_TAG
CLAN_TAG = DEFAULT_TAG  # текущий клан, меняется в collect_clan()
API_BASE = os.environ.get("API_BASE", "https://cocproxy.royaleapi.dev/v1").rstrip("/")
TOKEN = os.environ.get("COC_TOKEN", "").strip()
MOCK_DIR = os.environ.get("MOCK_DIR")
ROOT_DATA = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DATA_DIR = ROOT_DATA  # папка текущего клана, меняется в collect_clan()

NOW = os.environ.get("NOW") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
errors = []


# ───────────────────────── утилиты ─────────────────────────

def log(*a):
    print(*a, file=sys.stderr, flush=True)


class ApiError(Exception):
    def __init__(self, status, reason, path):
        super().__init__(f"{status} {reason} ({path})")
        self.status = status
        self.reason = reason


def api(path):
    """GET к API. path вида '/clans/#TAG/currentwar'. Возвращает dict."""
    if MOCK_DIR:
        name = path.strip("/").replace("/", "__").replace("#", "").replace("?", "__")
        f = Path(MOCK_DIR) / f"{name}.json"
        if not f.exists():
            raise ApiError(404, "notFound", path)
        data = json.loads(f.read_text("utf-8"))
        if isinstance(data, dict) and data.get("__status"):
            raise ApiError(data["__status"], data.get("reason", ""), path)
        return data

    url = API_BASE + urllib.parse.quote(path, safe="/?=&")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
        "User-Agent": "clan-stats-site/1.0",
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                reason = json.loads(body).get("reason", body[:200])
            except Exception:
                reason = body[:200]
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            raise ApiError(e.code, reason, path)
        except urllib.error.URLError as e:
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            raise ApiError(0, str(e.reason), path)


def load(name, default):
    f = DATA_DIR / name
    if f.exists():
        try:
            return json.loads(f.read_text("utf-8"))
        except Exception as e:
            log(f"!! не удалось прочитать {name}: {e}")
    return default


def save(name, obj):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_DIR / (name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=False), "utf-8")
    tmp.replace(DATA_DIR / name)


def iso(t):
    """'20261015T120000.000Z' -> '2026-10-15T12:00:00Z'"""
    if not t:
        return None
    t = t.split(".")[0].rstrip("Z")
    try:
        return datetime.strptime(t, "%Y%m%dT%H%M%S").strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return t


def badge(b):
    b = b or {}
    return b.get("medium") or b.get("small") or b.get("large")


# ───────────────────────── игроки и членство ─────────────────────────

def ensure_player(players, tag, name=None, th=None):
    """Заводит карточку игрока, если её ещё нет (например, игрок был в войне,
    но ушёл из клана до начала отслеживания)."""
    p = players.get(tag)
    if not p:
        p = players[tag] = {
            "tag": tag, "name": name or tag, "names": [name] if name else [],
            "th": th, "inClan": False, "role": None,
            "firstSeen": NOW, "events": [],
        }
    if name and name != p.get("name"):
        if p.get("name") and p["name"] not in p.setdefault("names", []):
            p["names"].append(p["name"])
        p["name"] = name
        if name not in p["names"]:
            p["names"].append(name)
    if th and (not p.get("th") or th > p["th"]):
        p["th"] = th
    return p


def update_members(players, clan, first_run):
    current = {m["tag"]: m for m in clan.get("memberList", [])}

    for tag, m in current.items():
        existed = tag in players
        p = ensure_player(players, tag, m.get("name"), m.get("townHallLevel"))
        if not existed or not p.get("inClan"):
            kind = "seen" if (first_run and not existed) else "join"
            p.setdefault("events", []).append({"t": kind, "at": NOW})
            if not existed:
                p["firstSeen"] = NOW
        p.update({
            "inClan": True,
            "role": m.get("role"),
            "th": m.get("townHallLevel") or p.get("th"),
            "exp": m.get("expLevel"),
            "trophies": m.get("trophies"),
            "league": (m.get("leagueTier") or m.get("league") or {}).get("name"),
            "leagueIcon": badge((m.get("leagueTier") or m.get("league") or {}).get("iconUrls")),
            "donations": m.get("donations"),
            "received": m.get("donationsReceived"),
            "rank": m.get("clanRank"),
            "lastSeen": NOW,
        })

    for tag, p in players.items():
        if p.get("inClan") and tag not in current:
            p["inClan"] = False
            p["role"] = None
            p.setdefault("events", []).append({"t": "leave", "at": NOW})


# ───────────────────────── войны (КВ и ЛВК) ─────────────────────────

def war_result(us, them):
    if us["stars"] != them["stars"]:
        return "win" if us["stars"] > them["stars"] else "lose"
    if abs(us["pct"] - them["pct"]) > 1e-9:
        return "win" if us["pct"] > them["pct"] else "lose"
    return "tie"


def normalize_war(raw, kind, players, extra=None):
    state = raw.get("state")
    if not state or state == "notInWar":
        return None
    a, b = raw.get("clan", {}), raw.get("opponent", {})
    if a.get("tag") == CLAN_TAG:
        us, them = a, b
    elif b.get("tag") == CLAN_TAG:
        us, them = b, a
    else:
        return None

    if (raw.get("warType") or "").lower() == "friendly":
        kind = "friendly"

    apm = raw.get("attacksPerMember") or (1 if kind == "cwl" else 2)
    prep = iso(raw.get("preparationStartTime"))

    def ranked(side):
        ms = sorted(side.get("members", []), key=lambda m: m.get("mapPosition", 99))
        return [(i + 1, m) for i, m in enumerate(ms)]

    opp = {m["tag"]: (pos, m) for pos, m in ranked(them)}

    # «новые» звёзды: сколько звёзд атака реально добавила к счёту войны
    all_attacks = []
    for m in us.get("members", []):
        for at in m.get("attacks", []) or []:
            all_attacks.append(at)
    best = {}
    fresh = {}
    for at in sorted(all_attacks, key=lambda x: x.get("order", 0)):
        d = at["defenderTag"]
        prev = best.get(d, 0)
        fresh[(at["attackerTag"], at.get("order"))] = max(0, at["stars"] - prev)
        best[d] = max(prev, at["stars"])

    members = []
    for pos, m in ranked(us):
        ensure_player(players, m["tag"], m.get("name"), m.get("townhallLevel"))
        atts = []
        for at in sorted(m.get("attacks", []) or [], key=lambda x: x.get("order", 0)):
            dpos, dm = opp.get(at["defenderTag"], (None, {}))
            atts.append({
                "d": at["defenderTag"], "dn": dm.get("name"), "dpos": dpos,
                "dth": dm.get("townhallLevel"), "s": at.get("stars", 0),
                "p": at.get("destructionPercentage", 0), "o": at.get("order"),
                "n": fresh.get((m["tag"], at.get("order")), 0),
                "dur": at.get("duration"),
            })
        boa = m.get("bestOpponentAttack")
        members.append({
            "tag": m["tag"], "name": m.get("name"), "th": m.get("townhallLevel"),
            "pos": pos, "attacks": atts,
            "def": {"n": m.get("opponentAttacks", 0),
                    "s": boa.get("stars") if boa else None,
                    "p": boa.get("destructionPercentage") if boa else None},
        })

    def side(s):
        return {"tag": s.get("tag"), "name": s.get("name"), "badge": badge(s.get("badgeUrls")),
                "level": s.get("clanLevel"), "stars": s.get("stars", 0),
                "pct": round(s.get("destructionPercentage", 0), 2), "attacks": s.get("attacks", 0)}

    w = {
        "id": f"{kind}_{(prep or '').replace('-', '').replace(':', '')}_{(them.get('tag') or '').lstrip('#')}",
        "type": kind, "state": state,
        "prep": prep, "start": iso(raw.get("startTime")), "end": iso(raw.get("endTime")),
        "teamSize": raw.get("teamSize"), "apm": apm,
        "us": side(us), "them": side(them),
        "members": members,
    }
    if state == "warEnded":
        w["result"] = war_result(w["us"], w["them"])
    if raw.get("battleModifier") and raw.get("battleModifier") != "none":
        w["modifier"] = raw["battleModifier"]
    if extra:
        w.update(extra)
    return w


def store_war(wars, w):
    old = wars.get(w["id"])
    # законченную войну не перезаписываем более старым состоянием
    if old and old.get("state") == "warEnded" and w["state"] != "warEnded":
        return
    if old and old.get("state") == "warEnded" and not old.get("partial"):
        return  # законченная война больше не меняется
    if w["state"] != "warEnded":
        w["snap"] = NOW  # когда сделан последний снимок незаконченной войны
    elif old and old.get("snap"):
        w["snap"] = old["snap"]
    if w["state"] == "warEnded":
        w.pop("partial", None)
    wars[w["id"]] = w


def collect_current_war(wars, players):
    try:
        raw = api(f"/clans/{CLAN_TAG}/currentwar")
    except ApiError as e:
        if e.status == 403:
            errors.append("Журнал войн клана закрыт — данные КВ недоступны. Откройте его в настройках клана.")
        elif e.status != 404:
            errors.append(f"КВ: {e}")
        return
    # во время ЛВК currentwar может вернуть войну лиги — её соберём отдельно
    if raw.get("attacksPerMember") == 1 and raw.get("warStartTime"):
        return
    w = normalize_war(raw, "war", players)
    if w:
        store_war(wars, w)
        log(f"КВ: {w['us']['name']} vs {w['them']['name']} — {w['state']}")


def finalize_stale_wars(wars, cwl):
    """Если мы «проспали» конец войны (клан сразу начал новый поиск, а запуск
    по расписанию опоздал), война так и осталась бы в статусе «идёт».
    Закрываем такие войны: итог берём из журнала войн, атаки — из последнего
    снимка. Помечаем partial=True: последние минуты атак могли не попасть."""
    stale = [w for w in wars.values()
             if w.get("state") != "warEnded" and w.get("end") and w["end"] < NOW]
    if not stale:
        return
    log_items = []
    if any(w["type"] != "cwl" for w in stale):
        try:
            log_items = api(f"/clans/{CLAN_TAG}/warlog?limit=20").get("items", [])
        except ApiError as e:
            log(f"журнал войн недоступен: {e}")
    active_cwl_seasons = {s for s, v in cwl.items() if v.get("state") not in ("ended",)}
    for w in stale:
        if w["type"] == "cwl" and w.get("season") in active_cwl_seasons:
            # ЛВК-войну можно перезапросить по warTag — дождёмся следующего запуска,
            # но если сезон уже исчез из API, закроем как есть
            if w.get("end", "") > _hours_ago(48):
                continue
        for it in log_items:
            if (it.get("opponent") or {}).get("tag") == w["them"]["tag"] and iso(it.get("endTime")) == w["end"]:
                c, o = it.get("clan") or {}, it.get("opponent") or {}
                w["us"].update(stars=c.get("stars", w["us"]["stars"]),
                               pct=round(c.get("destructionPercentage", w["us"]["pct"]), 2),
                               attacks=c.get("attacks", w["us"]["attacks"]))
                w["them"].update(stars=o.get("stars", w["them"]["stars"]),
                                 pct=round(o.get("destructionPercentage", w["them"]["pct"]), 2))
                if it.get("result"):
                    w["result"] = it["result"]
                break
        w["state"] = "warEnded"
        w["partial"] = True
        w.setdefault("result", war_result(w["us"], w["them"]))
        log(f"закрыта война без финального снимка: {w['id']}")


def _hours_ago(h):
    t = datetime.strptime(NOW, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (t - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_cwl(wars, cwl, players):
    try:
        g = api(f"/clans/{CLAN_TAG}/currentwar/leaguegroup")
    except ApiError as e:
        if e.status not in (404,):
            errors.append(f"ЛВК: {e}")
        return
    if not g or g.get("state") in (None, "notInWar"):
        return
    season = g.get("season")
    if not season:
        return
    s = cwl.setdefault(season, {"season": season, "wars": {}, "others": {}})
    s["state"] = g.get("state")
    s["clans"] = [{"tag": c["tag"], "name": c.get("name"), "badge": badge(c.get("badgeUrls")),
                   "level": c.get("clanLevel")} for c in g.get("clans", [])]
    for c in g.get("clans", []):
        if c.get("tag") == CLAN_TAG:
            s["roster"] = [{"tag": m["tag"], "name": m.get("name"), "th": m.get("townHallLevel")}
                           for m in c.get("members", [])]
            for m in c.get("members", []):
                ensure_player(players, m["tag"], m.get("name"), m.get("townHallLevel"))

    for rnd, r in enumerate(g.get("rounds", []), start=1):
        for wt in r.get("warTags", []):
            if not wt or wt == "#0":
                continue
            known_id = s["wars"].get(wt)
            if known_id and wars.get(known_id, {}).get("state") == "warEnded":
                continue
            if s["others"].get(wt, {}).get("state") == "warEnded":
                continue
            try:
                raw = api(f"/clanwarleagues/wars/{wt}")
            except ApiError as e:
                errors.append(f"ЛВК война {wt}: {e}")
                continue
            tags = {raw.get("clan", {}).get("tag"), raw.get("opponent", {}).get("tag")}
            if CLAN_TAG in tags:
                w = normalize_war(raw, "cwl", players, {"season": season, "round": rnd, "warTag": wt})
                if w:
                    store_war(wars, w)
                    s["wars"][wt] = w["id"]
            else:
                c, o = raw.get("clan", {}), raw.get("opponent", {})
                s["others"][wt] = {
                    "round": rnd, "state": raw.get("state"),
                    "a": {"tag": c.get("tag"), "stars": c.get("stars", 0), "pct": c.get("destructionPercentage", 0)},
                    "b": {"tag": o.get("tag"), "stars": o.get("stars", 0), "pct": o.get("destructionPercentage", 0)},
                }
    log(f"ЛВК {season}: {s['state']}, наших войн {len(s['wars'])}")


# ───────────────────────── рейды столицы ─────────────────────────

def collect_raids(raids, players, first_run):
    try:
        data = api(f"/clans/{CLAN_TAG}/capitalraidseasons?limit={25 if first_run else 4}")
    except ApiError as e:
        errors.append(f"Рейды: {e}")
        return
    for it in data.get("items", []):
        start = iso(it.get("startTime"))
        if not start:
            continue
        rid = start[:10]
        members = []
        for m in it.get("members", []) or []:
            ensure_player(players, m["tag"], m.get("name"))
            members.append({
                "tag": m["tag"], "name": m.get("name"),
                "a": m.get("attacks", 0),
                "lim": (m.get("attackLimit", 5) or 0) + (m.get("bonusAttackLimit", 0) or 0),
                "loot": m.get("capitalResourcesLooted", 0),
            })
        raids[rid] = {
            "id": rid, "state": it.get("state"),
            "start": start, "end": iso(it.get("endTime")),
            "loot": it.get("capitalTotalLoot", 0),
            "raids": it.get("raidsCompleted", 0),
            "attacks": it.get("totalAttacks", 0),
            "districts": it.get("enemyDistrictsDestroyed", 0),
            "offReward": it.get("offensiveReward", 0),
            "defReward": it.get("defensiveReward", 0),
            "members": sorted(members, key=lambda x: -x["loot"]),
        }
    log(f"Рейды: всего выходных {len(raids)}")



# ───────────────────── вклады золота столицы ─────────────────────

CONTRIB_EVERY_H = float(os.environ.get("CONTRIB_EVERY_H", 1))


def collect_contributions(players, last_fetch):
    """Раз в час запоминает счётчик «всего вложено золота столицы» у каждого
    участника. Сохраняем только изменения: capHist = [[время, значение], ...].
    Куда именно вложено золото, API не сообщает — только общий счётчик."""
    if last_fetch:
        try:
            prev = datetime.strptime(last_fetch, "%Y-%m-%dT%H:%M:%SZ")
            cur = datetime.strptime(NOW, "%Y-%m-%dT%H:%M:%SZ")
            if (cur - prev).total_seconds() < CONTRIB_EVERY_H * 3600 - 120:
                return last_fetch
        except ValueError:
            pass
    ok = 0
    for p in players.values():
        if not p.get("inClan"):
            continue
        try:
            d = api(f"/players/{p['tag']}")
        except ApiError as e:
            log(f"игрок {p['tag']}: {e}")
            continue
        v = d.get("clanCapitalContributions")
        if v is None:
            continue
        ok += 1
        h = p.setdefault("capHist", [])
        if not h or h[-1][1] != v:
            h.append([NOW, v])
        p["capContrib"] = v
    log(f"Вклады столицы: обновлено {ok} игроков")
    return NOW if ok else last_fetch

# ───────────────────────── main ─────────────────────────

def collect_clan(tag, slug, show=True):
    """Собирает данные одного клана в data/<slug>/. Возвращает True при успехе."""
    global CLAN_TAG, DATA_DIR, errors
    CLAN_TAG, DATA_DIR, errors = tag, ROOT_DATA / slug, []

    meta = load("clan.json", {})
    players = load("players.json", {})
    wars = {w["id"]: w for w in load("wars.json", [])}
    raids = {r["id"]: r for r in load("raids.json", [])}
    cwl = load("cwl.json", {})
    first_run = not players

    try:
        clan = api(f"/clans/{CLAN_TAG}")
    except ApiError as e:
        log(f"!! {tag}: не удалось получить клан: {e}")
        if e.status == 403:
            log("   Проверьте ключ: он должен быть создан для IP 45.79.218.79")
        return False

    update_members(players, clan, first_run)
    collect_current_war(wars, players)
    collect_cwl(wars, cwl, players)
    collect_raids(raids, players, first_run)
    finalize_stale_wars(wars, cwl)
    contrib_fetched = collect_contributions(players, meta.get("contribFetched"))

    meta = {
        "tag": clan.get("tag"), "name": clan.get("name"),
        "badge": badge(clan.get("badgeUrls")), "level": clan.get("clanLevel"),
        "description": clan.get("description"), "members": clan.get("members"),
        "points": clan.get("clanPoints"), "warWins": clan.get("warWins"),
        "warLosses": clan.get("warLosses"), "warTies": clan.get("warTies"),
        "warStreak": clan.get("warWinStreak"),
        "warLeague": (clan.get("warLeague") or {}).get("name"),
        "capitalLeague": (clan.get("capitalLeague") or {}).get("name"),
        "capitalHall": (clan.get("clanCapital") or {}).get("capitalHallLevel"),
        "warLogPublic": clan.get("isWarLogPublic"),
        "trackingSince": meta.get("trackingSince") or NOW,
        "contribFetched": contrib_fetched,
        "updated": NOW,
        "errors": errors,
        "show": show,
    }

    save("clan.json", meta)
    save("players.json", players)
    save("wars.json", sorted(wars.values(), key=lambda w: w.get("prep") or "", reverse=True))
    save("raids.json", sorted(raids.values(), key=lambda r: r["start"], reverse=True))
    save("cwl.json", cwl)

    for e in errors:
        log("!!", tag, e)
    log(f"{meta['name']} {tag}: игроков {len(players)}, в клане {sum(1 for p in players.values() if p.get('inClan'))}, "
        f"войн {len(wars)}, рейдов {len(raids)}")
    return True


# ───────────────────── список кланов ─────────────────────
#
# Список хранится в _admin/clans.json. Папки с «_» GitHub Pages не публикует,
# поэтому со страниц сайта список не виден (на своём сервере он будет лежать
# вне папки сайта). Каждый клан открывается по своей ссылке ?c=<тег без #>;
# страница клана загружает только данные этого клана.
#
# {"default": "280L809U9",
#  "clans": [{"tag": "#280L809U9", "slug": "280L809U9", "name": "Badboys",
#             "collect": true, "show": true, "added": "..."}]}
#   collect — собирать ли статистику; show — показывать ли страницу клана.

FILES = ("clan.json", "players.json", "wars.json", "raids.json", "cwl.json")
REG_FILE = ROOT_DATA.parent / "_admin" / "clans.json"


def slug_of(tag):
    """Код клана в ссылке и имя папки с данными — тег без #."""
    return tag.lstrip("#").upper()


def load_registry():
    old_reg = ROOT_DATA / "clans.json"
    if REG_FILE.exists():
        reg = json.loads(REG_FILE.read_text("utf-8"))
    elif old_reg.exists():
        # переезд с промежуточной схемы: data/clans.json + папки data/<ТЕГ>/
        reg = json.loads(old_reg.read_text("utf-8"))
        for c in reg["clans"]:
            c["slug"] = slug_of(c["tag"])
            if reg.get("default") == c["tag"]:
                reg["default"] = c["slug"]
        old_reg.unlink()
        log("список кланов перенесён в _admin/clans.json")
    else:
        slug = slug_of(DEFAULT_TAG)
        reg = {"default": slug, "clans": [
            {"tag": DEFAULT_TAG, "slug": slug, "name": None, "collect": True, "show": True, "added": NOW}]}
        # переезд со старой схемы, где данные одного клана лежали прямо в data/
        if (ROOT_DATA / "clan.json").exists():
            d = ROOT_DATA / slug
            d.mkdir(parents=True, exist_ok=True)
            for name in FILES:
                if (ROOT_DATA / name).exists():
                    (ROOT_DATA / name).replace(d / name)
            log(f"данные основного клана перенесены в data/{slug}/")
    for c in reg["clans"]:
        c["slug"] = slug_of(c["tag"])
    return reg


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(path)


def save_registry(reg):
    write_json(REG_FILE, reg)
    # публично известен только основной клан — ссылка на сайт без параметров
    write_json(ROOT_DATA / "default.json", {"c": reg.get("default")})
    # флаг «показывать» дублируем в данные клана, чтобы страница знала, что она скрыта
    for c in reg["clans"]:
        f = ROOT_DATA / c["slug"] / "clan.json"
        if f.exists():
            m = json.loads(f.read_text("utf-8"))
            if m.get("show", True) != c.get("show", True):
                m["show"] = c.get("show", True)
                write_json(f, m)


def main():
    if not TOKEN and not MOCK_DIR:
        log("Нет COC_TOKEN. Добавьте секрет в настройках репозитория (см. README).")
        sys.exit(1)
    reg = load_registry()
    ok_any, tried = False, 0
    for c in reg["clans"]:
        if not c.get("collect", True):
            continue
        tried += 1
        if collect_clan(c["tag"], c["slug"], c.get("show", True)):
            ok_any = True
            m = load("clan.json", {})
            c["name"] = m.get("name") or c.get("name")
            c["badge"] = m.get("badge")
    save_registry(reg)
    if tried and not ok_any:
        sys.exit(1)


if __name__ == "__main__":
    main()
