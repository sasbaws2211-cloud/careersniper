"""Derive clean, filterable fields from the messy text employers publish.

Everything here is a pure function of the job's own text, so it runs at scrape time and can be re-run over old rows.
Unknown stays empty ('') rather than guessed: a wrong country or seniority hides a job from the people it is for.
"""
import re

# ---------------------------------------------------------------- countries
COUNTRIES = """
US:United States|United States of America|USA|U.S.A.|U.S.|US
GB:United Kingdom|UK|U.K.|Great Britain|England|Scotland|Wales|Northern Ireland
CA:Canada
AU:Australia
NZ:New Zealand
IE:Ireland
DE:Germany|Deutschland
FR:France
ES:Spain|España
PT:Portugal
IT:Italy|Italia
NL:Netherlands|The Netherlands|Holland
BE:Belgium
LU:Luxembourg
CH:Switzerland
AT:Austria
SE:Sweden
NO:Norway
DK:Denmark
FI:Finland
IS:Iceland
PL:Poland
CZ:Czech Republic|Czechia
SK:Slovakia
HU:Hungary
RO:Romania
BG:Bulgaria
GR:Greece
HR:Croatia
RS:Serbia
SI:Slovenia
BA:Bosnia and Herzegovina|Bosnia
MK:North Macedonia
ME:Montenegro
AL:Albania
XK:Kosovo
MD:Moldova
BY:Belarus
UA:Ukraine
EE:Estonia
LV:Latvia
LT:Lithuania
TR:Turkey|Türkiye|Turkiye
IL:Israel
AE:United Arab Emirates|UAE
SA:Saudi Arabia|KSA
QA:Qatar
KW:Kuwait
BH:Bahrain
OM:Oman
JO:Jordan
LB:Lebanon
IQ:Iraq
IR:Iran
EG:Egypt
MA:Morocco
TN:Tunisia
DZ:Algeria
NG:Nigeria
GH:Ghana
KE:Kenya
ZA:South Africa
ET:Ethiopia
TZ:Tanzania
UG:Uganda
RW:Rwanda
SN:Senegal
CI:Ivory Coast|Cote d'Ivoire|Côte d'Ivoire
CM:Cameroon
ZM:Zambia
ZW:Zimbabwe
BW:Botswana
NA:Namibia
MZ:Mozambique
AO:Angola
MU:Mauritius
IN:India
PK:Pakistan
BD:Bangladesh
LK:Sri Lanka
NP:Nepal
AF:Afghanistan
CN:China|PRC|People's Republic of China
HK:Hong Kong|Hong Kong SAR
TW:Taiwan
MO:Macau
JP:Japan
KR:South Korea|Korea|Republic of Korea
MN:Mongolia
SG:Singapore
MY:Malaysia
TH:Thailand
VN:Vietnam|Viet Nam
PH:Philippines
ID:Indonesia
KH:Cambodia
MM:Myanmar
BN:Brunei
UZ:Uzbekistan
KZ:Kazakhstan
AZ:Azerbaijan
AM:Armenia
CY:Cyprus
MT:Malta
MX:Mexico|México
BR:Brazil|Brasil
AR:Argentina
CL:Chile
CO:Colombia
PE:Peru
UY:Uruguay
EC:Ecuador
BO:Bolivia
PY:Paraguay
VE:Venezuela
CR:Costa Rica
PA:Panama
GT:Guatemala
HN:Honduras
SV:El Salvador
NI:Nicaragua
DO:Dominican Republic
PR:Puerto Rico
JM:Jamaica
TT:Trinidad and Tobago
BS:Bahamas
FJ:Fiji
"""

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
CA_PROVINCES = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick", "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia", "NT": "Northwest Territories", "NU": "Nunavut", "ON": "Ontario", "PE": "Prince Edward Island",
    "QC": "Quebec", "SK": "Saskatchewan", "YT": "Yukon",
}
ALPHA3 = dict(GBR="GB", IND="IN", CAN="CA", AUS="AU", DEU="DE", FRA="FR", ESP="ES", ITA="IT", NLD="NL", IRL="IE", SGP="SG", JPN="JP",
              BRA="BR", MEX="MX", POL="PL", CHN="CN", KOR="KR", ARE="AE", ZAF="ZA", NGA="NG", KEN="KE", PHL="PH", IDN="ID", MYS="MY",
              THA="TH", VNM="VN", HKG="HK", TWN="TW", SWE="SE", NOR="NO", DNK="DK", FIN="FI", CHE="CH", AUT="AT", BEL="BE", PRT="PT",
              GRC="GR", TUR="TR", ISR="IL", EGY="EG", PAK="PK", BGD="BD", ARG="AR", CHL="CL", COL="CO", PER="PE", NZL="NZ", ROU="RO",
              CZE="CZ", HUN="HU", UKR="UA", USA="US", ISL="IS", LKA="LK", NPL="NP", GHA="GH", UGA="UG", RWA="RW", MAR="MA", SAU="SA")

