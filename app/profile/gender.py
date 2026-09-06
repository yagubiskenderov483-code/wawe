from __future__ import annotations

import re

_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001faff"
    "\U0001f900-\U0001f9ff"
    "\U00002600-\U000027bf"
    "\U0000fe00-\U0000fe0f"
    "\U0000200d"
    "]+",
    flags=re.UNICODE,
)
_NON_LETTERS_RE = re.compile(r"[^\w\-]+", flags=re.UNICODE)
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґ]")

_MALE_A_NAMES = {
    "никита",
    "илья",
    "илия",
    "савва",
    "фома",
    "кузьма",
    "данила",
    "лука",
    "лёва",
    "лева",
    "миша",
    "ваня",
    "вова",
    "дима",
    "коля",
    "паша",
    "рома",
    "гена",
    "толя",
    "юра",
    "костя",
    "степа",
    "стёпа",
    "лёша",
    "леша",
    "серёжа",
    "сережа",
    "nikita",
    "ilya",
    "ilja",
    "sava",
    "luka",
    "misha",
    "vanya",
    "vova",
    "dima",
    "kolya",
    "pasha",
    "roma",
    "gena",
    "tolya",
    "yura",
    "kostya",
    "styopa",
    "lesha",
    "lyosha",
    "seryozha",
}
_MALE_A_NAMES |= {
    # Russian male hypocorisms ending in -а / -я. Without these the
    # "cyrillic name ends in а/я -> female" guess labels every one of them
    # female, which is how men reach a girls-only channel.
    "витя",
    "вася",
    "петя",
    "митя",
    "сеня",
    "тёма",
    "тема",
    "гоша",
    "жора",
    "лёха",
    "леха",
    "валера",
    "серёга",
    "серега",
    "гриша",
    "яша",
    "боря",
    "федя",
    "кеша",
    "сева",
    "тоха",
    "саня",
    "гера",
    "эдя",
    "додя",
    "жека",
    "димка",
    "вовка",
    "санька",
    "vitya",
    "vasya",
    "petya",
    "mitya",
    "senya",
    "tyoma",
    "tema",
    "gosha",
    "zhora",
    "lyokha",
    "lekha",
    "valera",
    "seryoga",
    "seryoga",
    "grisha",
    "yasha",
    "borya",
    "fedya",
    "kesha",
    "seva",
    "tokha",
    "sanya",
    "gera",
    "zheka",
}

_UNISEX_NAMES = {"саша", "женя", "валя", "слава", "sasha", "zhenya"}
_FEMALE_NAMES = {
    "anna",
    "maria",
    "marie",
    "elena",
    "olga",
    "irina",
    "natalia",
    "natalie",
    "ekaterina",
    "kate",
    "katya",
    "daria",
    "darya",
    "alina",
    "alisa",
    "alice",
    "victoria",
    "viktoria",
    "sofia",
    "sophia",
    "anastasia",
    "yulia",
    "julia",
    "polina",
    "ksenia",
    "oksana",
    "tatiana",
    "tatyana",
    "marina",
    "svetlana",
    "ludmila",
    "lyudmila",
    "galina",
    "nina",
    "vera",
    "nadezhda",
    "lyubov",
    "karina",
    "arina",
    "milana",
    "varvara",
    "evgenia",
    "elizaveta",
    "nastya",
    "masha",
    "dasha",
    "lena",
    "olya",
    "yulya",
    "tanya",
    "natasha",
    "anya",
    "liza",
    "vika",
    "sonya",
    "ksyusha",
    "ira",
    "lera",
    "nika",
    "diana",
    "kristina",
    "valeria",
    "veronica",
    "angelina",
    "alexandra",
    "uliana",
    "yana",
    "kira",
    "zlata",
    "margo",
    "rita",
    "anna",
    "анна",
    "мария",
    "маша",
    "елена",
    "лена",
    "ольга",
    "оля",
    "ирина",
    "ира",
    "наталья",
    "наталия",
    "наташа",
    "екатерина",
    "катя",
    "катюша",
    "дарья",
    "даша",
    "алина",
    "алиса",
    "виктория",
    "вика",
    "софия",
    "софья",
    "соня",
    "анастасия",
    "настя",
    "юлия",
    "юля",
    "полина",
    "ксения",
    "ксюша",
    "оксана",
    "татьяна",
    "таня",
    "марина",
    "светлана",
    "людмила",
    "галина",
    "нина",
    "вера",
    "надежда",
    "любовь",
    "карина",
    "арина",
    "милана",
    "варвара",
    "евгения",
    "елизавета",
    "лиза",
    "диана",
    "кристина",
    "валерия",
    "лера",
    "вероника",
    "ангелина",
    "александра",
    "ульяна",
    "яна",
    "инна",
    "кира",
    "злата",
    "маргарита",
    "рита",
    "ника",
    "аня",
    "анечка",
    "василиса",
    "лилия",
    "мирослава",
    "таисия",
    "элина",
    "регина",
}
_SLAVIC_FIRST_NAMES = _FEMALE_NAMES | _MALE_A_NAMES | {
    "иван",
    "ivan",
    "петр",
    "пётр",
    "алексей",
    "alexey",
    "дмитрий",
    "dmitry",
    "сергей",
    "sergey",
    "андрей",
    "andrey",
    "максим",
    "maxim",
    "николай",
    "nikolai",
    "владимир",
    "vladimir",
    "павел",
    "pavel",
    "роман",
    "roman",
    "артем",
    "артём",
    "кирилл",
    "константин",
    "евгений",
    "михаил",
    "александр",
    "денис",
    "олег",
    "юрий",
    "виктор",
    "игорь",
    "anton",
    "антон",
}


