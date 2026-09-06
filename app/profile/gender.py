from __future__ import annotations

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
}
_UNISEX_NAMES = {"саша", "женя", "валя", "слава"}
_FEMALE_LATIN = {
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
}


def infer_gender(first_name: str | None, manual: str | None = None) -> str | None:
    if manual:
        value = manual.strip().lower()
        if value:
            return value
    raw = (first_name or "").strip()
    if not raw:
        return None
    name = raw.split()[0].casefold()
    if name in _UNISEX_NAMES:
        return None
    if name in _FEMALE_LATIN:
        return "female"
    if name in _MALE_A_NAMES:
        return "male"
    if name.endswith(("а", "я", "ия", "a", "ia", "ya")):
        return "female"
    return None
