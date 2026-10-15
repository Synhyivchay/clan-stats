#!/usr/bin/env python3
"""
Цикл сбора данных внутри одного запуска GitHub Actions.

Расписание GitHub ненадёжно (запуски могут опаздывать на часы), поэтому один
запуск работает почти 6 часов и сам собирает данные каждые INTERVAL секунд.
Следующий запуск по расписанию встаёт в очередь и подхватывает работу, когда
этот закончится, — так сбор идёт без перерывов.

Коммит в репозиторий делается:
  • сразу, если изменилось что-то важное (атаки в войне, рейды, состав клана);
  • не реже и не чаще раза в 10 минут, если поменялись только «шумные» поля
    (трофеи, донаты, время обновления) — чтобы не пересобирать сайт впустую.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
INTERVAL = int(os.environ.get("INTERVAL", 300))          # 5 минут
DURATION = int(os.environ.get("DURATION", 5 * 3600 + 40 * 60))  # 5 ч 40 мин
QUIET_PUSH = int(os.environ.get("QUIET_PUSH", 600))      # 10 минут

VOLATILE = {"updated", "snap", "lastSeen", "trophies", "donations", "received",
            "rank", "exp", "league", "leagueIcon", "points", "contribFetched"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def strip(o):
    if isinstance(o, dict):
        return {k: strip(v) for k, v in o.items() if k not in VOLATILE}
    if isinstance(o, list):
        return [strip(v) for v in o]
    return o


def signature():
    h = hashlib.sha256()
    for f in sorted(list(DATA.rglob("*.json")) + list((ROOT / "_admin").glob("*.json"))):
        try:
            h.update(json.dumps(strip(json.loads(f.read_text("utf-8"))), sort_keys=True).encode())
        except Exception:
            h.update(f.read_bytes())
    return h.hexdigest()


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, check=check, capture_output=True, text=True)


def push(reason):
    git("add", "data", "_admin")
    staged = git("diff", "--staged", "--quiet", check=False).returncode != 0
    ahead = git("rev-list", "--count", "@{u}..HEAD", check=False).stdout.strip() not in ("", "0")
    if not staged and not ahead:
        return False
    if staged:
        git("commit", "-m", f"Обновление данных {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC ({reason})")
    for attempt in range(3):
        # при конфликте берём версию из репозитория: там правки из «Управления кланами»
        r = git("pull", "--rebase", "-X", "ours", "--quiet", check=False)
        if r.returncode == 0 and git("push", "--quiet", check=False).returncode == 0:
            log("отправлено:", reason)
            return True
        git("rebase", "--abort", check=False)
        time.sleep(5)
    log("!! не удалось отправить изменения, попробую в следующий раз")
    return False


def refresh_registry():
    """Перед каждым сбором берём свежий список кланов из репозитория —
    чтобы добавленный или выключенный клан учитывался сразу, а не после
    следующей отправки данных."""
    if git("fetch", "--quiet", "origin", "main", check=False).returncode != 0:
        return
    if git("cat-file", "-e", "FETCH_HEAD:_admin/clans.json", check=False).returncode == 0:
        git("checkout", "FETCH_HEAD", "--", "_admin/clans.json", check=False)
        git("reset", "--quiet", "--", "_admin/clans.json", check=False)


def main():
    start = time.time()
    last_sig = signature()
    last_push = 0.0
    n = 0
    while True:
        tick = time.time()
        n += 1
        refresh_registry()
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "collect.py")], cwd=ROOT,
                           capture_output=True, text=True)
        out = (r.stderr or "").strip().splitlines()
        log(f"сбор #{n}: код {r.returncode}; {out[-1] if out else ''}")
        if r.returncode != 0:
            print(r.stderr[-2000:], flush=True)
        else:
            sig = signature()
            if sig != last_sig:
                if push("изменения"):
                    last_sig, last_push = sig, time.time()
            elif time.time() - last_push >= QUIET_PUSH - 30:
                if push("плановое"):
                    last_push = time.time()

        if time.time() - start + INTERVAL > DURATION:
            log("время запуска вышло, работу продолжит следующий запуск")
            break
        time.sleep(max(5, INTERVAL - (time.time() - tick)))


if __name__ == "__main__":
    main()
