"""
Deterministic, zero-cost article tagger — the first pass for every article.

The Gemini extractor (extract_and_tag.py) is quota-bound (free tier: 20
requests/day/model, plus frequent 503 "high demand" rejections), which left
production with a 5,000+ article backlog and an empty feed. This module tags
every article immediately with dictionary methods, the standard baseline for
financial text (Loughran & McDonald, 2011; Apel & Blix Grimaldi, 2014); the LLM
then refines rows in batches as quota allows (tagger='rules' -> 'llm').

Output matches extract_and_tag.Extraction's fields:
  country_codes   ISO2, from country names/aliases, central banks, capitals and
                  sector-defining companies (case-sensitive, longest match first)
  sector_tags     keyword lexicon per atomized commodity
  article_category / policy_subtype / policy_stance / sentiment_score / mechanisms
"""
from __future__ import annotations

import re

from common.countries import ALL_COUNTRIES, EURO_ADOPTION

EURO_MEMBERS = sorted(EURO_ADOPTION)

# ---------------------------------------------------------------- countries --
# Official World Bank names that are unusable as-is or too ambiguous in news
# text ("Georgia" is usually the US state, "Jordan"/"Chad" are personal names).
_SKIP_OFFICIAL = {"GE", "JO", "TD", "CD", "CG", "KP", "KR"}
_ALIASES: dict[str, list[str]] = {
    "US": ["United States", "U.S.", "US", "USA", "Federal Reserve", "FOMC", "Fed", "Powell", "Wall Street", "Treasury Department"],
    "GB": ["United Kingdom", "UK", "U.K.", "Britain", "British", "Bank of England", "BoE", "BOE"],
    "JP": ["Japanese", "Bank of Japan", "BOJ", "BoJ", "Tokyo", "Ueda"],
    "CN": ["Chinese", "People's Bank of China", "PBOC", "PBoC", "Beijing", "Shanghai", "Shenzhen", "Huawei", "SMIC", "CATL", "Tsingshan", "Zijin", "Ganfeng", "Tianqi", "CMOC"],
    "IN": ["Reserve Bank of India", "RBI", "New Delhi", "Mumbai"],
    "AU": ["Australian", "Reserve Bank of Australia", "RBA", "Canberra", "BHP", "Pilbara Minerals", "Lynas"],
    "CA": ["Canadian", "Bank of Canada", "Ottawa"],
    "CH": ["Swiss", "Swiss National Bank", "SNB"],
    "SE": ["Swedish", "Riksbank"],
    "NO": ["Norwegian", "Norges Bank"],
    "NZ": ["Reserve Bank of New Zealand", "RBNZ"],
    "KR": ["South Korea", "South Korean", "Korea", "Korean", "Bank of Korea", "Seoul", "Samsung", "SK Hynix"],
    "KP": ["North Korea", "North Korean", "Pyongyang"],
    "TW": ["Taiwanese", "Taipei", "TSMC", "MediaTek", "Foxconn"],
    "MX": ["Mexican", "Banxico"],
    "BR": ["Brazilian", "Copom", "Brasilia", "Vale"],
    "ZA": ["South African", "SARB", "Pretoria", "Johannesburg"],
    "TR": ["Turkey", "Turkish", "Türkiye", "CBRT", "Ankara"],
    "ID": ["Indonesian", "Bank Indonesia", "Jakarta"],
    "PH": ["Filipino", "Philippine", "Bangko Sentral", "Manila"],
    "MY": ["Malaysian", "Bank Negara", "Kuala Lumpur"],
    "TH": ["Thai", "Bank of Thailand", "Bangkok"],
    "VN": ["Vietnam", "Vietnamese", "Hanoi"],
    "CL": ["Chilean", "Codelco", "SQM", "Antofagasta", "Santiago de Chile"],
    "PE": ["Peruvian", "BCRP"],
    "AR": ["Argentine", "Argentinian", "Buenos Aires"],
    "CO": ["Colombian", "Bogota"],
    "CD": ["DR Congo", "DRC", "Democratic Republic of Congo", "Democratic Republic of the Congo", "Congo-Kinshasa", "Congo", "Congolese", "Kinshasa"],
    "CG": ["Republic of Congo", "Republic of the Congo", "Congo-Brazzaville", "Brazzaville"],
    "ZM": ["Zambian", "Lusaka"],
    "RU": ["Russia", "Russian", "Moscow", "Norilsk", "Nornickel"],
    "IR": ["Iran", "Iranian", "Tehran"],
    "EG": ["Egypt", "Egyptian", "Cairo"],
    "VE": ["Venezuela", "Venezuelan"],
    "SA": ["Saudi", "Riyadh"],
    "AE": ["UAE", "Emirati", "Dubai", "Abu Dhabi"],
    "IL": ["Israeli", "Bank of Israel"],
    "DE": ["German", "Bundesbank", "Berlin"],
    "FR": ["French", "Paris"],
    "IT": ["Italian", "Rome"],
    "ES": ["Spanish", "Madrid"],
    "NL": ["Dutch", "ASML", "Amsterdam"],
    "PL": ["Polish", "Warsaw"],
    "NG": ["Nigerian", "Abuja", "Lagos"],
    "CZ": ["Czech Republic", "Czech"],
    "SK": ["Slovakia"],
    "KG": ["Kyrgyzstan"],
    "LA": ["Laos"],
    "SY": ["Syria"],
    "YE": ["Yemen"],
    "CI": ["Ivory Coast", "Côte d'Ivoire"],
    "GM": ["Gambia"],
    "BS": ["Bahamas"],
    "FM": ["Micronesia"],
    "SO": ["Somalia"],
    "BN": ["Brunei"],
    "CV": ["Cape Verde"],
    "SZ": ["Swaziland"],
    "TL": ["East Timor"],
    "PS": ["Palestine", "Gaza", "West Bank"],
    "NR": ["Nauru"],
    "LC": ["Saint Lucia"],
    "KN": ["Saint Kitts and Nevis"],
    "VC": ["Saint Vincent and the Grenadines"],
    "PR": ["Puerto Rico"],
    "HK": ["Hong Kong", "HKMA"],
    "MO": ["Macau", "Macao"],
    "GE": ["Tbilisi"],
    "JO": ["Amman"],
    "TD": ["N'Djamena"],
}
# Euro-area institutions tag every member: the ECB sets each one's policy rate.
_EURO_ALIASES = ["European Central Bank", "ECB", "Lagarde", "eurozone", "euro zone", "euro area", "Eurozone"]
# Longer phrases that contain a country token but are not about that country.
_SENTINELS = ["Latin America", "South America", "North America", "Central America", "Indian Ocean",
              "Bank of America", "American Express", "South China Sea", "East China Sea", "Guinea pig",
              "New Mexico", "New England", "Jersey City", "New Jersey"]


