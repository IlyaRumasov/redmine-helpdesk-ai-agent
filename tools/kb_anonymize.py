"""Готує обезличені решені заявки HelpDesk для бази знань (локально, без мережі).

Читає issues.json і journals.json з C:\\Claude\\redmine-analysis (там персональні дані, їх не комітимо),
пише kb/kb_tickets.jsonl: тема, опис, відповіді інженерів, де логіни, пошти, телефони, імена, IP, посилання
замінено мітками (<LOGIN>, <EMAIL>, <PHONE>, <PERSON>, <IP>, <URL>). Вивід у чат лише зі статистикою й обезличеними прикладами.

python tools/kb_anonymize.py [--sample 6]
"""
import argparse, collections, json, re, sys
from pathlib import Path

DIR = Path(r"C:\Claude\redmine-analysis")
OUT = DIR / "kb"

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
HANDLE = re.compile(r"(?<![\w.])@[A-Za-z0-9_]{3,}")  # @нік у Telegram, залишки паролів із @
URL = re.compile(r"https?://\S+")
IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{8,}\d")
IMG = re.compile(r"!\S+\.(?:png|jpg|jpeg|gif)!|!clipboard-\S+!")
# логіни: ms.x.y, mdy_x_y, x.surname, name_surname з обов'язковою крапкою або підкресленням між літерними частинами
LOGIN = re.compile(r"\b(?:ms|mdy)[._][a-z0-9][a-z0-9._-]*[a-z0-9]\b|\b[a-z][a-z0-9-]{0,20}[._][a-z][a-z0-9._-]{2,30}\b", re.I)
SAFE_DOTTED = re.compile(r"\.(?:com|net|org|pdf|png|jpg|docx?|xlsx?|txt|json|csv|zip|exe|msi|local|google|io|ua|ai|tech)$", re.I)
SIGN = re.compile(r"(?is)\n-- ?\n.*$")
QUOTE_HDR = re.compile(r"(?m)^.*\bwrote in #note-\d+:[ \t]*\n?")  # «Ім'я Прізвище wrote in #note-6:» (цитата Redmine з іменем автора)
CAP = r"[А-ЯІЇЄҐ][а-яіїєґ'’-]{2,}"
# ПІБ за по батькові: «Прізвище Ім'я По-батькові» (і в зворотному порядку), навіть якщо людини немає серед авторів заявок
FIO = re.compile(rf"\b{CAP}\s+{CAP}\s+{CAP}(?:ович|евич|йович|ович|ич|івна|ївна|евна|овна|ична|ична)\b|\b{CAP}\s+{CAP}(?:ович|евич|йович|ич|івна|ївна|евна|овна)\b")
DOMAIN = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|ua|us|ai|dev|tech|co|eu|info|biz|me|app|cloud|pl|de|ru)\b", re.I)
BIGRAM = re.compile(rf"\b({CAP})\s+({CAP})\b")
CLIENTS = re.compile(r"\b(?:<CLIENT_NAME_A>|<CLIENT_NAME_B>)\b", re.I)
# слова з великої літери, що збігаються з іменем якогось користувача, але іменами не є (знайдено перевіркою замін на всьому корпусі)
EXTRA_STOP = {"redmine", "hello", "acme", "user", "users", "finance", "test", "editor", "vendor", "change", "lutsk", "адміни", "support", "teams", "google"}
GENERIC_DOMAINS ={"google.com", "gmail.com", "microsoft.com", "office.com", "zoom.us", "github.com", "docs.google.com", "meet.google.com", "teams.microsoft.com"}


def build_name_tokens(issues, journals):
    names = set()
    for i in issues.values():
        for k in ("author", "assignee"):
            if i.get(k):
                names.add(i[k])
    for t in journals:
        for j in t["journals"]:
            if j.get("user"):
                names.add(j["user"])
    extra = DIR / "user_name_tokens.json"  # усі імена користувачів Redmine, зібрані локально (лише для заміни)
    if extra.exists():
        names.update(json.load(open(extra, encoding="utf-8")))
    toks = set()
    for n in names:
        for w in re.split(r"[\s,]+", n):
            if len(w) >= 4 and w[0].isalpha():
                toks.add(w)
    return toks


