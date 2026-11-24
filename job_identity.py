"""
Job identity, normalization and classification helpers.

Pure functions only (no network, no file I/O) so they are easy to unit-test and are shared by
the crawler, the seen_jobs migration and the tests.

Identity rules (see CLAUDE.md):
- A real ATS / board posting ID is the strongest identity: ("ats", provider, id).
- Otherwise: normalized company + normalized title + coarse location + canonical URL.
- A shared application form (e.g. one monday.com form for several roles) is NEVER an ID,
  so distinct titles behind the same form stay distinct jobs.
"""

import hashlib
import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse, unquote

# ---------------------------------------------------------------------------
# Text / company / title / location normalization
# ---------------------------------------------------------------------------

MOJIBAKE_RE = re.compile(r"[\xd7][\x80-\xbf]")


def repair_mojibake(s):
    """
    Hebrew UTF-8 bytes decoded as Latin-1 look like '×\\x9e×¤×ª×\\x97'. Reverse it when that pattern is present
    and the round-trip is lossless; otherwise return the input unchanged.
    """
    if not s or not MOJIBAKE_RE.search(s):
        return s
    try:
        fixed = s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    return fixed if re.search(r"[֐-׿]", fixed) else s


def normalize_text(s):
    s = repair_mojibake(s or "")
    s = s.replace("‎", "").replace("‏", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


COMPANY_SUFFIX_RE = re.compile(
    r"\b(ltd|ltd\.|inc|inc\.|llc|plc|corp|corporation|co\.|technologies|technology|group|israel|il|"
    r"labs|software|systems|the|בע\"מ|בעמ)\b", re.I)

COMPANY_ALIASES = {
    "gongio": "gong", "hailoai": "hailo", "nanoxvision": "nanox", "towersemiconductor": "tower",
    "kla": "kla", "appliedmaterials": "appliedmaterials", "amazonawsisrael": "amazon", "amazonaws": "amazon",
    "awsisrael": "amazon", "teadsformerlyoutbrain": "teads", "outbrainteads": "teads", "walkmesap": "walkme",
    "cyberarkpaloaltonetworks": "cyberark", "ironsourceunity": "ironsource", "salesforceisrael": "salesforce",
    "nvidiaisrael": "nvidia", "inteli": "intel", "intelisrael": "intel", "kplmgisrael": "kpmg", "kpmgisrael": "kpmg",
    "valenssemiconductor": "valens", "innovizt": "innoviz", "arberobotics": "arbe", "vayyarimaging": "vayyar",
    "similarwebltd": "similarweb", "myheritage": "myheritage", "travelierbookaway": "travelier",
    "morninggreeninvoice": "morning", "nextinsurance": "nextinsurance", "highlander": "highlander",
}


def normalize_company(name):
    s = normalize_text(name).lower()
    s = re.sub(r"\(.*?\)", " ", s)  # "Travelier (Bookaway)" -> "travelier"
    s = COMPANY_SUFFIX_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9֐-׿]+", "", s)
    return COMPANY_ALIASES.get(s, s)