# Well-known cities that employers list on their own, with no state or country.
CITIES = """
US:San Francisco Bay Area|SF Bay Area|Bay Area|Silicon Valley|Ann Arbor|New York|New York City|NYC|San Francisco|SF|Los Angeles|Chicago|Seattle|Austin|Boston|Denver|Atlanta|Dallas|Houston|Miami|San Diego|San Jose|Santa Clara|Sunnyvale|Mountain View|Palo Alto|Menlo Park|Redwood City|San Mateo|Oakland|Brooklyn|Philadelphia|Phoenix|Portland|Washington DC|Washington D.C.|Minneapolis|Detroit|Nashville|Charlotte|Raleigh|Salt Lake City|Pittsburgh|Cambridge|Irvine|Cupertino|Fremont|Plano|Tampa|Orlando|Las Vegas|Sacramento|Columbus|Indianapolis|Kansas City|St. Louis|Baltimore|Boulder
GB:London|Manchester|Edinburgh|Glasgow|Birmingham|Bristol|Leeds|Belfast|Cardiff|Liverpool
IE:Dublin|Cork|Galway|Limerick
CA:Toronto|Vancouver|Montreal|Montréal|Ottawa|Calgary|Edmonton|Waterloo|Kitchener|Mississauga|Winnipeg|Quebec City|Halifax
AU:Sydney|Melbourne|Brisbane|Perth|Adelaide|Canberra
NZ:Auckland|Wellington|Christchurch
DE:Berlin|Munich|München|Hamburg|Frankfurt|Cologne|Köln|Stuttgart|Düsseldorf|Dusseldorf|Leipzig
FR:Paris|Lyon|Marseille|Toulouse|Nice|Nantes|Bordeaux|Lille|Strasbourg
NL:Amsterdam|Rotterdam|Utrecht|Eindhoven|The Hague|Delft
BE:Brussels|Antwerp|Ghent
CH:Zurich|Zürich|Geneva|Basel|Lausanne
AT:Vienna|Wien|Graz
ES:Madrid|Barcelona|Valencia|Seville|Malaga|Bilbao
PT:Lisbon|Lisboa|Porto
IT:Milan|Milano|Rome|Roma|Turin|Torino|Bologna
SE:Stockholm|Gothenburg|Malmö|Malmo
NO:Oslo|Bergen
DK:Copenhagen|København|Aarhus
FI:Helsinki|Espoo|Tampere
PL:Warsaw|Krakow|Kraków|Wroclaw|Wrocław|Gdansk|Gdańsk|Poznan
CZ:Prague|Brno
HU:Budapest
RO:Bucharest|Cluj-Napoca|Cluj
GR:Athens|Thessaloniki
TR:Istanbul|Ankara
GE:Tbilisi
RS:Belgrade|Novi Sad
LT:Vilnius|Kaunas
LV:Riga
EE:Tallinn
HR:Zagreb
BG:Sofia
UA:Kyiv|Kiev|Lviv
XK:Pristina
AL:Tirana
AZ:Baku
IL:Tel Aviv|Tel Aviv-Yafo|Jerusalem|Haifa
AE:Dubai|Abu Dhabi
SA:Riyadh|Jeddah
EG:Cairo
NG:Lagos|Abuja
KE:Nairobi
ZA:Cape Town|Johannesburg|Pretoria|Durban
GH:Accra
IN:Bangalore|Bengaluru|Mumbai|Delhi|New Delhi|Hyderabad|Pune|Chennai|Gurgaon|Gurugram|Noida|Kolkata|Ahmedabad|Jaipur|Kochi|Mohali
PK:Karachi|Lahore|Islamabad
BD:Dhaka
SG:Singapore
MY:Kuala Lumpur
TH:Bangkok
VN:Ho Chi Minh City|Hanoi
PH:Manila|Makati|Cebu|Taguig
ID:Jakarta|Bali
JP:Tokyo|Osaka|Kyoto|Yokohama
KR:Seoul
CN:Shanghai|Beijing|Shenzhen|Guangzhou|Hangzhou|Chengdu
HK:Hong Kong
TW:Taipei
MX:Mexico City|Guadalajara|Monterrey|Ciudad de México
BR:Sao Paulo|São Paulo|Rio de Janeiro|Brasilia|Belo Horizonte|Curitiba
AR:Buenos Aires
CL:Santiago
CO:Bogota|Bogotá|Medellin|Medellín
PE:Lima
UY:Montevideo
PR:San Juan
"""


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower().replace("’", "'")).strip(" .")


