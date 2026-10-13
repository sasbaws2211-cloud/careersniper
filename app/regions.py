"""Where a remote job can be done from, and whether a job is remote at all.

workplace_of: 'remote' | 'hybrid' | 'onsite' | '' from what the source says, else from the job's own text.
remote_regions_of: the places a REMOTE job names, as '|WORLD|' / '|US|' / '|EMEA|' ('' when it only says 'Remote'). Only what the post
names is stored: 'Remote - Canada' is '|CA|', not also North America, or a US searcher (whose region groups include North America)
would be shown a job that needs a Canadian. Groups are expanded when searching (match_tokens), not when storing.
"""
import re

from . import classify

GROUPS = {
    "NAMER": ("North America", "US CA MX"),
    "LATAM": ("Latin America", "MX BR AR CL CO PE UY EC BO PY VE CR PA GT HN SV NI DO PR JM TT BS"),
    "EU": ("European Union", "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE"),
    "EUROPE": ("Europe", "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE GB CH NO IS UA RS BA MK ME AL XK MD BY"),
    "EMEA": ("EMEA", "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE GB CH NO IS UA RS BA MK ME AL XK MD BY "
                     "IL AE SA QA KW BH OM JO LB IQ IR TR EG MA TN DZ NG GH KE ZA ET TZ UG RW SN CI CM ZM ZW BW NA MZ AO MU"),
    "AFRICA": ("Africa", "EG MA TN DZ NG GH KE ZA ET TZ UG RW SN CI CM ZM ZW BW NA MZ AO MU"),
    "APAC": ("Asia-Pacific", "IN PK BD LK NP AF CN HK TW MO JP KR MN SG MY TH VN PH ID KH MM BN AU NZ FJ"),
}
GROUPS = {k: (name, set(members.split())) for k, (name, members) in GROUPS.items()}
GROUPS["AMER"] = ("Americas", GROUPS["NAMER"][1] | GROUPS["LATAM"][1])
GROUP_ORDER = ("WORLD", "AFRICA", "NAMER", "AMER", "LATAM", "EUROPE", "EU", "EMEA", "APAC")
REGION_NAMES = {"WORLD": "Anywhere in the world", **{k: v[0] for k, v in GROUPS.items()}}

# words a location uses for a region, longest first so 'north america' wins over 'america'
_WORDS = (
    ("WORLD", r"worldwide|world[- ]wide|anywhere|global(?:ly)?|international|everywhere|any ?where|work from anywhere|fully distributed|distributed"),
    ("NAMER", r"north america|namer|\bna\b"), ("AMER", r"americas|\bamer\b"), ("LATAM", r"latam|latin america|south america|central america"),
    ("AFRICA", r"(?<!south )\bafrica\b"),     # 'South Africa' names one country, not the continent
    ("EU", r"\beu\b|european union"), ("EUROPE", r"europe(?:an)?"), ("EMEA", r"emea"), ("APAC", r"apac|asia[- ]pacific|\basia\b|oceania"),
)
_WORDS = tuple((k, re.compile(p, re.I)) for k, p in _WORDS)
_REMOTE = re.compile(r"\bremote(?:ly)?\b|\bwork from home\b|\bwfh\b|\btelecommut|\bhome[- ]based\b|\bhome office\b|\bwork from anywhere\b", re.I)
_HYBRID = re.compile(r"\bhybrid\b|\bflex(?:ible)? (?:work|location)\b", re.I)
_ONSITE = re.compile(r"\bon[- ]?site\b|\bin[- ]office\b|\bin[- ]person\b", re.I)


def groups_of(code: str) -> set[str]:
    """The region groups a country belongs to."""
    return {g for g, (_, members) in GROUPS.items() if code in members}


def normalize_workplace(raw) -> str:
    """The source's own workplace value ('Remote', 'OnSite', 'hybrid', 'Flex', 'TELECOMMUTE', 'on-site', 1/2/0 ...) as a key, else ''."""
    v = str(raw if raw is not None else "").strip().lower().replace("_", "-")
    if v in ("remote", "telecommute", "fully-remote", "remote-first", "work-from-home", "wfh", "true"):
        return "remote"
    if v in ("hybrid", "flex", "flexible", "mixed"):
        return "hybrid"
    if v in ("onsite", "on-site", "on site", "office", "in-office", "in-person", "onsite-only"):
        return "onsite"
    return ""


def workplace_of(explicit=None, location: str = "", title: str = "", remote_flag: bool = False) -> str:
    """remote | hybrid | onsite | ''. The source's own value wins; then the words in the location and title ('hybrid' before
    'remote', since 'Hybrid - London, or Remote' is a hybrid role); a bare legacy remote flag is the last resort."""
    got = normalize_workplace(explicit)
    if got:
        return got
    text = f"{location or ''} | {title or ''}"
    if _HYBRID.search(text):
        return "hybrid"
    if _REMOTE.search(text):
        return "remote"
    if remote_flag:
        return "remote"
    return "onsite" if _ONSITE.search(text) else ""


def remote_regions_of(location: str, workplace: str) -> str:
    """'|US|NAMER|AMER|' style set of where a remote job is open to; '' for non-remote jobs and for a bare 'Remote'."""
    if workplace != "remote":
        return ""
    tokens: list[str] = []

    def add(t):
        if t not in tokens:
            tokens.append(t)
    loc = location or ""
    for key, rx in _WORDS:
        if rx.search(loc):
            add(key)
    for code in classify.countries_of(loc):
        add(code)
    return "|" + "|".join(tokens) + "|" if tokens else ""


def match_tokens(selected: str) -> set[str]:
    """The stored tokens that make a remote job open to someone in `selected` (a country code or a region group).
    A country: that country, worldwide jobs, and any region group the country is in. A group: the group itself, worldwide jobs,
    any wider group, and every member country (a 'Remote - France' job is open to someone searching Europe)."""
    out = {selected, "WORLD"}
    if selected in GROUPS:
        members = GROUPS[selected][1]
        out |= {g for g, (_, m) in GROUPS.items() if m >= members and g != selected}      # a wider group covers this one
        out |= members
    else:
        out |= groups_of(selected)
    return out


def region_options(counts: dict[str, int], limit_countries: int = 40) -> list[tuple[str, str]]:
    """[(key, label)] for the 'Remote from' dropdown: region groups that have jobs, then countries by number of remote jobs.
    `counts` is per stored token; a country's jobs also count toward every group it belongs to, as they would when searching."""
    counts = dict(counts)
    for token, n in list(counts.items()):
        for g in groups_of(token):
            counts[g] = counts.get(g, 0) + n
    out = [(g, REGION_NAMES[g]) for g in GROUP_ORDER if counts.get(g)]
    countries = sorted(((c, n) for c, n in counts.items() if c not in REGION_NAMES), key=lambda kv: (-kv[1], kv[0]))
    out += [(c, classify.COUNTRY_NAMES.get(c, c)) for c, _ in countries[:limit_countries]]
    return out


VALID_REGIONS = set(REGION_NAMES) | set(classify.COUNTRY_NAMES)