# Known Israeli places (English + Hebrew). Used both to normalize locations and to strip a location
# that a scraper glued to the end of a title ("Frontend EngineerKfar Saba, Israel").
CITY_CANON = [
    ("tel aviv", r"tel[\s\-_]*aviv(?:[\s\-]*(?:yafo|jaffa))?|תל[\s\-]*אביב(?:[\s\-]*יפו)?"),
    ("ramat gan", r"ramat[\s\-]*gan|רמת[\s\-]*גן"),
    ("petah tikva", r"petah?[\s\-]*tikv?a|petach[\s\-]*tikva|פתח[\s\-]*תקו?וה"),
    ("herzliya", r"herzl?iy?a|הרצליה"),
    ("raanana", r"ra'?anana|רעננה"),
    ("kfar saba", r"kfar[\s\-]*saba|כפר[\s\-]*סבא"),
    ("hod hasharon", r"hod[\s\-]*hasharon|הוד[\s\-]*השרון"),
    ("holon", r"holon|חולון"),
    ("bat yam", r"bat[\s\-]*yam|בת[\s\-]*ים"),
    ("rishon lezion", r"rishon[\s\-]*le[\s\-]*zion|ראשון[\s\-]*לציון"),
    ("rehovot", r"rehovot|רחובות"),
    ("ness ziona", r"ness?[\s\-]*ziona|נס[\s\-]*ציונה"),
    ("givatayim", r"givatayim|גבעתיים"),
    ("bnei brak", r"bnei[\s\-]*brak|בני[\s\-]*ברק"),
    ("or yehuda", r"or[\s\-]*yehuda|אור[\s\-]*יהודה"),
    ("yehud", r"\byehud\b|יהוד"),
    ("rosh haayin", r"rosh[\s\-]*ha'?ayin|ראש[\s\-]*העין"),
    ("airport city", r"airport[\s\-]*city|איירפורט"),
    ("lod", r"\blod\b|לוד"),
    ("netanya", r"netanya|נתניה"),
    ("haifa", r"haifa|חיפה"),
    ("jerusalem", r"jerusalem|ירושלים"),
    ("yokneam", r"yo[kq]neam|יוקנעם|יקנעם"),
    ("beer sheva", r"be'?er[\s\-]*sheva|beersheba|באר[\s\-]*שבע"),
    ("caesarea", r"caesarea|קיסריה"),
    ("migdal haemek", r"migdal[\s\-]*ha'?emek|מגדל[\s\-]*העמק"),
    ("kiryat gat", r"kiryat[\s\-]*gat|קרית[\s\-]*גת|קריית[\s\-]*גת"),
    ("modiin", r"modi'?in|מודיעין"),
    ("ashdod", r"ashdod|אשדוד"),
    ("beer yaakov", r"beer[\s\-]*ya'?a[ck]ov|באר[\s\-]*יעקב"),
    ("ofakim", r"ofakim|אופקים"),
    ("gush dan", r"gush[\s\-]*dan|גוש[\s\-]*דן"),
]
CITY_RES = [(canon, re.compile(pat, re.I)) for canon, pat in CITY_CANON]
ISRAEL_RE = re.compile(r"\bisrael\b|ישראל|\bIL\b(?![\s,]*\d)", re.I)

FOREIGN_RE = re.compile(
    r"\b(bangkok|thailand|philippines|singapore|australia|london|united kingdom|germany|berlin|munich|"
    r"poland|warsaw|krak[oó]w|india|bangalore|bengaluru|hyderabad|pune|canada|toronto|new york|nyc|"
    r"san francisco|california|seattle|chicago|austin|texas|boston|florida|miami|atlanta|denver|"
    r"paris|france|amsterdam|netherlands|dublin|ireland|tokyo|japan|spain|madrid|barcelona|brazil|"
    r"mexico|colombia|vietnam|united states|usa|romania|ukraine|portugal|lisbon|cyprus|bulgaria|"
    r"serbia|greece|italy|switzerland|sweden|denmark|czech|prague|hungary|china|taiwan|korea|"
    r"malaysia|argentina|uae|dubai|hong kong|saudi arabia|turkey|malta|emea|apac|latam)\b", re.I)


def normalize_location(loc):
    """Coarse, stable location token: a canonical Israeli city, 'israel', 'foreign' or 'unknown'."""
    s = normalize_text(loc)
    if not s or s.lower() in ("unspecified", "n/a", "none"):
        return "unknown"
    for canon, rx in CITY_RES:
        if rx.search(s):
            return canon
    if ISRAEL_RE.search(s) or re.search(r"\b(center|central) district\b|מרכז", s, re.I):
        return "israel"
    if FOREIGN_RE.search(s):
        return "foreign"
    return "unknown"


EU_GENDER_RE = re.compile(r"\((?:m/f/d|m/w/d|d/m/w|m/f/x|w/m/d)\)", re.I)