def _table(block: str) -> dict:
    out = {}
    for line in block.strip().splitlines():
        code, names = line.split(":", 1)
        for n in names.split("|"):
            out.setdefault(_clean(n), code)
    return out


_COUNTRY = _table(COUNTRIES)
_CITY = _table(CITIES)
COUNTRY_NAMES = {line.split(":", 1)[0]: line.split(":", 1)[1].split("|")[0] for line in COUNTRIES.strip().splitlines()}
COUNTRY_NAMES["GE"] = "Georgia (country)"      # only ever matched via its capital: the bare name is also a US state
_NAMES_BY_LEN = sorted((n for n in _COUNTRY if len(n) > 3), key=len, reverse=True)   # for "CA United States" style suffixes
_STATE_NAMES = {_clean(n): "US" for n in US_STATES.values()} | {_clean(n): "CA" for n in CA_PROVINCES.values()}
_US_CODES, _CA_CODES = set(US_STATES), set(CA_PROVINCES)
_SPLIT = re.compile(r"\s*[,;/|()\[\]:\-–—&]+\s*|\s+\.\s*|\s+(?:or|and)\s+|(?<=[A-Z]{3})\.(?=[A-Z])|(?<=[A-Z]{2})\.(?=[A-Za-z])")
_FILLER = re.compile(r"(?i)\b(remote|hybrid|hq|headquarters|offices?|on-?site|in-office|based|campus)\b")
_UPPER_TAIL = re.compile(r"\b(US|UK|UAE|USA|KSA)$")


def _token_country(t: str, cities: list[str]) -> str:
    """The country (or US state / Canadian province, hence country) a single place-name token points at, else ''."""
    c = _clean(t)
    if c == "georgia" and "GE" in cities:                  # the country and the US state share a name; only the city tells
        return "GE"
    if c in _STATE_NAMES:
        return _STATE_NAMES[c]
    if t.isupper() and t in ALPHA3:
        return ALPHA3[t]
    if c in _COUNTRY and (len(c) > 2 or t.isupper()):
        return _COUNTRY[c]
    for name in _NAMES_BY_LEN:                           # "CA United States", "Remote London UK" style endings
        if c.endswith(" " + name) and len(c) > len(name):
            return _COUNTRY[name]
    for name, code in _STATE_NAMES.items():
        if c.endswith(" " + name) and len(c) > len(name):
            return code
    m = _UPPER_TAIL.search(t)
    if m and len(t) > len(m.group(1)) + 1:
        return _COUNTRY[_clean(m.group(1))]
    if len(t) == 2 and t.isupper():
        return "CA" if t in _CA_CODES else ("US" if t in _US_CODES else "")
    m = re.search(r"\b([A-Z]{2})$", t)                   # "San Mateo CA"
    if m and len(t) > 3 and m.group(1) in _US_CODES:
        return "US"
    return ""


def countries_of(location: str) -> list[str]:
    """Every ISO 3166 alpha-2 country a free-text location names, in order, without repeats; [] when it cannot be told.
    'Remote - Sweden'->[SE], 'USA.VA.Reston'->[US], 'Ireland / United Kingdom'->[IE, GB], 'Bangalore; Noida'->[IN].
    A place is read from explicit countries / states / province codes first; well-known city names are only the
    fallback, so 'Birmingham, AL' is the US while a bare 'Birmingham' is the UK."""
    out: list[str] = []
    for segment in re.split(r"\s*;\s*|\s+\+\d+\s", (location or "").strip()):
        segment = re.sub(r"(?i)\bu\.s\.a?\.?", "USA", segment)          # U.S. / U.S.A. would be split on the dots
        tokens = []
        for t in _SPLIT.split(segment):
            t = re.sub(r"\s+", " ", _FILLER.sub(" ", t or "")).strip()
            if t:
                tokens.append(t)
        cities = [_CITY[_clean(t)] for t in tokens if _clean(t) in _CITY]
        found = [code for t in tokens if (code := _token_country(t, cities))]
        for code in found or cities:
            if code not in out:
                out.append(code)
    return out


def country_of(location: str) -> str:
    """The first country of countries_of(), or ''."""
    found = countries_of(location)
    return found[0] if found else ""