def _name_tokens(*parts: str | None) -> list[str]:
    tokens: list[str] = []
    for part in parts:
        raw = _EMOJI_RE.sub(" ", part or "")
        raw = _NON_LETTERS_RE.sub(" ", raw)
        for token in raw.split():
            cleaned = token.casefold().strip("-")
            if cleaned:
                tokens.append(cleaned)
    return tokens


def looks_slavic_name(first_name: str | None, last_name: str | None = None) -> bool:
    tokens = _name_tokens(first_name, last_name)
    if not tokens:
        return False
    for token in tokens:
        if _CYRILLIC_RE.search(token):
            return True
        if token in _SLAVIC_FIRST_NAMES:
            return True
        if token.endswith(("ова", "ева", "ина", "ская", "цкая", "ova", "eva", "ina", "skaya")):
            return True
        if token.endswith(("ов", "ев", "ин", "ский", "ov", "ev", "in", "sky")):
            return True
    return False


def infer_gender(
    first_name: str | None,
    manual: str | None = None,
    last_name: str | None = None,
    suffix_guess: bool = False,
) -> str | None:
    """Infer gender from a display name.

    `suffix_guess` enables the old "cyrillic name ends in а/я -> female" rule.
    It is off by default: Russian male hypocorisms (Витя, Вася, Костя, Гоша)
    end the same way, so the rule lets men through a female-only filter.
    Names outside the known lists return None, which a strict filter skips.
    """
    if manual:
        value = manual.strip().lower()
        if value:
            return value
    tokens = _name_tokens(first_name)
    if not tokens:
        tokens = _name_tokens(last_name)
        return _gender_from_last_name(tokens)
    name = tokens[0]
    if name in _UNISEX_NAMES:
        return _gender_from_last_name(_name_tokens(last_name))
    if name in _FEMALE_NAMES:
        return "female"
    if name in _MALE_A_NAMES:
        return "male"
    last_guess = _gender_from_last_name(_name_tokens(last_name))
    if last_guess:
        return last_guess
    if suffix_guess and _CYRILLIC_RE.search(name) and name.endswith(("а", "я", "ия")):
        return "female"
    return None


def _gender_from_last_name(tokens: list[str]) -> str | None:
    for token in tokens:
        if token.endswith(("ова", "ева", "ина", "ская", "цкая", "ая", "ova", "eva", "ina", "skaya")):
            return "female"
        if token.endswith(("ов", "ев", "ин", "ский", "ov", "ev", "in", "sky")):
            return "male"
    return None