def location_is_foreign(*texts):
    """
    True when the texts name a foreign place (or carry a European '(m/f/d)' marker) and no specific Israeli city.
    Pass the job's own title/URL/location - never the employer's HQ: an Israeli company with a Bangkok-based
    role is a foreign job. A bare 'Israel' default does not outweigh an explicit foreign city.
    """
    blob = " ".join(normalize_text(t) for t in texts if t)
    if not blob:
        return False
    # Split glued words ("ThailandSupply" -> "Thailand Supply") so word boundaries work.
    blob = re.sub(r"([a-z])([A-Z])", r"\1 \2", blob)
    if any(rx.search(blob) for canon, rx in CITY_RES if canon != "gush dan"):
        return False
    return bool(FOREIGN_RE.search(blob) or EU_GENDER_RE.search(blob))


TITLE_NOISE_RE = re.compile(
    r"(\(opens in a new tab\)|\bapply now\b|\bread more\b|\bmore details\b|\blearn more\b|\bview job\b|"
    r"לפרטים\s*←?|לפרטים נוספים|קרא עוד|\bfull[\s\-]?time\b|\bpart[\s\-]?time\b|\bhybrid\b|"
    r"\bpermanent\b|\bentry[\s\-]level\b(?=\s*$)|·)", re.I)
TITLE_PREFIX_RE = re.compile(r"^(apply for|apply to|משרה פתוחה|דרושים|דרוש\s*/\s*ה|דרוש)\s+", re.I)
HEBREW_SLASH_RE = re.compile(r"(?<=[֐-׿])\s*([/.])\s*(?=[֐-׿]{1,3}\b)")  # "מנהל /ת" -> "מנהל/ת"
TRAILING_LOC_RE = re.compile(
    r"(?:[\s,|\-–]*\(?(?:" + "|".join(p for _, p in CITY_CANON) + r")(?:\s*,\s*[\w\s]+)?\)?|[\s,|\-–]+israel)\s*$",
    re.I)


def clean_title(title):
    """
    Remove scraper noise from a title: CTA text, '(opens in a new tab)', 'Apply for', and a location
    glued to the end ("Frontend EngineerKfar Saba, Israel" -> "Frontend Engineer").
    Returns the cleaned title (never empty: falls back to the normalized input).
    """
    raw = normalize_text(title)
    t = HEBREW_SLASH_RE.sub(r"\1", raw)
    t = TITLE_PREFIX_RE.sub("", t)
    t = TITLE_NOISE_RE.sub(" ", t)
    t = re.sub(r"\(\s*\)|\[\s*\]", " ", t)  # brackets emptied by noise removal, e.g. "(Hybrid)"
    t = normalize_text(t)
    for _ in range(3):  # strip "..., Israel", then "Kfar Saba", etc.
        new = normalize_text(TRAILING_LOC_RE.sub("", t))
        if new == t or len(new) < 3:
            break
        t = new
    t = t.strip(" -–|,·")
    return t or raw


def normalize_title(title):
    t = clean_title(title).lower()
    t = re.sub(r"[\/\.]", " ", t)          # "מנהל/ת" == "מנהל ת", "מנהל.ת"
    t = re.sub(r"[^\w֐-׿+#& ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------------------
# URLs and ATS IDs
# ---------------------------------------------------------------------------

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid", "_gl", "gh_src",
    "lever-source", "lever-origin", "source", "ref", "referrer", "trk", "trackingid", "refid", "position",
    "pagenum", "coref", "shared_id", "src", "t", "mc_cid", "mc_eid", "redirect", "redirect_to", "_wpnonce",
}


def canonicalize_url(url):
    """Lowercase scheme/host, drop fragments and tracking params, keep meaningful query params, sort them."""
    u = (url or "").strip()
    if not u:
        return ""
    p = urlparse(u)
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host.endswith("linkedin.com"):
        host = "linkedin.com"  # il.linkedin.com == www.linkedin.com
    path = unquote(p.path) or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    query = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
                   if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_"))
    return urlunparse(((p.scheme or "https").lower(), host, path, "", urlencode(query), ""))


SHARED_FORM_HOSTS = ("forms.monday.com", "docs.google.com", "forms.gle", "typeform.com", "jotform.com",
                     "forms.office.com", "airtable.com", "tally.so")

