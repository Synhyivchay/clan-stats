#!/usr/bin/env python3
"""
Управление списком кланов (data/clans.json).

Запускается из GitHub Actions кнопкой «Run workflow» в «Управление кланами»
(.github/workflows/manage.yml) — запускать её может только владелец репозитория.

  python3 scripts/manage.py <действие> <тег>

Действия:
  add          — добавить клан (тег проверяется через API)
  collect-off  — остановить сбор статистики (данные сохраняются)
  collect-on   — возобновить сбор
  hide         — скрыть страницу клана на сайте
  show         — снова показывать страницу клана
  remove       — убрать клан из списка (собранные данные остаются в репозитории)
  default      — сделать клан основным (открывается по ссылке без параметров)
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect  # noqa: E402

ACTIONS = {"add", "collect-off", "collect-on", "hide", "show", "remove", "default"}


def norm(tag):
    tag = (tag or "").strip().upper().replace("O", "0")
    if not tag.startswith("#"):
        tag = "#" + tag
    if not re.fullmatch(r"#[0289PYLQGRJCUV]{3,12}", tag):
        raise SystemExit(f"Похоже, это не тег клана: «{tag}». Тег состоит из символов 0289PYLQGRJCUV, например #280L809U9.")
    return tag


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ACTIONS:
        raise SystemExit(__doc__)
    action, tag = sys.argv[1], norm(sys.argv[2])
    reg = collect.load_registry()
    clans = reg["clans"]
    found = next((c for c in clans if c["tag"] == tag), None)

    if action == "add":
        if found:
            found["collect"] = found["show"] = True
            print(f"{tag} уже в списке — сбор и показ включены.")
        else:
            try:
                info = collect.api(f"/clans/{tag}")
            except collect.ApiError as e:
                raise SystemExit(f"Клан {tag} не найден в игре или API недоступен: {e}")
            slug = collect.slug_of(tag)
            clans.append({"tag": tag, "slug": slug, "name": info.get("name"), "badge": collect.badge(info.get("badgeUrls")),
                          "collect": True, "show": True, "added": collect.NOW})
            print(f"Добавлен клан {info.get('name')} {tag}. Данные появятся после следующего сбора (до 5 минут).")
            print(f"Ссылка для клана: https://clantracker.net/clan/?c={slug}")
    else:
        if not found:
            raise SystemExit(f"Клана {tag} нет в списке.")
        if action == "collect-off":
            found["collect"] = False
        elif action == "collect-on":
            found["collect"] = True
        elif action == "hide":
            found["show"] = False
        elif action == "show":
            found["show"] = True
        elif action == "remove":
            if reg.get("default") == found["slug"]:
                raise SystemExit("Это основной клан — сначала сделайте основным другой (действие default).")
            clans.remove(found)
        elif action == "default":
            if not found.get("show", True):
                raise SystemExit("Скрытый клан нельзя сделать основным — сначала покажите его (show).")
            reg["default"] = found["slug"]
        print(f"{tag}: выполнено «{action}».")

    collect.save_registry(reg)


if __name__ == "__main__":
    main()