def country_codes(location: str) -> str:
    """The stored form: '|IE|GB|' so 'contains |IE|' is an exact membership test; '' when unknown."""
    found = countries_of(location)
    return "|" + "|".join(found) + "|" if found else ""


# ---------------------------------------------------------------- job type
def job_type_of(employment_type: str, title: str = "") -> str:
    """full_time | part_time | contract | internship | temporary, or '' when the employer says nothing."""
    e, t = _clean(employment_type or ""), _clean(title or "")
    if re.search(r"\bintern(ship)?s?\b|\bco-?op\b|\bapprentice", e) or re.search(r"\bintern(ship)?\b|\bco-?op\b|\bworking student\b", t):
        return "internship"
    if re.search(r"part[- ]?time", e) and not re.search(r"full[- ]?time", e):
        return "part_time"
    if re.search(r"contract|freelance|consultant|fixed[- ]?term", e):
        return "contract"
    if re.search(r"seasonal|temp(orary)?\b", e):
        return "temporary"
    if re.search(r"full[- ]?time|permanent|regular|\bft\b", e):
        return "full_time"
    if re.search(r"part[- ]?time", t):
        return "part_time"
    if re.search(r"\bcontract(or)?\b|\bfreelance\b|\b\d+[- ]month contract\b", t):
        return "contract"
    if re.search(r"\btemporary\b|\bseasonal\b", t):
        return "temporary"
    return ""


# ---------------------------------------------------------------- seniority
_LEVELS = (
    ("intern", r"\bintern(ship)?s?\b|\bapprentice(ship)?\b|\btrainee\b|\bco-?op\b|\bworking student\b"),
    ("executive", r"\bchief\b|\bc[etfmoi]o\b|\bpresident\b|\b(s|e)vp\b|\bsvp\b|\bgeneral counsel\b|\bfounder\b"),
    ("director", r"\bdirector\b|\bvp\b|\bvice president\b|\bhead of\b"),
    ("manager", r"\bmanager\b|\bsupervisor\b|\bsuperintendent\b|\bmanaging\b"),
    ("lead", r"\blead\b|\bteam lead\b|\btech lead\b|\bprincipal\b"),
    ("senior", r"\bsenior\b|\bsr\b|\bstaff\b|\bexpert\b|\biii\b|\biv\b"),
    ("entry", r"\bjunior\b|\bjr\b|\bentry[- ]level\b|\bgraduate\b|\bnew grad\b|\bearly career\b|\bassociate engineer\b|\bi\b(?= *$)"),
)
_LEVELS = tuple((n, re.compile(p)) for n, p in _LEVELS)


def seniority_of(title: str) -> str:
    """intern | entry | senior | lead | manager | director | executive from the job title; '' for plain mid-level titles."""
    t = _clean(re.sub(r"[.,]", " ", title or ""))
    for name, rx in _LEVELS:
        if rx.search(t):
            return name
    return ""


# ---------------------------------------------------------------- pay
# Approximate US dollars per unit of currency (October 2026). Good enough to sort jobs into salary bands, not for accounting;
# edit here when the rates drift. A currency missing from the table means the job simply has no filterable pay.
USD_PER = dict(USD=1.0, CAD=0.73, EUR=1.08, GBP=1.27, AUD=0.65, NZD=0.60, CHF=1.12, SGD=0.74, HKD=0.128, JPY=0.0067, KRW=0.00073,
               CNY=0.14, TWD=0.031, INR=0.012, SEK=0.095, NOK=0.093, DKK=0.145, PLN=0.25, CZK=0.043, HUF=0.0027, RON=0.22, BGN=0.55,
               TRY=0.03, ILS=0.27, AED=0.272, SAR=0.267, QAR=0.275, ZAR=0.054, NGN=0.00065, KES=0.0077, EGP=0.02, BRL=0.18,
               MXN=0.052, ARS=0.001, CLP=0.00105, COP=0.00025, PEN=0.27, MYR=0.22, THB=0.028, PHP=0.0175, IDR=0.000063, VND=0.00004)
HOURS_PER_YEAR = 2080
PERIOD_FACTOR = dict(year=1, month=12, week=52, day=260, hour=HOURS_PER_YEAR)
_PERIOD_WORDS = (("hour", r"hour|hourly|\bhr\b|/hr"), ("month", r"month"), ("week", r"week"), ("day", r"\bday\b|daily"),
                 ("year", r"annual|year|yearly|per[- ]annum|\bpa\b"))
_PERIOD_WORDS = tuple((n, re.compile(p, re.I)) for n, p in _PERIOD_WORDS)
MAX_PLAUSIBLE_USD = 3_000_000         # above this a "salary" is a data error, not a salary