ATS_ID_PATTERNS = [
    ("greenhouse", re.compile(r"greenhouse\.io/(?:[\w\-]+/)?([\w\-]+)/jobs/(\d+)", re.I), lambda m: f"{m.group(1).lower()}/{m.group(2)}"),
    ("greenhouse", re.compile(r"[?&]gh_jid=(\d+)", re.I), lambda m: m.group(1)),
    ("lever", re.compile(r"lever\.co/([\w\-]+)/([0-9a-f\-]{36})", re.I), lambda m: m.group(2).lower()),
    ("ashby", re.compile(r"ashbyhq\.com/([\w\-\.%]+)/([0-9a-f\-]{36})", re.I), lambda m: m.group(2).lower()),
    ("comeet", re.compile(r"comeet\.(?:com|co)/jobs/[\w\-]+/[0-9A-F]{2}\.[0-9A-F]{3}/[^/]+/([0-9A-F]{2}\.[0-9A-F]{3})", re.I), lambda m: m.group(1).upper()),
    ("comeet", re.compile(r"/(?:career|careers|jobs|job|position|positions)(?:/co/[\w\-]+)?/([0-9A-F]{2}\.[0-9A-F]{3})(?:/|$)", re.I), lambda m: m.group(1).upper()),
    ("workday", re.compile(r"([\w\-]+)\.wd\d+\.myworkdayjobs\.com/.*?_((?:JR|R|REQ)?[\-]?\d{4,}(?:-\d+)?)(?:[/?#]|$)", re.I),
     lambda m: f"{m.group(1).lower()}/{m.group(2).upper()}"),
    ("smartrecruiters", re.compile(r"smartrecruiters\.com/([\w\-]+)/(\d{6,})", re.I), lambda m: m.group(2)),
    ("linkedin", re.compile(r"linkedin\.com/jobs/view/(?:[\w\-%]*-)?(\d{8,})", re.I), lambda m: m.group(1)),
    ("linkedin", re.compile(r"linkedin\.com/jobs-guest/jobs/api/jobposting/(\d{8,})", re.I), lambda m: m.group(1)),
    ("amazon", re.compile(r"amazon\.jobs/[a-z\-]+/jobs/(\d+)", re.I), lambda m: m.group(1)),
    ("apple", re.compile(r"jobs\.apple\.com/[\w\-]+/details/([\w\-]+?)(?:/|$)", re.I), lambda m: m.group(1)),
    ("google", re.compile(r"careers/applications/jobs/results/(\d{6,})", re.I), lambda m: m.group(1)),
    ("alljobs", re.compile(r"alljobs\.co\.il/.*[?&]jobid=(\d+)", re.I), lambda m: m.group(1)),
    ("drushim", re.compile(r"drushim\.co\.il/job/(\d+)", re.I), lambda m: m.group(1)),
    ("jobmaster", re.compile(r"jobmaster\.co\.il/.*[?&]key=(\d+)", re.I), lambda m: m.group(1)),
    ("gotfriends", re.compile(r"gotfriends\.co\.il/jobslobby/[\w\-]+/[\w\-]+/(\d+)", re.I), lambda m: m.group(1)),
]


def is_shared_form_url(url):
    host = urlparse(url or "").netloc.lower()
    return any(host == h or host.endswith("." + h) for h in SHARED_FORM_HOSTS)


def extract_ats_job_id(url):
    """Return (provider, job_id) for a job-specific URL, or (None, None). Shared forms never yield an ID."""
    if not url or is_shared_form_url(url):
        return None, None
    for provider, rx, fmt in ATS_ID_PATTERNS:
        m = rx.search(url)
        if m:
            return provider, fmt(m)
    return None, None


def job_key(company, title, url, location=""):
    provider, jid = extract_ats_job_id(url)
    if provider == "comeet":
        # Comeet position UIDs are short; scope them by employer.
        return f"ats:comeet:{normalize_company(company)}:{jid}"
    if provider:
        return f"ats:{provider}:{jid}"
    basis = "|".join([normalize_company(company), normalize_title(title), normalize_location(location),
                      canonicalize_url(url)])
    return "fb:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def legacy_hash(company, title, url):
    """The original identity used by job_crawler.py <= v5 and by index.html's localStorage state. Do not change."""
    normalized = f"{company.strip().lower()}::{title.strip().lower()}::{url.strip().lower()}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Non-job detection