def _clean_official(name: str) -> str | None:
    if "," in name or "(" in name:
        return None
    return name


def _build_country_regex():
    entries: dict[str, list[str] | None] = {}
    for cc, name, *_ in ALL_COUNTRIES:
        clean = _clean_official(name)
        if clean and cc not in _SKIP_OFFICIAL:
            entries[clean] = [cc]
    for cc, aliases in _ALIASES.items():
        for a in aliases:
            entries[a] = [cc]
    for a in _EURO_ALIASES:
        entries[a] = EURO_MEMBERS
    for a in _SENTINELS:
        entries[a] = None
    # Longest first so "Papua New Guinea" wins over "Guinea", "South Korea" over "Korea".
    keys = sorted(entries, key=len, reverse=True)
    pattern = re.compile(r"(?<![\w.])(" + "|".join(re.escape(k) for k in keys) + r")(?![\w])")
    return pattern, entries


_COUNTRY_RE, _COUNTRY_MAP = _build_country_regex()


def match_countries(text: str) -> list[str]:
    found: list[str] = []
    for m in _COUNTRY_RE.finditer(text):
        for cc in _COUNTRY_MAP[m.group(1)] or []:
            if cc not in found:
                found.append(cc)
    return found


# ------------------------------------------------------------------ lexicons --
def _words(*terms: str) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(terms) + r")\b", re.IGNORECASE)


SECTOR_LEXICON = {
    "copper": _words("copper", "cathodes?", "Codelco", "Freeport", "Antofagasta", "Kamoa", "Grasberg", "Escondida"),
    "lithium": _words("lithium", "spodumene", "Albemarle", "SQM", "Pilbara", "Ganfeng", "Tianqi", "battery[- ]grade"),
    "nickel": _words("nickel", "Tsingshan", "HPAL", "Norilsk", "Nornickel", "laterite"),
    "rare_earths": _words("rare[- ]earths?", "neodymium", "dysprosium", "praseodymium", "terbium", "MP Materials", "Lynas", "permanent magnets?"),
    "cobalt": _words("cobalt", "CMOC", "Kamoto", "Glencore cobalt"),
    "semiconductors": _words("semiconductors?", "chips?", "chipmakers?", "foundr(?:y|ies)", "wafers?", "fabs?",
                             "TSMC", "Nvidia", "Intel", "SK Hynix", "Samsung Electronics", "Micron", "ASML", "AMD",
                             "Qualcomm", "Broadcom", "DRAM", "NAND", "HBM", "GPUs?", "lithography", "SMIC", "MediaTek"),
}

POLICY_RE = _words("interest rates?", "rate (?:cut|hike|rise|increase|decision)s?", "policy rate", "monetary policy",
                   "central bank", "basis points?", "bps", "inflation target", "quantitative (?:easing|tightening)",
                   "FOMC", "Federal Reserve", "ECB", "Bank of England", "Bank of Japan", "PBOC", "RBI", "RBA",
                   "export (?:controls?|bans?|restrictions?|curbs?|licen[cs]es?)", "sanctions?", "capital controls?",
                   "regulat(?:or|ors|ion|ions|ory)", "SEC", "CFTC", "tariffs?", "rate-setting", "MPC")