def period_of(text: str | None) -> str:
    """year | month | week | day | hour from an interval or a pay-range title ('per-hour-wage', '1 MONTH', 'Annual Pay Range'),
    or '' when the text does not say."""
    t = text or ""
    for name, rx in _PERIOD_WORDS:
        if rx.search(t):
            return name
    return ""


# Currencies where an unlabelled small number can safely be read as an hourly rate or a monthly stipend. For the rest a small
# number is just as likely a yearly salary in a low-wage economy, so it is taken as yearly (and dropped if that is absurd).
HARD_CURRENCIES = {"USD", "EUR", "GBP", "CAD", "AUD", "NZD", "CHF", "SGD", "SEK", "NOK", "DKK", "HKD"}
MIN_PLAUSIBLE_USD = 3_000             # below this a yearly "salary" is a data error or a monthly figure we could not recognise
UNLABELLED_FLOOR_USD = 8_000          # an unlabelled yearly figure under this in a soft currency may well be monthly: left out
# If a stated period gives a per-period amount above this (in USD), the employer mislabelled it: the number is really yearly
MAX_RANGE_RATIO = 10                  # a posted range whose top is over 10x its bottom is an employer typo ('7,100 - 89,100')
_MISLABEL_ABOVE = dict(hour=1_000, day=3_000, week=10_000, month=40_000)


def annual_usd(lo, hi, currency: str, period: str = "") -> tuple[int | None, int | None]:
    """The pay range as whole US dollars per year: (None, None) when there is no pay, (0, 0) when there is pay that cannot
    be converted (unknown currency, or a number that fits no sensible reading). A stated period is trusted unless the figure
    is absurd for it ('$88,000 per month'), when it is taken as yearly. When no period is stated, a major-currency amount up
    to $300 is read as hourly, $1,000 to $10,000 as monthly, and more as yearly; $300 to $1,000 is ambiguous (a day rate?)
    and is left out, as are small unlabelled amounts in other currencies, rather than guessed."""
    lo, hi = (float(lo) if lo else 0.0), (float(hi) if hi else 0.0)
    if not lo and not hi:
        return None, None
    lo, hi = (lo or hi), (hi or lo)
    if lo > hi:
        lo, hi = hi, lo
    cur = (currency or "").strip().upper()
    rate = USD_PER.get(cur)
    if not rate or hi > lo * MAX_RANGE_RATIO:       # a range this wide means the two ends are in different units
        return 0, 0
    u = lo * rate
    factor = PERIOD_FACTOR.get(period)
    if factor and u > _MISLABEL_ABOVE.get(period, float("inf")):
        factor = 1
    if not factor:
        if cur not in HARD_CURRENCIES:
            if u < UNLABELLED_FLOOR_USD:
                return 0, 0
            factor = 1
        elif u <= 300:
            factor = HOURS_PER_YEAR
        elif u < 1000:
            return 0, 0
        elif u < 10_000:
            factor = 12
        else:
            factor = 1
    a, b = lo * factor * rate, hi * factor * rate
    if a < MIN_PLAUSIBLE_USD or b > MAX_PLAUSIBLE_USD:
        return 0, 0
    return int(round(a)), int(round(b))


JOB_TYPES = (("full_time", "Full-time"), ("part_time", "Part-time"), ("contract", "Contract"), ("internship", "Internship"),
             ("temporary", "Temporary / seasonal"))
SENIORITIES = (("intern", "Intern / trainee"), ("entry", "Entry level"), ("senior", "Senior"), ("lead", "Lead / principal"),
               ("manager", "Manager"), ("director", "Director / VP"), ("executive", "Executive"))


def classify(d: dict) -> dict:
    """The derived columns for a parsed job dict (title, location, employment_type, salary_min/max/currency/period)."""
    lo, hi = annual_usd(d.get("salary_min"), d.get("salary_max"), d.get("salary_currency") or "", d.get("salary_period") or "")
    from . import regions, visa           # these import this module, so they are loaded on first use
    workplace = regions.workplace_of(d.get("workplace"), d.get("location", ""), d.get("title", ""), bool(d.get("remote")))
    return dict(countries=country_codes(d.get("location", "")), job_type=job_type_of(d.get("employment_type", ""), d.get("title", "")),
                seniority=seniority_of(d.get("title", "")), pay_min_usd=lo, pay_max_usd=hi, workplace=workplace,
                remote=workplace == "remote", remote_regions=regions.remote_regions_of(d.get("location", ""), workplace),
                visa=visa.visa_of(d.get("description")))