def make_cleaner(tokens, stop=frozenset()):
    low = {t.lower() for t in tokens}
    cap_word = re.compile(r"\b[А-ЯІЇЄҐA-Z][а-яіїєґa-z'’-]{2,}\b")

    def is_name(w):
        w = w.lower()
        forms = [w[:len(w) - k] for k in range(0, 4) if len(w) - k >= 4]
        if any(f in stop for f in forms):  # часті слова (Redmine, Acme, Hello, привітання) не вважаємо іменами, навіть якщо є користувач із таким словом в імені
            return False
        return any(f in low for f in forms)

    class _TokRe:  # імітує re.sub з тим самим інтерфейсом: лише слова з великої літери, що збігаються з іменами (з відмінковими закінченнями)
        @staticmethod
        def sub(repl, text):
            return cap_word.sub(lambda m: repl(m) if is_name(m.group(0)) else m.group(0), text)
    tok_re = _TokRe if tokens else None
    counts = collections.Counter()

    def sub(rx, label, text):
        def r(m):
            if rx is LOGIN and SAFE_DOTTED.search(m.group(0)):
                return m.group(0)
            if rx is DOMAIN and m.group(0).lower() in GENERIC_DOMAINS:
                return m.group(0)
            counts[label] += 1
            return label
        return rx.sub(r, text)

    def person(m):
        a, b = m.group(1), m.group(2)
        if a.lower() in stop and b.lower() in stop:
            return m.group(0)
        counts["<PERSON>"] += 1
        return "<PERSON>"

    def clean(text):
        text = (text or "").replace("\r", "")
        text = IMG.sub("", text)
        text = SIGN.sub("", text)
        text = QUOTE_HDR.sub("", text)
        text = sub(EMAIL, "<EMAIL>", text)
        text = sub(HANDLE, "<HANDLE>", text)
        text = sub(URL, "<URL>", text)
        text = sub(DOMAIN, "<DOMAIN>", text)
        text = sub(CLIENTS, "<CLIENT>", text)
        text = sub(FIO, "<PERSON>", text)
        text = sub(IP, "<IP>", text)
        text = sub(PHONE, "<PHONE>", text)
        text = sub(LOGIN, "<LOGIN>", text)
        if tok_re:
            text = sub(tok_re, "<PERSON>", text)
        text = sub(BIGRAM, "<PERSON>", text) if False else BIGRAM.sub(lambda m: person(m), text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()
    return clean, counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=6)
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    issues = {str(i["id"]): i for i in json.load(open(DIR / "issues.json", encoding="utf-8"))}
    journals = [t for t in json.load(open(DIR / "journals.json", encoding="utf-8")) if "journals" in t]
    # стоп-список: слова з великої літери, що часто трапляються (привітання, «Нова Пошта» тощо), не вважаємо іменами
    freq = collections.Counter()
    for t in journals:
        txt = (t.get("description") or "") + " " + " ".join((j.get("notes") or "") for j in t["journals"])
        freq.update(w.lower() for w in re.findall(CAP, txt))
    stop = {w for w, c in freq.items() if c >= 10} | {"нова", "нової", "пошта", "пошти", "сервіс", "редмайн", "гугл", "драйв", "тімс", "зум", "вітаю", "привіт", "добрий", "доброго", "дякую"} | EXTRA_STOP
    clean, counts = make_cleaner(build_name_tokens(issues, journals), stop)
    OUT.mkdir(exist_ok=True)
    rows, skipped = [], collections.Counter()
    for t in journals:
        iss = issues.get(str(t["id"]), {})
        if iss.get("project") != "HelpDesk" or iss.get("status") not in ("Closed", "Resolved"):
            skipped["не HelpDesk/не закрита"] += 1
            continue
        author = iss.get("author")
        notes = [clean(j["notes"]) for j in t["journals"] if (j.get("notes") or "").strip() and j.get("user") != author]
        notes = [n for n in notes if len(n) >= 15]
        if not notes:
            skipped["немає відповіді інженера"] += 1
            continue
        rows.append({"id": t["id"], "subject": clean(iss.get("subject")), "description": clean(t.get("description"))[:1500],
                     "answer": "\n---\n".join(notes)[:2000], "answer_len": max(len(n) for n in notes), "category": iss.get("category")})
    with open(OUT / "kb_tickets.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("заявок у KB-вибірці:", len(rows), "| пропущено:", dict(skipped))
    print("замін:", dict(counts))
    print("із відповіддю >=80 символів:", sum(1 for r in rows if r["answer_len"] >= 80), "| >=200:", sum(1 for r in rows if r["answer_len"] >= 200))
    rich = [r for r in rows if r["answer_len"] >= 200][: a.sample]
    for r in rich:
        print(f"\n#{r['id']} [{r['subject'][:80]}]\n  опис: {r['description'][:220]!r}\n  відповідь: {r['answer'][:300]!r}")


if __name__ == "__main__":
    main()