# ---------------------------------------------------------------------------

NON_JOB_URL_RULES = [
    ("logout_or_auth_url", re.compile(r"logout|log-out|signout|sign-out|/authorization/|wp-login|_wpnonce|/login\b|/signin\b", re.I)),
    ("article_or_guide", re.compile(r"articlepage|/blog/|/article|/news/|/magazine/|/guide", re.I)),
    ("search_results_page", re.compile(r"searchresults|/search\?|[?&]search=|[?&]q=|/jobs/search/|/jobs/q-", re.I)),
    ("category_landing_page", re.compile(r"/jobslobby/(?![\w\-]+/[\w\-]+/\d+)|/categories/|/category/|nisha\.co\.il/positions/[\w\-]+/?$|/job_cat/", re.I)),
    ("program_or_portal_page", re.compile(r"pathways-programs|/career-opportunities/students-internship|/early-careers/?$|/students/?$", re.I)),
    ("company_profile_page", re.compile(r"linkedin\.com/company/|linkedin\.com/in/|"
                                        r"alljobs\.co\.il/employer/|drushim\.co\.il/companies?/", re.I)),
]
NON_JOB_TITLE_RULES = [
    ("article_or_guide", re.compile(r"מדריך|הכשרות|טיפים|איך להצליח|ה-?AI לא יחליף", re.I)),
    ("navigation_title", re.compile(r"^(developer zone|entry-level roles|students? &\s*internships?|students|careers|open positions|"
                                    r"all jobs|view all jobs|דרושים [\w\s]{1,25}|ai לתחום.*)$", re.I)),
]


def classify_non_job(title, url):
    """Return a reason string when the record is clearly not a single job posting, else None."""
    u = url or ""
    t = normalize_text(title)
    if extract_ats_job_id(u)[0]:
        return None  # a specific ATS posting is a job even if its title is odd
    for reason, rx in NON_JOB_URL_RULES:
        if rx.search(u):
            return reason
    for reason, rx in NON_JOB_TITLE_RULES:
        if rx.search(t):
            return reason
    return None


# ---------------------------------------------------------------------------
# Experience classification (with evidence) - "not mentioned" is UNSPECIFIED, never 0-2
# ---------------------------------------------------------------------------

EXPERIENCE_LEVELS = ["explicit_student", "explicit_junior", "explicit_0_2", "stretch_2_plus", "unspecified", "senior"]
EXPERIENCE_LABELS_HE = {
    "explicit_student": "סטודנט / מתמחה",
    "explicit_junior": "ג'וניור / בוגר",
    "explicit_0_2": "0–2 שנים",
    "stretch_2_plus": "2+ שנים",
    "unspecified": "לא צוין",
    "senior": "בכיר",
}

STUDENT_RE = re.compile(r"\b(student|intern|internship|working student)\b|סטודנט|משרת סטודנט|התמחות", re.I)
JUNIOR_RE = re.compile(r"\b(junior|jr\.?|graduate|new grad|entry[\s\-]?level|associate product manager|apm|"
                       r"no (?:prior )?experience (?:is )?required|recent graduates?)\b|ג'וניור|גוניור|בוגר|ללא ניסיון|"
                       r"מתאים למתחילים", re.I)
SENIOR_TITLE_RE = re.compile(r"\b(senior|sr\.?|lead|head of|director|vp|vice president|principal|staff|chief|"
                             r"group manager|team lead(?:er)?)\b|בכיר|ראש צוות|ראש תחום|סמנכ", re.I)
# For profiles that accept more than 2 years: 'Senior'/'Lead' alone is not disqualifying, management titles are.
MANAGEMENT_TITLE_RE = re.compile(r"\b(head of|director|vp|vice president|principal|chief(?! of staff)|group manager|"
                                 r"team lead(?:er)?)\b|ראש צוות|ראש תחום|סמנכ", re.I)
YEARS_RE = re.compile(
    r"(?P<pre>at\s+least|minimum(?:\s+of)?|min\.?|up\s+to|לפחות|עד)?\s*"
    r"(?P<a>\d{1,2})\s*(?P<plus>\+)?\s*(?:(?:-|–|—|to|עד)\s*(?P<b>\d{1,2}))?\s*\+?\s*"
    r"(?:years?|yrs?|שנים|שנות|שנת)\b[^.\n]{0,40}?(?:experience|ניסיון|hands-on|in\b|of\b|ב)",
    re.I)