MARKET_RE = _words("shares?", "stocks?", "equit(?:y|ies)", "earnings", "profits?", "revenues?", "futures",
                   "prices?", "output", "production", "mines?", "mining", "miners?", "supply", "demand", "ETF")

SUBTYPE_LEXICON = {
    "interest_rate": _words("interest rates?", "rate (?:cut|hike|rise|increase|decision)s?", "policy rate",
                            "basis points?", "bps", "FOMC", "monetary policy", "MPC", "rate-setting"),
    "market_regulation": _words("regulat(?:or|ors|ion|ions|ory)", "SEC", "CFTC", "oversight", "compliance",
                                "position limits?", "supervis(?:ion|ory)"),
    "export_control": _words("export (?:controls?|bans?|restrictions?|curbs?|licen[cs]es?)", "entity list",
                             "tariffs?", "sanctions?", "embargo"),
    "capital_controls": _words("capital controls?", "currency controls?", "FX controls?", "capital outflows?",
                               "foreign exchange restrictions?"),
}

# Hawkish/dovish dictionaries in the spirit of Apel & Blix Grimaldi (2014).
HAWKISH_RE = _words("hikes?", "hiked", "hiking", "raises? rates?", "raised rates?", "tighten(?:s|ed|ing)?",
                    "hawkish", "restrictive", "higher for longer", "rate increases?", "quantitative tightening")
DOVISH_RE = _words("cuts?", "cutting", "lowers? rates?", "lowered rates?", "eas(?:e|es|ed|ing)", "dovish",
                   "accommodative", "stimulus", "quantitative easing", "rate reductions?")

# Finance-specific polarity words (subset of Loughran & McDonald, 2011, plus
# market-move verbs common in commodity headlines).
POSITIVE_RE = _words("gains?", "gained", "rises?", "rose", "rall(?:y|ies|ied)", "surg(?:e|es|ed)", "jump(?:s|ed)?",
                     "record high", "beats?", "strong(?:er)?", "growth", "boost(?:s|ed)?", "upgrades?", "upgraded",
                     "rebound(?:s|ed)?", "recover(?:s|ed|y)?", "soar(?:s|ed)?", "optimis(?:m|tic)", "outperform(?:s|ed)?")
NEGATIVE_RE = _words("falls?", "fell", "drops?", "dropped", "slump(?:s|ed)?", "plung(?:e|es|ed)", "declin(?:e|es|ed)",
                     "loss(?:es)?", "weak(?:er|ness)?", "downgrades?", "downgraded", "risks?", "concerns?", "fears?",
                     "crisis", "slowdown", "tumbl(?:e|es|ed)", "sinks?", "sank", "warns?", "warned", "losses",
                     "underperform(?:s|ed)?", "recession", "default(?:s|ed)?")

MECHANISM_LEXICON = {
    "cost_of_carry": _words("inventor(?:y|ies)", "stockpiles?", "storage", "warehouses?", "carry trade", "cost of carry"),
    "usd_value": _words("dollar", "greenback", "DXY", "USD"),
    "investment_substitution": _words("treasury yields?", "bond yields?", "yields?", "safe[- ]haven"),
    "extraction_incentives": _words("output", "production", "mine supply", "extraction", "mining output"),
}


def _hits(pattern: re.Pattern, text: str) -> int:
    return len(pattern.findall(text))


def tag(title: str, summary: str, source_category: str = "", source_name: str = "") -> dict:
    text = f"{title}. {summary or ''}"
    sectors = [s for s, rx in SECTOR_LEXICON.items() if rx.search(text)]
    policy, market = _hits(POLICY_RE, text), _hits(MARKET_RE, text)

    # Central-bank feeds publish policy communication; otherwise the larger
    # lexicon score wins, and an unclassifiable article defaults to the source's
    # side (both categories are mandatory in the schema, as for the LLM).
    if source_category == "central_bank" or policy > market or (policy and not sectors):
        category = "monetary_policy"
    else:
        category = "commodity_stocks"

    pos, neg = _hits(POSITIVE_RE, text), _hits(NEGATIVE_RE, text)
    sentiment = round((pos - neg) / max(1, pos + neg), 3)

    if category == "monetary_policy":
        subtypes = [k for k, rx in SUBTYPE_LEXICON.items() if rx.search(text)]
        hawk, dove = _hits(HAWKISH_RE, text), _hits(DOVISH_RE, text)
        stance = "contractionary" if hawk > dove else "expansionary" if dove > hawk else "neutral"
    else:
        subtypes, stance = [], "neutral"

    return {
        "article_category": category,
        # Fall back to the publisher ("Bank of England" -> GB, "European
        # Central Bank" -> euro members) when the text names no country.
        "country_codes": match_countries(text) or match_countries(source_name),
        "sector_tags": sectors if category == "commodity_stocks" or sectors else [],
        "policy_subtype": subtypes,
        "sentiment_score": sentiment,
        "sentiment_target": sectors[0] if sectors else "",
        "mechanisms": [k for k, rx in MECHANISM_LEXICON.items() if rx.search(text)],
        "policy_stance": stance,
    }
