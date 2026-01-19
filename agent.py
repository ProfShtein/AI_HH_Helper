import os
import re
import time
import shutil
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import quote_plus, urlsplit, urlunsplit, parse_qsl, urlencode

from playwright.sync_api import (
    sync_playwright,
    BrowserContext,
    Page,
    TimeoutError as PWTimeoutError,
)

# -------------------------
# Настройки
# -------------------------

YANDEX_PATH = os.path.expandvars(r"%LOCALAPPDATA%\Yandex\YandexBrowser\Application\browser.exe")

PROFILE_DIR = os.path.abspath("./browser_profile_pw")  # persistent-профиль (логин, куки)
FALLBACK_PROFILE_DIR = os.path.abspath("./browser_profile_pw_fallback")

HEADLESS = False
ITEMS_ON_PAGE = 20
MAX_LINKS_SCAN = 260


# -------------------------
# Модели
# -------------------------

@dataclass
class Vacancy:
    title: str
    url: str
    snippet: str


# -------------------------
# Текст/URL утилиты
# -------------------------

def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def set_query_param(url: str, key: str, value: str) -> str:
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q[key] = value
    new_query = urlencode(q, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def build_search_url(
    query: str,
    page: int = 0,
    area: Optional[int] = None,
    experience: Optional[str] = None,
    remote: Optional[bool] = None,
    salary: Optional[int] = None,
    only_with_salary: Optional[bool] = None,
) -> str:
    base = f"https://hh.ru/search/vacancy?text={quote_plus(query)}&items_on_page={ITEMS_ON_PAGE}&no_magic=true"

    if area is not None:
        base = set_query_param(base, "area", str(area))

    if experience:
        base = set_query_param(base, "experience", experience)

    if remote:
        base = set_query_param(base, "schedule", "remote")

    if salary is not None:
        base = set_query_param(base, "salary", str(salary))

    if only_with_salary:
        base = set_query_param(base, "only_with_salary", "true")

    base = set_query_param(base, "page", str(page))
    return base


def wait_settle(page: Page, timeout_ms: int = 20000) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass
    page.wait_for_timeout(500)


def safe_click(page: Page, selectors: List[str], timeout: int = 9000) -> bool:
    for sel in selectors:
        try:
            page.locator(sel).first.click(timeout=timeout)
            return True
        except Exception:
            continue
    return False


def safe_fill(page: Page, selectors: List[str], value: str, timeout: int = 9000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.click(timeout=timeout)
            loc.fill(value, timeout=timeout)
            return True
        except Exception:
            continue
    return False


def ensure_logged_in_hint(page: Page) -> None:
    # Подсказка: если видим "Войти" — вероятно, не авторизованы
    try:
        if page.locator("text=Войти").first.is_visible(timeout=1200):
            print("\n[info] Похоже, вы не авторизованы на hh.ru.")
            print("[info] Войдите вручную в открывшемся браузере и продолжайте работу.\n")
    except Exception:
        pass


def ensure_dir_clean(path: str) -> None:
    if os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)


# -------------------------
# Playwright запуск
# -------------------------

def _launch_once(pw, user_data_dir: str, use_yandex: bool) -> BrowserContext:
    kwargs = dict(
        user_data_dir=user_data_dir,
        headless=HEADLESS,
        viewport={"width": 1280, "height": 900},
        timeout=180_000,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        ignore_default_args=["--enable-automation"],
    )

    if use_yandex and os.path.exists(YANDEX_PATH):
        kwargs["executable_path"] = YANDEX_PATH

    return pw.chromium.launch_persistent_context(**kwargs)


def launch_context_robust(pw) -> BrowserContext:
    """
    Порядок попыток:
      1) Chromium (Playwright) с основным профилем
      2) Chromium (Playwright) с чистым fallback-профилем
      3) Yandex с чистым fallback-профилем (если установлен)
    """
    attempts = [
        ("Chromium main profile", PROFILE_DIR, False, False),
        ("Chromium fresh fallback", FALLBACK_PROFILE_DIR, False, True),
        ("Yandex fresh fallback", FALLBACK_PROFILE_DIR, True, True),
    ]

    last_exc: Optional[Exception] = None

    for name, profile_dir, use_yandex, fresh in attempts:
        try:
            if fresh:
                ensure_dir_clean(profile_dir)
            else:
                os.makedirs(profile_dir, exist_ok=True)

            print(f"[info] Launch attempt: {name} | profile={profile_dir}")
            ctx = _launch_once(pw, user_data_dir=profile_dir, use_yandex=use_yandex)
            print("[ok] Browser context launched.")
            return ctx
        except Exception as e:
            last_exc = e
            print(f"[warn] Launch failed: {name}\n       {type(e).__name__}: {e}\n")
            time.sleep(1.0)

    raise RuntimeError(f"Не удалось запустить браузер ни одним способом. Последняя ошибка: {last_exc}")


# -------------------------
# HH логика
# -------------------------

def collect_vacancies_from_search(page: Page) -> List[Vacancy]:
    vacancies: List[Vacancy] = []

    try:
        page.wait_for_selector("a[href*='/vacancy/']", timeout=20000)
    except Exception:
        return vacancies

    anchors = page.locator("a[href*='/vacancy/']")
    n = min(anchors.count(), MAX_LINKS_SCAN)

    seen = set()
    for i in range(n):
        try:
            a = anchors.nth(i)
            href = a.get_attribute("href") or ""
            if "/vacancy/" not in href:
                continue

            if href.startswith("/"):
                href = "https://hh.ru" + href

            href = href.split("?")[0]
            if href in seen:
                continue
            seen.add(href)

            title = norm_text(a.inner_text(timeout=800))
            if not title:
                continue

            snippet = ""
            try:
                card = a.locator("xpath=ancestor::*[self::div or self::article][1]")
                snippet = norm_text(card.inner_text(timeout=800))[:450]
            except Exception:
                pass

            vacancies.append(Vacancy(title=title, url=href, snippet=snippet))

            if len(vacancies) >= ITEMS_ON_PAGE:
                break
        except Exception:
            continue

    return vacancies


def open_vacancy(page: Page, v: Vacancy) -> None:
    page.goto(v.url, wait_until="domcontentloaded", timeout=60000)
    wait_settle(page)
    ensure_logged_in_hint(page)


def cover_letter_6_8_lines() -> str:
    lines = [
        "Здравствуйте!",
        "Интересна позиция Python-разработчика: пишу production-код и довожу задачи до результата.",
        "Опыт: backend/API, интеграции, фоновые задачи, БД, оптимизация и отладка.",
        "Работаю с Django/FastAPI, уделяю внимание качеству, тестированию и логированию.",
        "При необходимости подключаю LLM-интеграции (RAG, инструменты, оценка качества).",
        "Готов быстро пройти интервью и выполнить тестовое задание.",
        "Спасибо! Буду рад обсудить детали.",
    ]
    return "\n".join(lines)


def respond_to_vacancy(page: Page, v: Vacancy, letter: str, submit: bool) -> bool:
    open_vacancy(page, v)

    clicked = safe_click(
        page,
        selectors=[
            "text=Откликнуться",
            "button:has-text('Откликнуться')",
            "[data-qa*='vacancy-response']",
        ],
        timeout=12000,
    )

    if not clicked:
        print(f"[warn] Не нашёл кнопку «Откликнуться»: {v.url}")
        return False

    wait_settle(page)

    filled = safe_fill(
        page,
        selectors=[
            "[data-qa*='vacancy-response-letter'] textarea",
            "[data-qa*='cover-letter'] textarea",
            "textarea",
        ],
        value=letter,
        timeout=12000,
    )

    if not filled:
        try:
            ed = page.locator("[contenteditable='true']").first
            ed.click(timeout=6000)
            ed.fill(letter, timeout=6000)
            filled = True
        except Exception:
            pass

    if not filled:
        print(f"[warn] Не удалось вставить письмо. Оставил страницу открытой: {v.url}")
        return True

    if not submit:
        print(f"[ok] Письмо вставлено (НЕ отправлено). Проверьте и отправьте вручную: {v.url}")
        return True

    sent = safe_click(
        page,
        selectors=[
            "button:has-text('Отправить')",
            "text=Отправить",
        ],
        timeout=9000,
    )

    if sent:
        print(f"[ok] Отклик отправлен: {v.url}")
        return True

    print(f"[warn] Не нашёл финальную кнопку отправки. Проверьте вручную: {v.url}")
    return False


# -------------------------
# "AI" парсер задачи -> query и фильтры
# -------------------------

def _extract_int(text: str) -> Optional[int]:
    m = re.search(r"\b(\d{1,2})\b", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def ai_interpret_user_goal(user_text: str) -> dict:
    t = norm_text(user_text or "")
    tl = t.lower()

    want_n = _extract_int(tl)

    # area: Москва=1, СПб=2, РФ=113
    area = None
    if "моск" in tl:
        area = 1
    elif "питер" in tl or "спб" in tl or "санкт-петер" in tl:
        area = 2
    elif "росси" in tl or "рф" in tl:
        area = 113

    # опыт
    experience = None
    if any(x in tl for x in ["без опыта", "стажер", "стажёр", "intern", "junior", "джун"]):
        experience = "noExperience"
    elif any(x in tl for x in ["middle", "мидл", "мид"]):
        experience = "between1And3"
    elif any(x in tl for x in ["senior", "сеньор", "синьор", "lead", "лид"]):
        experience = "between3And6"

    if re.search(r"\b1\s*[-–]\s*3\b", tl):
        experience = "between1And3"
    if re.search(r"\b3\s*[-–]\s*6\b", tl):
        experience = "between3And6"
    if re.search(r"\b6\+\b|\bболее\s*6\b", tl):
        experience = "moreThan6"

    # удалёнка
    remote = any(x in tl for x in ["удален", "удалён", "remote", "из дома"])

    # зарплата
    salary = None
    only_with_salary = False
    m_sal = re.search(r"(?:от\s*)?(\d{2,3})\s*(?:к|k)\b", tl)  # "200к"
    if m_sal:
        salary = int(m_sal.group(1)) * 1000
    else:
        m_sal2 = re.search(r"(?:от\s*)?(\d{5,6})\b", tl)  # "150000"
        if m_sal2:
            salary = int(m_sal2.group(1))

    if salary is not None and any(x in tl for x in ["только с зп", "только с зарплат", "с зарплатой", "only_with_salary"]):
        only_with_salary = True

    # запрос (text)
    role_bits = []
    if "python" in tl or "питон" in tl:
        role_bits.append("Python")
    if "backend" in tl or "бэкенд" in tl or "бекенд" in tl:
        role_bits.append("backend")
    if "django" in tl:
        role_bits.append("Django")
    if "fastapi" in tl:
        role_bits.append("FastAPI")
    if "asyncio" in tl:
        role_bits.append("asyncio")

    if role_bits:
        query = " ".join(dict.fromkeys(role_bits)) + " разработчик"
    else:
        query = t

    query = re.sub(
        r"\b(открой|открыть|hh|hh\.ru|хх|хх\.ру|найди|найти|покажи|ваканси[яи]|с фильтрами|по фильтрам)\b",
        " ",
        query,
        flags=re.I,
    )
    query = norm_text(query)
    if not query:
        query = "Python разработчик"

    return {
        "query": query,
        "want_n": want_n,
        "area": area,
        "experience": experience,
        "remote": remote,
        "salary": salary,
        "only_with_salary": only_with_salary,
    }


def print_ai_examples() -> None:
    print(
        "\nПримеры обращения к AI:\n"
        "- Открой hh.ru и найди 5 вакансий Python backend в Москве, удалёнка, middle\n"
        "- Открой hh.ru и найди 3 вакансии Django в СПб, junior, зарплата от 150к\n"
        "- Открой hh.ru и найди 10 вакансий FastAPI по РФ, удалёнка, between 3-6\n"
    )


# -------------------------
# Интерактивный UI
# -------------------------

HELP = """
Команды:
  help                  справка
  list                  показать вакансии на текущей странице
  open N                открыть вакансию N
  apply N               подготовить/отправить отклик на вакансию N
  next                  следующая страница поиска
  prev                  предыдущая страница поиска
  refresh               обновить страницу и пересобрать список

  submit on|off          авто-отправка (по умолчанию off)

  ai                    повторное обращение к "AI" (перестроить поиск и фильтры)
  ai top                показать топ N (N из последней AI-фразы, иначе 5)
  ai help               примеры AI-запросов

  exit                  выйти
""".strip()


def print_list(vacancies: List[Vacancy], page_num: int) -> None:
    print(f"\nСтраница: {page_num} | Вакансий: {len(vacancies)}\n")
    for i, v in enumerate(vacancies, 1):
        print(f"{i:>2}. {v.title}")
    print("")


def parse_int(s: str) -> Optional[int]:
    try:
        return int(s)
    except Exception:
        return None


def run_search(
    page: Page,
    query: str,
    page_num: int,
    area: Optional[int],
    experience: Optional[str],
    remote: Optional[bool],
    salary: Optional[int],
    only_with_salary: Optional[bool],
) -> List[Vacancy]:
    url = build_search_url(
        query=query,
        page=page_num,
        area=area,
        experience=experience,
        remote=remote,
        salary=salary,
        only_with_salary=only_with_salary,
    )
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    wait_settle(page)
    ensure_logged_in_hint(page)
    return collect_vacancies_from_search(page)


def print_active_filters(
    query: str,
    area: Optional[int],
    experience: Optional[str],
    remote: Optional[bool],
    salary: Optional[int],
    only_with_salary: Optional[bool],
) -> None:
    area_name = {1: "Москва", 2: "СПб", 113: "РФ"}.get(area, str(area) if area else "не задано")
    print("\nАктивный поиск:")
    print(f"- text: {query}")
    print(f"- area: {area_name}")
    print(f"- experience: {experience or 'не задано'}")
    print(f"- remote: {'да' if remote else 'нет'}")
    if salary is None:
        print("- salary: не задано")
    else:
        print(f"- salary: от {salary}")
    print(f"- only_with_salary: {'да' if only_with_salary else 'нет'}\n")


def main() -> None:
    print("Агент HH (интерактивный режим).")

    # --- Первый ввод: обращение к AI (задача), а не поисковая строка
    user_goal = input(
        "AI> Опишите задачу (пример: 'Открой hh.ru и найди 5 вакансий Python в Москве, удалёнка, middle'): "
    ).strip()
    if not user_goal:
        user_goal = "Открой hh.ru и найди 5 вакансий Python разработчик в Москве, удалёнка, middle"

    parsed = ai_interpret_user_goal(user_goal)

    query = parsed.get("query") or "Python разработчик"
    last_ai_want_n: Optional[int] = parsed.get("want_n")

    area = parsed.get("area")
    experience = parsed.get("experience")
    remote = parsed.get("remote")
    salary = parsed.get("salary")
    only_with_salary = parsed.get("only_with_salary")

    submit = False
    letter = cover_letter_6_8_lines()
    page_num = 0

    with sync_playwright() as pw:
        context = launch_context_robust(pw)
        page = context.new_page()

        # Открываем hh.ru
        page.goto("https://hh.ru", wait_until="domcontentloaded", timeout=60000)
        wait_settle(page)
        ensure_logged_in_hint(page)

        # Переходим на поиск с фильтрами
        vacancies = run_search(
            page=page,
            query=query,
            page_num=page_num,
            area=area,
            experience=experience,
            remote=remote,
            salary=salary,
            only_with_salary=only_with_salary,
        )

        print_active_filters(query, area, experience, remote, salary, only_with_salary)

        if vacancies:
            print_list(vacancies, page_num)
        else:
            print("[warn] Не удалось собрать вакансии (возможна капча/изменение верстки). Попробуйте refresh или ai.")

        print("\nСопроводительное (6–8 строк):\n")
        print(letter)
        print("\n" + HELP + "\n")

        while True:
            cmd = input(f"[submit={'on' if submit else 'off'}] hh> ").strip()
            if not cmd:
                continue

            parts = cmd.split()
            c = parts[0].lower()

            try:
                if c == "help":
                    print("\n" + HELP + "\n")

                elif c == "exit":
                    break

                elif c == "list":
                    vacancies = collect_vacancies_from_search(page)
                    print_list(vacancies, page_num)

                elif c == "refresh":
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                    wait_settle(page)
                    ensure_logged_in_hint(page)
                    vacancies = collect_vacancies_from_search(page)
                    if not vacancies:
                        print("[warn] Пусто/не распарсилось. Возможно, капча.")
                    else:
                        print_list(vacancies, page_num)

                elif c == "next":
                    page_num += 1
                    vacancies = run_search(page, query, page_num, area, experience, remote, salary, only_with_salary)
                    print_list(vacancies, page_num)

                elif c == "prev":
                    page_num = max(0, page_num - 1)
                    vacancies = run_search(page, query, page_num, area, experience, remote, salary, only_with_salary)
                    print_list(vacancies, page_num)

                elif c == "submit":
                    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
                        print("Использование: submit on|off")
                        continue
                    submit = (parts[1].lower() == "on")
                    print(f"[ok] submit={'on' if submit else 'off'}")

                elif c == "open":
                    if len(parts) < 2:
                        print("Использование: open N")
                        continue
                    n = parse_int(parts[1])
                    if not n or n < 1 or n > len(vacancies):
                        print("[err] Неверный номер вакансии.")
                        continue
                    v = vacancies[n - 1]
                    open_vacancy(page, v)
                    print(f"[ok] Открыто: {v.title}\n{v.url}\n")

                elif c == "apply":
                    if len(parts) < 2:
                        print("Использование: apply N")
                        continue
                    n = parse_int(parts[1])
                    if not n or n < 1 or n > len(vacancies):
                        print("[err] Неверный номер вакансии.")
                        continue
                    v = vacancies[n - 1]
                    respond_to_vacancy(page, v, letter=letter, submit=submit)
                    page.wait_for_timeout(900)

                elif c == "ai":
                    sub = parts[1].lower() if len(parts) > 1 else ""

                    if sub == "help":
                        print_ai_examples()
                        continue

                    if sub == "top":
                        current = collect_vacancies_from_search(page)
                        if not current:
                            print("[warn] Нет вакансий для top. Сделайте refresh или ai (новый поиск).")
                            continue

                        top_n = last_ai_want_n if last_ai_want_n else 5
                        top_n = max(1, min(top_n, len(current)))

                        print(f"\nТоп {top_n} вакансий (как на странице):\n")
                        for i, v in enumerate(current[:top_n], 1):
                            print(f"{i:>2}. {v.title}\n    {v.url}")
                        print("")
                        continue

                    user_text = input(
                        "AI> Сформулируйте заново (пример: 'Найди 3 вакансии Django в СПб, junior, удалёнка'): "
                    ).strip()
                    if not user_text:
                        print("[info] Пусто — команда ai отменена.")
                        continue

                    parsed = ai_interpret_user_goal(user_text)

                    query = parsed.get("query") or query
                    last_ai_want_n = parsed.get("want_n") or last_ai_want_n

                    area = parsed.get("area")
                    experience = parsed.get("experience")
                    remote = parsed.get("remote")
                    salary = parsed.get("salary")
                    only_with_salary = parsed.get("only_with_salary")

                    page_num = 0
                    vacancies = run_search(page, query, page_num, area, experience, remote, salary, only_with_salary)

                    print_active_filters(query, area, experience, remote, salary, only_with_salary)

                    if vacancies:
                        print_list(vacancies, page_num)
                    else:
                        print("[warn] По этому запросу не удалось собрать вакансии (возможно капча/верстка).")

                else:
                    print("[err] Неизвестная команда. Введите help.")

            except PWTimeoutError:
                print("[warn] Таймаут. Попробуйте refresh или повторите команду.")
            except Exception as e:
                print(f"[warn] Ошибка: {type(e).__name__}: {e}")

        context.close()


if __name__ == "__main__":
    main()