def parse_experience_requirement(title, text="", max_yoe=2):
    """
    Returns (level, evidence). Order: senior title > explicit student > explicit junior > years in text > unspecified.
    'evidence' is the matched span so a human can audit the classification.
    max_yoe: the profile's upper bound. A stated minimum above it is 'senior'; 2..max_yoe is 'stretch_2_plus'.
    """
    t = normalize_text(title)
    body = normalize_text(text)
    max_yoe = max(2, int(max_yoe))
    m = (SENIOR_TITLE_RE if max_yoe <= 2 else MANAGEMENT_TITLE_RE).search(t)
    if m:
        return "senior", m.group(0)
    m = STUDENT_RE.search(t)
    if m:
        return "explicit_student", m.group(0)
    m = JUNIOR_RE.search(t)
    if m:
        return "explicit_junior", m.group(0)

    mins = []
    for ym in YEARS_RE.finditer(body):
        a = int(ym.group("a"))
        if a > 15:
            continue
        pre = (ym.group("pre") or "").lower()
        if pre in ("up to", "עד"):
            mins.append((0, ym.group(0).strip()))
        else:
            mins.append((a, ym.group(0).strip()))
    if mins:
        lo, ev = min(mins, key=lambda x: x[0])
        if lo > max_yoe:
            return "senior", ev
        if lo >= 2:
            return "stretch_2_plus", ev
        return "explicit_0_2", ev

    m = STUDENT_RE.search(body[:1500])
    if m:
        return "explicit_student", m.group(0)
    m = JUNIOR_RE.search(body[:1500])
    if m:
        return "explicit_junior", m.group(0)
    return "unspecified", ""


def experience_from_legacy(experience_field, title):
    """
    Legacy records stored free text such as '0-2 YOE (Deterministic match)', which was NOT evidence.
    Re-derive the level from the title and the stored text; generic tracker labels count as unspecified.
    """
    txt = normalize_text(experience_field)
    if re.search(r"deterministic match|implied|not explicitly excluded|job description is empty|fits junior/entry", txt, re.I):
        txt = ""
    return parse_experience_requirement(title, txt)


# ---------------------------------------------------------------------------
# Ranking (transparent heuristic; weights configurable per profile)
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS = {"role": 35, "eligibility": 25, "location": 15, "priority": 10, "fit": 10, "freshness": 5}
ELIGIBILITY_POINTS = {"explicit_student": 1.0, "explicit_junior": 1.0, "explicit_0_2": 0.95,
                      "unspecified": 0.55, "stretch_2_plus": 0.4, "senior": 0.0}
PRIORITY_POINTS = {"P1": 1.0, "P2": 0.7, "P3": 0.45, None: 0.35}


def match_score(role_match, experience_level, location_token, priority=None, skills_fit=0.5, freshness=1.0,
                weights=None, allowed_location_tokens=None, eligibility_points=None):
    """0-100. role_match / skills_fit / freshness are 0..1. eligibility_points overrides ELIGIBILITY_POINTS per profile."""
    w = dict(DEFAULT_WEIGHTS, **(weights or {}))
    elig = dict(ELIGIBILITY_POINTS, **(eligibility_points or {}))
    if location_token in ("foreign",):
        loc = 0.0
    elif allowed_location_tokens and location_token in allowed_location_tokens:
        loc = 1.0
    elif location_token == "unknown":
        loc = 0.5
    else:
        loc = 0.75
    parts = {
        "role": max(0.0, min(1.0, role_match)),
        "eligibility": elig.get(experience_level, 0.5),
        "location": loc,
        "priority": PRIORITY_POINTS.get(priority, 0.35),
        "fit": max(0.0, min(1.0, skills_fit)),
        "freshness": max(0.0, min(1.0, freshness)),
    }
    total = sum(w[k] * parts[k] for k in w) / max(1, sum(w.values()))
    return round(100 * total, 1)
