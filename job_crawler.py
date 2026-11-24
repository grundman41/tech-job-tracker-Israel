#!/usr/bin/env python3
"""
Autonomous Israeli Job Crawler (v5.0 - Business / Product / Operations profile).

Key features:
1. Hybrid Scraping Engine:
   - Direct ATS JSON APIs: Greenhouse, Lever (incl. EU), Ashby, Comeet (hosted pages + embedded widgets),
     Workday, SmartRecruiters and Amazon Jobs - structured data, location and full descriptions.
   - Fast parallel `requests` for standard HTML career pages and job-board search pages (AllJobs, Drushim, GotFriends).
   - Playwright Headless Chromium fallback for JS-rendered SPAs / 403-blocked pages.
2. Config-driven title filtering (`config.json`):
   - `title_keywords(_he)` decide which titles are candidates; `excluded_title_keywords(_he)` and
     `seniority_reject_keywords(_he)` hard-reject before any AI call.
3. Location policy:
   - Only `allowed_cities` (Gush Dan by default), except companies listed in `location_exempt_companies`
     which may be anywhere in Israel.
4. Strict Anti-Guessing & Minimum-YOE Guard:
   - Never sends empty descriptions to AI (min 150 chars).
   - Deterministic `has_excessive_min_yoe()` rejects "3+ years" etc. when max_yoe=2, keeps "1-3 years".
5. Small-batch Gemini verification (4 jobs/batch) with role-category tagging for the dashboard.
"""

import os
import sys
import re
import json
import time
import hashlib
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse, quote
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
load_dotenv()

import warnings
import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import openpyxl

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

import job_identity as ji
import storage

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# --- Paths & Configuration ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

if os.path.exists(os.path.join(BASE_DIR, "master_companies_with_referrals.json")):
    MASTER_FILE = os.path.join(BASE_DIR, "master_companies_with_referrals.json")
elif os.path.exists(os.path.join(BASE_DIR, "master_companies.json")):
    MASTER_FILE = os.path.join(BASE_DIR, "master_companies.json")
else:
    MASTER_FILE = os.path.join(BASE_DIR, "companies_registry_public.json")

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
SEEN_JOBS_FILE = os.path.join(BASE_DIR, "seen_jobs.json")
CHECKPOINT_FILE = os.path.join(BASE_DIR, "progress_checkpoint.json")
OUTPUT_EXCEL = os.path.join(BASE_DIR, f"new_jobs_{datetime.now().strftime('%Y%m%d')}.xlsx")

# Free-tier text models, best first. The preflight falls back along this list (and then to any available
# Flash-Lite / Flash model from models.list) if the configured model has been renamed or retired.
GEMINI_MODEL_PREFERENCE = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite",
                           "gemini-flash-lite-latest", "gemini-flash-latest", "gemini-2.5-flash"]
MODEL_NAME = os.getenv("GEMINI_MODEL", "").strip()  # resolved after config load
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip().strip('"').strip("'")

DEFAULT_REFERRAL_CONTACT = "הגשה ישירה באתר (ללא רפרל)"
DEFAULT_REFERRAL_TYPE = "הגשה ישירה"

# Fallback only - the real profile lives in config.json.
DEFAULT_CONFIG = {
    "min_yoe": 0,
    "max_yoe": 2,
    "location": "Israel",
    "allowed_cities": ["Tel Aviv", "Ramat Gan", "Petah Tikva", "Herzliya", "Gush Dan", "תל אביב", "רמת גן", "גוש דן"],
    "location_exempt_companies": [],
    "include_student_and_graduate": True,
    "target_roles": [
        "Product Manager", "Project Manager", "PMO", "Business Analyst", "Operations Analyst",
        "Industrial Engineer", "Solutions Engineer", "Customer Success Manager", "Business Development"
    ],
    "title_keywords": [
        "product", "project", "program", "pmo", "business analyst", "operations", "industrial engineer",
        "solutions", "customer success", "business development", "consultant", "junior", "intern", "student", "graduate"
    ],
    "title_keywords_he": ["פרויקט", "מוצר", "אנליסט", "תפעול", "תעשייה וניהול"],
    "excluded_roles": ["Software Development", "DevOps", "QA", "Technical Support", "Marketing", "HR"],
    "excluded_title_keywords": ["developer", "software engineer", "devops", "qa", "technical support", "marketing", "hr"],
    "excluded_title_keywords_he": ["מפתח", "מתכנת", "תמיכה"],
    "seniority_reject_keywords": ["senior", "sr", "lead", "head of", "director", "vp", "chief", "principal", "staff", "architect"],
    "seniority_reject_keywords_he": ["בכיר", "ראש צוות"],
}

ROLE_CATEGORIES = [
    "product", "project", "operations", "business_analysis", "industrial_engineering",
    "solutions", "customer_success", "business_development", "consulting", "data_analytics", "finance",
    "business_systems", "other"
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
    # Avoid zstd: some servers (amazon.jobs) send it and urllib3 fails to decode.
    "Accept-Encoding": "gzip, deflate",
}
JSON_HEADERS = {**HEADERS, "Accept": "application/json", "Content-Type": "application/json"}


def load_json(filepath, default_val):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[!] Error loading {filepath}: {e}")
    return default_val


def save_json(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def read_config_file(path=None):
    path = path or CONFIG_FILE
    if not os.path.exists(path):
        print("[!] config.json not found - using DEFAULT_CONFIG.")
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        # Fail loudly: silently falling back to defaults hides a broken config for weeks.
        print(f"[!] FATAL: config.json is not valid JSON ({e}). Fix it before running.")
        sys.exit(1)
    return raw if isinstance(raw, dict) else {}


def resolve_profile(raw, profile_name=None):
    """
    Schema v2: {"active_profile": ..., "profiles": {name: {...}}, <global sections>}.
    Schema v1 (legacy, flat): the top-level keys are the profile.
    Returns (profile_name, merged_profile_config).
    """
    if "profiles" in raw:
        name = profile_name or os.getenv("JOB_PROFILE") or raw.get("active_profile")
        if name not in raw["profiles"]:
            print(f"[!] FATAL: profile '{name}' not found in config.json (available: {list(raw['profiles'])}).")
            sys.exit(1)
        source = raw["profiles"][name]
    else:
        name, source = "legacy_flat", raw
    cfg = DEFAULT_CONFIG.copy()
    for k, v in source.items():
        if v is not None:
            cfg[k] = v
    return name, cfg


RAW_CONFIG = read_config_file()
PROFILE_NAME, USER_CONFIG = resolve_profile(RAW_CONFIG)
GLOBAL_CONFIG = {k: v for k, v in RAW_CONFIG.items() if k not in ("profiles",)}
MODEL_NAME = MODEL_NAME or str(GLOBAL_CONFIG.get("gemini_model") or GEMINI_MODEL_PREFERENCE[0]).strip()
TARGET_LOCATION = str(USER_CONFIG.get("location", "Israel")).strip() or "Israel"
IS_ISRAEL_TARGET = TARGET_LOCATION.lower() in ("israel", "ישראל")
EXEMPT_COMPANIES = {str(n).strip().lower() for n in USER_CONFIG.get("location_exempt_companies", [])}


# --- Keyword regex helpers ---

def _phrase_pattern(phrase):
    """Escape a phrase and let spaces/hyphens match flexibly."""
    parts = [re.escape(p) for p in re.split(r"[\s\-_]+", phrase.strip()) if p]
    return r"[\s\-_/]*".join(parts)


def build_keyword_regex(english, hebrew=()):
    """English phrases get word boundaries; Hebrew phrases match as substrings (prefix letters attach)."""
    alts = []
    en = [_phrase_pattern(p) for p in english if str(p).strip()]
    if en:
        alts.append(r"(?<![a-z0-9])(?:" + "|".join(sorted(en, key=len, reverse=True)) + r")(?:s|es)?(?![a-z0-9])")
    he = [_phrase_pattern(p) for p in hebrew if str(p).strip()]
    if he:
        alts.append(r"(?:" + "|".join(sorted(he, key=len, reverse=True)) + r")")
    if not alts:
        return re.compile(r"(?!x)x")
    return re.compile("|".join(alts), re.IGNORECASE)


# --- Strict Deterministic Regex Rules (Config-Aware) ---

EUROPE_GENDER_REGEX = re.compile(
    r"\b(m/f/d|m/w/d|d/m/w|m/f/x|w/m/d)\b",
    re.IGNORECASE
)

FOREIGN_LOCATION_REGEX = re.compile(
    r"\b(bangkok|thailand|singapore|australia|sydney|melbourne|london|uk\b|united kingdom|"
    r"germany|deutschland|berlin|munich|walldorf|potsdam|frankfurt|dresden|hamburg|"
    r"poland|warsaw|krakow|kraków|wroclaw|india|bangalore|bengaluru|hyderabad|pune|gurgaon|"
    r"florida|fort lauderdale|miami|atlanta|denver|los angeles|new jersey|washington|virginia|"
    r"canada|toronto|vancouver|new[\s\-_]+york|nyc\b|san[\s\-_]+francisco|california|seattle|"
    r"chicago|austin|texas|boston|paris|france|amsterdam|netherlands|dublin|ireland|"
    r"tokyo|japan|spain|madrid|barcelona|brazil|mexico|colombia|philippines|vietnam|"
    r"united states|usa|u\.s\.|romania|bucharest|ukraine|kyiv|portugal|lisbon|cyprus|limassol|"
    r"bulgaria|sofia|serbia|belgrade|greece|athens|italy|milan|switzerland|zurich|sweden|stockholm|"
    r"denmark|copenhagen|czech|prague|hungary|budapest|china|shanghai|beijing|taiwan|taipei|korea|seoul|"
    r"malaysia|penang|kuala lumpur|costa rica|argentina|buenos aires|uae|dubai|abu dhabi|hong kong|"
    r"remote\s*-?\s*(?:us|usa|emea|europe|uk))\b",
    re.IGNORECASE
)

ISRAEL_LOCATION_REGEX = re.compile(
    r"(israel|ישראל|tel[\s\-_]*aviv|תל[\s\-_]*אביב|haifa|חיפה|herzliya|הרצליה|jerusalem|ירושלים|"
    r"beer[\s\-_']*sheva|באר[\s\-_]*שבע|petah?[\s\-_]*tikva|פתח[\s\-_]*תקו?וה|ra'?anana|רעננה|rehovot|רחובות|"
    r"ramat[\s\-_]*gan|רמת[\s\-_]*גן|yokneam|yoqneam|יוקנעם|יקנעם|glil[\s\-_]*yam|גליל[\s\-_]*ים|netanya|נתניה|"
    r"kfar[\s\-_]*saba|כפר[\s\-_]*סבא|holon|חולון|caesarea|קיסריה|airport[\s\-_]*city)",
    re.IGNORECASE
)

# Israeli places that are OUTSIDE the allowed region (used only for non-exempt companies).
OTHER_ISRAEL_CITY_REGEX = re.compile(
    r"(haifa|חיפה|jerusalem|ירושלים|yokneam|yoqneam|יוקנעם|יקנעם|beer[\s\-_']*sheva|be'?er[\s\-_]*sheva|beersheba|"
    r"באר[\s\-_]*שבע|netanya|נתניה|caesarea|קיסריה|migdal[\s\-_]*ha'?emek|מגדל[\s\-_]*העמק|kiryat[\s\-_]*gat|"
    r"קרית[\s\-_]*גת|קריית[\s\-_]*גת|ofakim|אופקים|nazareth|נצרת|karmiel|כרמיאל|modi'?in|מודיעין|ashdod|אשדוד|"
    r"ashkelon|אשקלון|dimona|דימונה|sderot|שדרות|\bomer\b|עומר|misgav|משגב|kiryat[\s\-_]*shmona|קרית[\s\-_]*שמונה|"
    r"eilat|אילת|afula|עפולה|hadera|חדרה|rosh[\s\-_]*pina|ראש[\s\-_]*פינה|tirat[\s\-_]*carmel|טירת[\s\-_]*כרמל|"
    r"nesher|נשר|kfar[\s\-_]*vradim|כפר[\s\-_]*ורדים|zichron|זכרון|or[\s\-_]*akiva|אור[\s\-_]*עקיבא|"
    r"beit[\s\-_]*shemesh|בית[\s\-_]*שמש|mevaseret|מבשרת|katzrin|קצרין|yavne|יבנה|kiryat[\s\-_]*ata|"
    r"northern[\s\-_]*district|southern[\s\-_]*district|haifa[\s\-_]*district|jerusalem[\s\-_]*district|"
    r"אזור[\s\-_]*הצפון|אזור[\s\-_]*הדרום|"
    # Central-Israel cities: rejected only when the profile's allowed_cities do not list them (allowed wins).
    r"petah?[\s\-_]*tikv?a|petach[\s\-_]*tikv?a|פתח[\s\-_]*תקו?וה|holon|חולון|bat[\s\-_]*yam|בת[\s\-_]*ים|"
    r"rishon[\s\-_]*le[\s\-_]*?zion|ראשון[\s\-_]*לציון|rehovot|רחובות|ness?[\s\-_]*ziona|נס[\s\-_]*ציונה|"
    r"ra'?anana|רעננה|kfar[\s\-_]*saba|כפר[\s\-_]*סבא|hod[\s\-_]*hasharon|הוד[\s\-_]*השרון|"
    r"ramat[\s\-_]*hasharon|רמת[\s\-_]*השרון|kiryat[\s\-_]*ono|קרי?ית[\s\-_]*אונו|or[\s\-_]*yehuda|אור[\s\-_]*יהודה|"
    r"\byehud\b|rosh[\s\-_]*ha'?[\s\-_]*ayin|ראש[\s\-_]*העין|\blod\b|לוד|ramla|רמלה|shoham|שוהם|"
    r"airport[\s\-_]*city|איירפורט[\s\-_]*סיטי|givat[\s\-_]*shmuel|גבעת[\s\-_]*שמואל)",
    re.IGNORECASE
)

# "Tel Aviv District" is a region (Holon, Bat Yam, Ramat HaSharon...), not the city: ignore it when matching cities.
TEL_AVIV_DISTRICT_REGEX = re.compile(r"tel[\s\-_]*aviv[\s\-_]*district|מחוז[\s\-_]*תל[\s\-_]*אביב", re.IGNORECASE)

ALLOWED_CITY_REGEX = build_keyword_regex(
    [c for c in USER_CONFIG.get("allowed_cities", []) if re.search(r"[a-zA-Z]", c)],
    [c for c in USER_CONFIG.get("allowed_cities", []) if not re.search(r"[a-zA-Z]", c)],
)

NON_JOB_URL_PATTERNS = re.compile(
    r"("
    r"[?&]search="
    r"|/search\?"
    r"|searchresults"
    r"|/jobslobby/(?![\w\-]+/[\w\-]+/\d+)"  # GotFriends lobby/category pages (individual jobs end with a numeric id)
    r"|/categories/"
    r"|/all-jobs/"
    r"|/all_jobs/"
    r"|/alljobs"
    r"|alljobs\.co\.il/employer/"   # AllJobs employer landing pages list many roles, they are not a posting
    r"|/career-opportunities/students-internship"
    r"|developer-zone|developer_zone|developers|documentation|/docs/|/blog/|/resources/|/community/|/about/(?!careers)|/contact/|/news/"
    r")",
    re.IGNORECASE
)

AGENCY_CATEGORY_REGEX = re.compile(
    r"(gotfriends\.co\.il/jobslobby/(?![\w\-]+/[\w\-]+/\d+)|"
    r"nisha\.co\.il/positions/[a-z\-]+/?$|"
    r"alljobs\.co\.il/search(?!/uploadsingle)|"
    r"experis\.co\.il/search)",
    re.IGNORECASE
)

NAV_EXCLUDE_REGEX = re.compile(
    r"^(developer zone|developer portal|developer community|developer guide|developer hub|"
    r"developers|for developers|developer docs|documentation|api documentation|resources|"
    r"community|blog|news|about us|contact us|sign in|log in|sign up|products|solutions|privacy policy|terms|view all jobs|search jobs|"
    # Department / filter links on career pages (not vacancies)
    r"operations|operation|project management|product|product management|strategy|corporate strategy|business development|"
    r"customer success|consulting|partnerships|business operations|professional services|students|graduates|internships|"
    r"students & internship|students and graduates|חטיבת הייעוץ|תפעול|ניהול פרויקטים|ניהול מוצר|סטודנטים)$",
    re.IGNORECASE
)

GENERIC_CTA_REGEX = re.compile(
    r"^(read more|learn more|apply now|apply|view job|see details|more details|details|לפרטים|הגש מועמדות|הגשת מועמדות|"
    r"למשרה|קרא עוד|פרטי המשרה|לפרטים נוספים|שליחת קו\"ח|הגש קו\"ח)$",
    re.IGNORECASE
)

ALWAYS_REJECT_REGEX = build_keyword_regex(
    ["legal", "counsel", "office manager", "administrative", "receptionist", "executive assistant", "payroll"],
    ["מדריך", "קורס", "הכשרה", "הכשרות", "סדנה", "טיפים", "איך להצליח"],
)


def build_reject_title_regex(cfg):
    english = list(cfg.get("excluded_title_keywords", []))
    hebrew = list(cfg.get("excluded_title_keywords_he", []))
    # The profile's seniority list is always applied: a profile with a wider YOE range simply lists fewer words.
    english += list(cfg.get("seniority_reject_keywords", []))
    hebrew += list(cfg.get("seniority_reject_keywords_he", []))
    return build_keyword_regex(english, hebrew)


def build_candidate_title_regex(cfg):
    english = list(cfg.get("title_keywords", []))
    hebrew = list(cfg.get("title_keywords_he", []))
    # Full target-role phrases are always candidates too (never split into single tokens - too noisy).
    english += [r for r in cfg.get("target_roles", []) if re.search(r"[a-zA-Z]", r)]
    if not cfg.get("include_student_and_graduate", True):
        drop = {"intern", "internship", "student", "graduate"}
        english = [k for k in english if k.lower() not in drop]
    return build_keyword_regex(list(dict.fromkeys(english)), list(dict.fromkeys(hebrew)))


def build_override_title_regex(cfg):
    """Compound phrases that are wanted even though a word inside them is excluded (e.g. 'Sales Operations')."""
    return build_keyword_regex(list(cfg.get("title_override_keywords", [])))


class ProfileTitleFilter:
    """Title-level matching for one profile (used by the crawler and to tag legacy records)."""

    def __init__(self, cfg):
        self.reject = build_reject_title_regex(cfg)
        self.candidate = build_candidate_title_regex(cfg)
        self.override = build_override_title_regex(cfg)
        self.seniority = build_keyword_regex(cfg.get("seniority_reject_keywords", []),
                                             cfg.get("seniority_reject_keywords_he", []))

    def matches(self, title):
        t = re.sub(r"\s+", " ", title or "").strip()
        if self.override.search(t) and not self.seniority.search(t):
            return True
        if self.reject.search(t):
            return False
        return bool(self.candidate.search(t))


def build_excluded_company_regex(cfg):
    """excluded_companies: {group: [names...], "comment": "..."} or a flat list. Whole words (EN) / substrings (HE)."""
    raw = cfg.get("excluded_companies") or {}
    names = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k != "comment" and isinstance(v, list):
                names += v
    elif isinstance(raw, list):
        names = raw
    return build_keyword_regex([n for n in names if re.search(r"[a-zA-Z]", n)],
                               [n for n in names if not re.search(r"[a-zA-Z]", n)])


EXCLUDED_COMPANY_REGEX = build_excluded_company_regex(USER_CONFIG)


def is_company_excluded(*names):
    return any(n and EXCLUDED_COMPANY_REGEX.search(str(n)) for n in names)


REJECT_TITLE_REGEX = build_reject_title_regex(USER_CONFIG)
CANDIDATE_TITLE_REGEX = build_candidate_title_regex(USER_CONFIG)
ACTIVE_TITLE_FILTER = ProfileTitleFilter(USER_CONFIG)
RESTRICT_TO_ALLOWED_CITIES = bool(USER_CONFIG.get("allowed_cities"))


def has_excessive_min_yoe(text, max_allowed_yoe):
    """
    Checks if the job text explicitly requires a minimum number of years > max_allowed_yoe.
    - Allows ranges like '1-3 years' or '2-4 years' (because minimum is <= 2).
    - Rejects '3+ years', '3-5 years', 'at least 3 years', 'minimum 3 years', '4+ years of experience', etc.
    """
    if not text:
        return False

    pattern = re.compile(
        r"(?:at\s+least|minimum(?:\s+of)?|min\.?|לפחות)?\s*"
        r"(\d{1,2})\s*(?:\+|(?:\s*[-–—to]+\s*\d{1,2}))?\s*"
        r"(?:years?|yrs?|שנים|שנות)\s*(?:of)?\s*(?:experience|industry|hands-on|ניסיון)?",
        re.IGNORECASE
    )

    for match in pattern.finditer(text):
        full_phrase = match.group(0).lower()
        min_years = int(match.group(1))
        if "company" in full_phrase or "founded" in full_phrase or "industry leader" in full_phrase:
            continue
        has_qualifier = any(q in full_phrase for q in ["+", "-", "–", "—", "to", "at least", "minimum", "min", "לפחות", "experience", "ניסיון"])
        if has_qualifier and min_years > max_allowed_yoe and min_years <= 15:
            return True, match.group(0).strip()

    return False


# --- Location policy ---

def is_location_exempt(company_name):
    return str(company_name).strip().lower() in EXEMPT_COMPANIES


def location_verdict(company_name, loc_text):
    """
    Returns 'ok', 'reject' or 'unknown' for a location string.
    - Foreign-only locations are rejected for everyone (Israel target).
    - Non-exempt companies must be in an allowed city; other Israeli cities are rejected.
    """
    loc = (loc_text or "").strip()
    if not loc or loc.lower() in ("unspecified", "n/a"):
        return "unknown"
    in_israel = bool(ISRAEL_LOCATION_REGEX.search(loc)) or bool(ALLOWED_CITY_REGEX.search(loc))
    if IS_ISRAEL_TARGET and FOREIGN_LOCATION_REGEX.search(loc) and not in_israel:
        return "reject"
    if is_location_exempt(company_name) or not RESTRICT_TO_ALLOWED_CITIES:
        return "ok" if in_israel else "unknown"
    city_text = TEL_AVIV_DISTRICT_REGEX.sub(" ", loc)
    if ALLOWED_CITY_REGEX.search(city_text):
        return "ok"
    if OTHER_ISRAEL_CITY_REGEX.search(city_text):
        return "reject"
    return "unknown"


# --- Helper Functions ---

def compute_job_hash(company, title, url):
    normalized = f"{company.strip().lower()}::{title.strip().lower()}::{url.strip().lower()}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def response_text(resp):
    """Many Israeli sites omit the charset header -> requests falls back to ISO-8859-1 and Hebrew becomes mojibake."""
    if resp.encoding and resp.encoding.lower() in ("iso-8859-1", "latin-1", "windows-1252") and "charset" not in resp.headers.get("content-type", "").lower():
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def html_to_text(html):
    return BeautifulSoup(html or "", "html.parser").get_text(separator="\n", strip=True)


def display_company(company_info):
    return company_info.get("display_name") or company_info["name"]


def source_type_of(company_info, via_ats):
    if company_info.get("source_type"):
        return company_info["source_type"]
    if company_info.get("category") == "Job Board":
        return "job_board"
    return "ats_api" if via_ats else "career_page"


def make_candidate(company_info, title, url, loc, description="", context="", via_ats=False):
    return {
        "company": display_company(company_info),
        "registry_name": company_info["name"],
        "raw_title": title,
        "title": ji.clean_title(title),
        "url": url,
        "context_snippet": context or f"{title} - {loc}",
        "requirements_text": (description or "").strip()[:6000],
        "page_location": loc or "Unspecified",
        "referral_contact": company_info.get("referral_contact", DEFAULT_REFERRAL_CONTACT),
        "referral_type": company_info.get("referral_type", DEFAULT_REFERRAL_TYPE),
        "source_type": source_type_of(company_info, via_ats),
        "source_name": company_info["name"],
        "priority": company_info.get("priority"),
        "sector": company_info.get("sector") or company_info.get("category"),
    }


GENERIC_LOCATION_REGEX = re.compile(r"^\s*(remote|hybrid|anywhere|multiple locations|\d+\s+locations|unspecified)?\s*$", re.I)


def ats_location_ok(company_info, loc):
    """ATS locations are structured and reliable: a stated location that is not in Israel is rejected."""
    verdict = location_verdict(company_info["name"], loc)
    if verdict == "reject":
        return False
    if verdict == "unknown" and not GENERIC_LOCATION_REGEX.match(loc or ""):
        return False
    return True


def call_gemini_with_retries(payload, timeout=45):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing from environment or .env file!")

    url = f"{GEMINI_API_BASE}/models/{MODEL_NAME}:generateContent"
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}  # key in a header, never in the URL/logs
    max_attempts = 5

    for attempt in range(1, max_attempts + 1):
        try:
            res = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if res.status_code == 200:
                return res.json()

            print(f"    [!] Gemini HTTP {res.status_code} ({attempt}/{max_attempts})")
            if res.status_code not in (408, 429, 500, 502, 503, 504):
                print(f"        Details: {res.text[:200]}")
                raise RuntimeError(f"Non-retryable HTTP {res.status_code}")

            if attempt == max_attempts:
                break

            wait_time = min(4 * (2 ** (attempt - 1)), 45)
            print(f"        Retrying in {wait_time}s...")
            time.sleep(wait_time)

        except requests.exceptions.RequestException as exc:
            print(f"    [!] Gemini network error ({attempt}/{max_attempts}): {exc}")
            if attempt == max_attempts:
                raise RuntimeError("Gemini API unreachable after retries") from exc
            wait_time = min(4 * (2 ** (attempt - 1)), 45)
            print(f"        Retrying in {wait_time}s...")
            time.sleep(wait_time)

    raise RuntimeError("Gemini API unavailable after maximum retries.")


def test_gemini_preflight():
    global MODEL_NAME
    print("=" * 60)
    print(f" [*] PRE-FLIGHT CHECK: Testing Gemini AI ({MODEL_NAME})...")
    print(f" [*] Active Config: YOE {USER_CONFIG['min_yoe']}-{USER_CONFIG['max_yoe']} | Country: {TARGET_LOCATION} | "
          f"Allowed cities: {len(USER_CONFIG.get('allowed_cities', []))} | Location-exempt companies: {len(EXEMPT_COMPANIES)}")
    print(f" [*] Playwright Headless Fallback: {'ENABLED' if PLAYWRIGHT_AVAILABLE else 'DISABLED'}")
    print("=" * 60)

    if not GEMINI_API_KEY:
        print("[!] ERROR: GEMINI_API_KEY is not defined in your environment or GitHub Secrets!")
        sys.exit(1)

    payload = {
        "contents": [{"parts": [{"text": "Reply with strictly the word: OK"}]}],
        "generationConfig": {"temperature": 0.0}
    }

    tried = []
    for candidate in [MODEL_NAME] + [m for m in GEMINI_MODEL_PREFERENCE if m != MODEL_NAME]:
        MODEL_NAME = candidate
        try:
            call_gemini_with_retries(payload, timeout=20)
            if tried:
                print(f"[!] Configured model unavailable ({tried[0]}); using {MODEL_NAME}. "
                      f"Set \"gemini_model\" in config.json to silence this.")
            print(f"[✓] AI Pre-flight Check SUCCESSFUL! Model {MODEL_NAME} is active.\n")
            return True
        except RuntimeError as exc:
            msg = str(exc)
            if "HTTP 401" in msg or "HTTP 403" in msg:
                print("[!] ERROR: Gemini rejected the API key (401/403). Check the GEMINI_API_KEY value / secret.")
                sys.exit(1)
            if "HTTP 404" not in msg and "HTTP 400" not in msg:
                raise
            tried.append(candidate)
    discovered = discover_available_model()
    if discovered:
        MODEL_NAME = discovered
        call_gemini_with_retries(payload, timeout=20)
        print(f"[✓] AI Pre-flight Check SUCCESSFUL with auto-discovered model {MODEL_NAME}.\n")
        return True
    print(f"[!] ERROR: none of these Gemini models are available for this key: {tried}")
    sys.exit(1)


def discover_available_model():
    """models.list -> newest stable Flash-Lite, then Flash model that supports generateContent."""
    try:
        r = requests.get(f"{GEMINI_API_BASE}/models", headers={"x-goog-api-key": GEMINI_API_KEY},
                         params={"pageSize": 200}, timeout=20)
        models = r.json().get("models", []) if r.status_code == 200 else []
    except (requests.RequestException, ValueError):
        return None
    names = [m.get("name", "").replace("models/", "") for m in models
             if "generateContent" in (m.get("supportedGenerationMethods") or ["generateContent"])]
    for needle in ("flash-lite", "flash"):
        stable = [n for n in names if needle in n and not any(x in n for x in ("preview", "image", "tts", "audio", "live"))]
        if stable:
            return sorted(stable, reverse=True)[0]
    return None


# --- ATS API Direct Scrapers ---

def _scrape_greenhouse(company_info, board):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true", headers=HEADERS, timeout=15)
    if r.status_code != 200:
        return None
    out = []
    for j in r.json().get("jobs", []):
        title = j.get("title", "")
        loc = (j.get("location") or {}).get("name", "Unspecified")
        job_url = j.get("absolute_url", "")
        if not is_title_candidate(title, job_url) or not ats_location_ok(company_info, loc):
            continue
        out.append(make_candidate(company_info, title, job_url, loc, html_to_text(html_to_text(j.get("content", "")))))
    return out


def _scrape_lever(company_info, slug, eu=False):
    r = requests.get(f"https://api{'.eu' if eu else ''}.lever.co/v0/postings/{slug}?mode=json", headers=HEADERS, timeout=15)
    if r.status_code != 200:
        return None
    out = []
    for j in r.json():
        title = j.get("text", "")
        cats = j.get("categories") or {}
        loc = " / ".join(filter(None, [cats.get("location", "")] + list(cats.get("allLocations") or [])))
        job_url = j.get("hostedUrl", "")
        if not is_title_candidate(title, job_url) or not ats_location_ok(company_info, loc):
            continue
        desc = j.get("descriptionPlain", "") + "\n" + "\n".join(
            lst.get("text", "") + "\n" + html_to_text(lst.get("content", "")) for lst in j.get("lists", [])
        )
        out.append(make_candidate(company_info, title, job_url, loc or "Unspecified", desc))
    return out


def _scrape_ashby(company_info, slug):
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", headers=HEADERS, timeout=15)
    if r.status_code != 200:
        return None
    out = []
    for j in r.json().get("jobs", []):
        title = j.get("title", "")
        locs = [j.get("location", "")] + [s.get("location", "") for s in (j.get("secondaryLocations") or []) if isinstance(s, dict)]
        loc = " / ".join(filter(None, locs)) or "Unspecified"
        job_url = j.get("jobUrl", "")
        if not is_title_candidate(title, job_url) or not ats_location_ok(company_info, loc):
            continue
        desc = j.get("descriptionPlain", "") or html_to_text(j.get("descriptionHtml", ""))
        out.append(make_candidate(company_info, title, job_url, loc, desc))
    return out


def _comeet_positions_to_candidates(company_info, positions):
    out = []
    for p in positions or []:
        title = p.get("name", "")
        loc_obj = p.get("location") or {}
        country = "Israel" if str(loc_obj.get("country", "")).upper() == "IL" else loc_obj.get("country")
        loc = ", ".join(filter(None, [loc_obj.get("city"), loc_obj.get("name"), country])) or "Unspecified"
        job_url = p.get("url_active_page") or p.get("url_comeet_hosted_page") or ""
        if not job_url or not is_title_candidate(title, job_url) or not ats_location_ok(company_info, loc):
            continue
        details = p.get("details") or (p.get("custom_fields") or {}).get("details") or []
        desc = "\n".join(f"{d.get('name', '')}:\n{html_to_text(d.get('value', ''))}" for d in details if isinstance(d, dict))
        if p.get("experience_level"):
            desc = f"Experience level: {p['experience_level']}\n{desc}"
        out.append(make_candidate(company_info, title, job_url, loc, desc))
    return out


def _scrape_comeet_hosted(company_info, url):
    r = requests.get(url, headers=HEADERS, timeout=15)
    m = re.search(r"COMPANY_POSITIONS_DATA\s*=\s*\[", r.text) if r.status_code == 200 else None
    if not m:
        return None
    positions, _ = json.JSONDecoder().raw_decode(r.text[m.end() - 1:])
    return _comeet_positions_to_candidates(company_info, positions)


def _scrape_comeet_api(company_info, uid, token):
    r = requests.get(
        f"https://www.comeet.co/careers-api/2.0/company/{uid}/positions?token={token}&details=true",
        headers=HEADERS, timeout=20
    )
    if r.status_code != 200:
        return None
    return _comeet_positions_to_candidates(company_info, r.json())


def _scrape_workday(company_info, tenant, wd, site):
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}"
    s = requests.Session()
    try:
        s.get(f"{base}/{site}", headers=HEADERS, timeout=15)
    except requests.RequestException:
        pass
    # Workday rate-limits (429) requests that lack Origin/Referer.
    wd_headers = {**JSON_HEADERS, "Origin": base, "Referer": f"{base}/{site}"}
    postings, offset = [], 0
    while offset < 600:
        r = s.post(f"{api}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": "Israel"},
                   headers=wd_headers, timeout=20)
        if r.status_code != 200:
            if offset == 0:
                return None
            break
        data = r.json()
        page = data.get("jobPostings", [])
        postings.extend(page)
        offset += 20
        if not page or offset >= data.get("total", 0):
            break
        time.sleep(0.3)

    out = []
    for p in postings:
        title = p.get("title", "")
        path = p.get("externalPath", "")
        job_url = f"{base}/{site}{path}"
        list_loc = f"{p.get('locationsText', '')} {path.split('/')[2] if path.count('/') >= 2 else ''}".replace("-", " ")
        if not is_title_candidate(title, job_url):
            continue
        if "israel" not in list_loc.lower() and "locations" not in list_loc.lower():
            continue
        try:
            d = s.get(f"{api}{path}", headers=wd_headers, timeout=20)
            info = d.json().get("jobPostingInfo", {}) if d.status_code == 200 else {}
        except Exception:
            info = {}
        country = (info.get("country") or {}).get("descriptor", "")
        locs = [info.get("location", "")] + list(info.get("additionalLocations") or [])
        loc = " / ".join(filter(None, locs)) or list_loc
        if country and country.lower() != "israel" and "israel" not in loc.lower():
            continue
        if not ats_location_ok(company_info, loc):
            continue
        out.append(make_candidate(company_info, title, job_url, loc, html_to_text(info.get("jobDescription", ""))))
        time.sleep(0.2)
    return out


def _scrape_smartrecruiters(company_info, company_id):
    out, offset = [], 0
    while offset < 500:
        r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings?country=il&limit=100&offset={offset}",
                         headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None if offset == 0 else out
        data = r.json()
        for p in data.get("content", []):
            title = p.get("name", "")
            loc_obj = p.get("location") or {}
            loc = ", ".join(filter(None, [loc_obj.get("city"), loc_obj.get("region"), loc_obj.get("country")]))
            job_url = f"https://jobs.smartrecruiters.com/{company_id}/{p.get('id')}"
            if not is_title_candidate(title, job_url) or not ats_location_ok(company_info, loc):
                continue
            desc = ""
            try:
                d = requests.get(p.get("ref", ""), headers=HEADERS, timeout=15).json()
                sections = (d.get("jobAd") or {}).get("sections") or {}
                desc = "\n".join(html_to_text((sections.get(k) or {}).get("text", ""))
                                 for k in ("jobDescription", "qualifications", "additionalInformation"))
            except Exception:
                pass
            out.append(make_candidate(company_info, title, job_url, loc, desc))
        offset += 100
        if offset >= data.get("totalFound", 0):
            break
    return out


def _scrape_amazon(company_info):
    out, offset = [], 0
    while offset < 600:
        r = requests.get(f"https://www.amazon.jobs/en/search.json?country=ISR&result_limit=100&offset={offset}",
                         headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return None if offset == 0 else out
        data = r.json()
        for j in data.get("jobs", []):
            title = j.get("title", "")
            loc = j.get("normalized_location") or j.get("location") or j.get("city", "")
            job_url = "https://www.amazon.jobs" + j.get("job_path", "")
            if not is_title_candidate(title, job_url) or not ats_location_ok(company_info, loc):
                continue
            desc = "\n".join(filter(None, [
                html_to_text(j.get("description", "")),
                "Basic qualifications:\n" + html_to_text(j.get("basic_qualifications", "")),
                "Preferred qualifications:\n" + html_to_text(j.get("preferred_qualifications", "")),
            ]))
            out.append(make_candidate(company_info, title, job_url, loc, desc))
        offset += 100
        if offset >= data.get("hits", 0):
            break
    return out


def resolve_scrape_target(company_info):
    return (
        company_info.get("ats") or
        company_info.get("official_scrape_url") or
        company_info.get("career_url") or
        company_info.get("link", "")
    ).strip()


def try_ats_api_scrape(company_info, url):
    """
    Detects supported ATS boards and queries their public JSON APIs directly.
    Returns (handled: bool, candidates: list)
    """
    try:
        if url.startswith("comeet-api:"):
            _, uid, token = url.split(":", 2)
            res = _scrape_comeet_api(company_info, uid, token)
            return (res is not None), (res or [])

        parsed = urlparse(url)
        host = parsed.netloc.lower()
        path_parts = [p for p in parsed.path.split("/") if p]
        res = None

        if "greenhouse.io" in host and path_parts and path_parts[0] != "embed":
            res = _scrape_greenhouse(company_info, path_parts[0])
        elif host in ("jobs.lever.co", "jobs.eu.lever.co") and path_parts:
            res = _scrape_lever(company_info, path_parts[0], eu=host.startswith("jobs.eu."))
        elif host == "jobs.ashbyhq.com" and path_parts:
            res = _scrape_ashby(company_info, path_parts[0])
        elif "comeet." in host and len(path_parts) >= 3 and path_parts[0] == "jobs":
            res = _scrape_comeet_hosted(company_info, f"https://www.comeet.com/jobs/{path_parts[1]}/{path_parts[2]}")
        elif host.endswith(".myworkdayjobs.com") and path_parts:
            tenant, wd = host.split(".")[0], host.split(".")[1]
            site = path_parts[1] if re.match(r"^[a-z]{2}-[A-Z]{2}$", path_parts[0]) and len(path_parts) > 1 else path_parts[0]
            res = _scrape_workday(company_info, tenant, wd, site)
        elif host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and path_parts:
            res = _scrape_smartrecruiters(company_info, path_parts[0])
        elif host.endswith("amazon.jobs"):
            res = _scrape_amazon(company_info)
        else:
            return False, []

        if res is None:
            return False, []
        return True, res
    except Exception as exc:
        print(f"  [!] ATS API error for {company_info['name']}: {type(exc).__name__}: {exc}")
        return False, []


# --- HTML Scraping Logic ---

def is_title_candidate(title, url=""):
    t = re.sub(r"\s+", " ", title or "").strip()
    if len(t) < 4 or len(t) > 130:
        return False
    if NAV_EXCLUDE_REGEX.match(t):
        return False
    if NON_JOB_URL_PATTERNS.search(url):
        return False
    if AGENCY_CATEGORY_REGEX.search(url):
        return False
    if IS_ISRAEL_TARGET:
        if EUROPE_GENDER_REGEX.search(t):
            return False
        if FOREIGN_LOCATION_REGEX.search(f"{t} {url}") and not ISRAEL_LOCATION_REGEX.search(f"{t} {url}"):
            return False
    if ALWAYS_REJECT_REGEX.search(t):
        return False
    if ji.classify_non_job(t, url):
        return False
    return ACTIVE_TITLE_FILTER.matches(t)


def _find_card_heading(a, max_levels=4):
    """Walk up from a generic CTA link to the nearest card heading (job title)."""
    node = a
    for _ in range(max_levels):
        node = node.parent
        if node is None:
            return None, ""
        heading = node.find(["h1", "h2", "h3", "h4", "h5", "strong"])
        if heading and not GENERIC_CTA_REGEX.match(heading.get_text(" ", strip=True)):
            return heading.get_text(separator=" ", strip=True), node.get_text(separator=" ", strip=True)
    return None, ""


# HTML pages: a vacancy link almost always has one of these in its URL. Filters out product / marketing pages
# whose link text happens to contain "planning", "operations", etc.
JOB_URL_HINT_REGEX = re.compile(
    r"(job|career|position|opening|vacanc|apply|role|opportunit|recruit|hiring|join|/co/|/details/|"
    r"comeet|greenhouse|lever\.co|ashbyhq|myworkdayjobs|smartrecruiters|bamboohr|workable|hibob|"
    r"%D7%9E%D7%A9%D7%A8|%D7%93%D7%A8%D7%95%D7%A9|משר|דרוש|קריירה)",
    re.IGNORECASE
)


def extract_candidates_from_soup(soup, base_url, company_info):
    links = soup.find_all("a", href=True)
    found_candidates = []
    seen_urls = set()
    base_tag = soup.find("base", href=True)
    if base_tag:
        base_url = urljoin(base_url, base_tag["href"])

    for a in links:
        title = a.get_text(separator=" ", strip=True)
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("javascript:") or href.startswith("mailto:"):
            continue

        parent_context = a.parent.get_text(separator=" ", strip=True) if a.parent else ""

        # Empty link (icon / whole-card link) or generic CTA ("Read More", "פרטי המשרה") -> nearest card heading is the title
        if not title or GENERIC_CTA_REGEX.match(title):
            heading, ctx = _find_card_heading(a)
            if not heading:
                continue
            title, parent_context = heading, ctx or parent_context

        full_url = urljoin(base_url, href)
        if full_url in seen_urls or not JOB_URL_HINT_REGEX.search(full_url):
            continue
        if is_title_candidate(title, full_url):
            if location_verdict(company_info["name"], parent_context[:400]) == "reject":
                continue
            if IS_ISRAEL_TARGET and EUROPE_GENDER_REGEX.search(parent_context) and not ISRAEL_LOCATION_REGEX.search(parent_context):
                continue
            seen_urls.add(full_url)
            found_candidates.append(make_candidate(company_info, title, full_url, "Unspecified", "", parent_context[:300]))

    return found_candidates


def scrape_single_company(company_info):
    name = company_info["name"]
    url = resolve_scrape_target(company_info)

    if not url:
        return name, False, [], "Missing career URL", company_info
    if "linkedin.com/in/" in url or "linkedin.com/company/" in url:
        return name, False, [], "LinkedIn profile/company page (direct outreach recommended)", company_info

    # Try direct ATS JSON API first
    ats_handled, ats_candidates = try_ats_api_scrape(company_info, url)
    if ats_handled:
        for c in ats_candidates:
            if c["source_type"] == "career_page":
                c["source_type"] = "ats_api"
        return name, True, ats_candidates, "ATS API scrape successful", company_info

    html_url = url if url.startswith("http") else (company_info.get("career_url") or "")
    if not html_url:
        return name, False, [], "ATS API failed and no HTML URL", company_info

    try:
        resp = requests.get(html_url, headers=HEADERS, timeout=10)
        if resp.status_code in [403, 429, 503]:
            return name, False, [], f"Blocked by server (HTTP {resp.status_code})", company_info
        if resp.status_code != 200:
            return name, False, [], f"HTTP error {resp.status_code}", company_info
        text = response_text(resp)
        if "challenge-running" in text or "cf-browser-verification" in text:
            return name, False, [], "Cloudflare bot challenge detected", company_info

        soup = BeautifulSoup(text, "html.parser")
        found_candidates = extract_candidates_from_soup(soup, html_url, company_info)

        body_len = len(soup.get_text(strip=True))
        if len(found_candidates) == 0 and body_len < 600:
            return name, False, [], "Client-side rendered SPA (requires JS/browser)", company_info

        return name, True, found_candidates, "Scrape successful", company_info

    except requests.exceptions.RequestException as e:
        return name, False, [], f"Network exception: {type(e).__name__}", company_info


def scrape_spa_with_playwright(failed_list, companies_by_name):
    """Uses Headless Chromium to render JS-heavy career pages that failed static requests."""
    if not PLAYWRIGHT_AVAILABLE or not failed_list:
        return [], failed_list

    retryable = [
        f for f in failed_list
        if ("SPA" in f.get("reason", "") or "403" in f.get("reason", "")) and str(f.get("link", "")).startswith("http")
    ]
    non_retryable = [f for f in failed_list if f not in retryable]

    if not retryable:
        return [], failed_list

    print(f"\n=== STAGE 1B: Headless Browser (Playwright) Fallback ({len(retryable)} JS/protected sites) ===")
    recovered_candidates = []
    still_failed = list(non_retryable)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = context.new_page()

            for item in retryable:
                c_name = item["name"]
                url = item["link"]
                c_info = companies_by_name.get(c_name, {"name": c_name})
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(3000)
                    html = page.content()
                    soup = BeautifulSoup(html, "html.parser")
                    cands = extract_candidates_from_soup(soup, url, c_info)
                    if cands:
                        recovered_candidates.extend(cands)
                        print(f"  [+] Playwright recovered {len(cands)} candidates from {c_name}")
                    else:
                        still_failed.append(item)
                except Exception:
                    still_failed.append(item)

            browser.close()
    except Exception as exc:
        print(f"  [!] Playwright stage error: {exc}")
        return recovered_candidates, failed_list

    return recovered_candidates, still_failed


def extract_clean_text_from_soup(soup):
    extracted_text = ""
    detected_loc = "Unspecified"

    for script_tag in soup.find_all("script", type="application/ld+json"):
        try:
            js_data = json.loads(script_tag.string or "{}")
            if isinstance(js_data, list):
                js_data = js_data[0] if js_data else {}
            if js_data.get("@type") == "JobPosting":
                loc_obj = js_data.get("jobLocation", {})
                if isinstance(loc_obj, list):
                    loc_obj = loc_obj[0] if loc_obj else {}
                addr = loc_obj.get("address", {})
                locality = addr.get("addressLocality") or ""
                country = addr.get("addressCountry") or ""
                if isinstance(country, dict):
                    country = country.get("name", "")
                if locality or country:
                    detected_loc = f"{locality}, {country}".strip(", ")
                desc = js_data.get("description", "")
                if desc:
                    clean_desc = html_to_text(desc)
                    if len(clean_desc) > 200:
                        extracted_text = clean_desc[:6000]
                        break
        except Exception:
            pass

    if detected_loc == "Unspecified":
        loc_candidates = []
        for tag in soup.find_all(["span", "div", "p", "li"], class_=re.compile(r"locat|workplace|city|country|area|region", re.I)):
            txt = tag.get_text(strip=True)
            if 2 < len(txt) < 60:
                loc_candidates.append(txt)
        if loc_candidates:
            detected_loc = " / ".join(loc_candidates[:2])

    if not extracted_text:
        for elem in soup(["script", "style", "nav", "footer", "noscript", "svg", "header"]):
            elem.decompose()
        body_text = soup.get_text(separator="\n", strip=True)
        lines = [line.strip() for line in body_text.splitlines() if len(line.strip()) > 3]
        extracted_text = "\n".join(lines)[:6000]

    return extracted_text, detected_loc


def fetch_job_details(job):
    # If an ATS API already populated full requirements text, keep it!
    if len(job.get("requirements_text", "")) >= 200:
        return job

    url = job["url"]
    extracted_text = ""
    detected_loc = job.get("page_location", "Unspecified")

    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            soup = BeautifulSoup(response_text(r), "html.parser")
            extracted_text, detected_loc = extract_clean_text_from_soup(soup)
    except Exception:
        pass

    job["requirements_text"] = extracted_text
    job["page_location"] = detected_loc
    return job


def fetch_missing_details_with_playwright(jobs_list):
    """For candidate jobs where static requests returned empty/JS shell, render with Playwright."""
    if not PLAYWRIGHT_AVAILABLE:
        return jobs_list

    missing = [j for j in jobs_list if len(j.get("requirements_text", "")) < 150]
    if not missing:
        return jobs_list

    print(f"[*] Rendering {len(missing)} JS job pages with Playwright to read full requirements...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = context.new_page()

            for job in missing:
                try:
                    page.goto(job["url"], wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(2000)
                    soup = BeautifulSoup(page.content(), "html.parser")
                    txt, loc = extract_clean_text_from_soup(soup)
                    if len(txt) > len(job.get("requirements_text", "")):
                        job["requirements_text"] = txt
                    if loc != "Unspecified":
                        job["page_location"] = loc
                except Exception:
                    pass

            browser.close()
    except Exception as exc:
        print(f"  [!] Playwright job detail fetch error: {exc}")

    return jobs_list


# --- Strict Config-Driven Batch AI Evaluation ---

def batch_verify_jobs_with_ai(jobs_batch):
    min_yoe = int(USER_CONFIG.get("min_yoe", 0))
    max_yoe = int(USER_CONFIG.get("max_yoe", 2))
    target_roles_str = ", ".join(USER_CONFIG.get("target_roles", []))
    excluded_roles_str = "; ".join(USER_CONFIG.get("excluded_roles", []))
    include_students = USER_CONFIG.get("include_student_and_graduate", True)
    allowed_cities_str = ", ".join(c for c in USER_CONFIG.get("allowed_cities", []) if re.search(r"[a-zA-Z]", c))

    student_clause = (
        "including Junior, Associate, Graduate / Graduate-Program, Student and Intern roles"
        if include_students else
        "excluding Student/Intern roles unless they meet the full-time YOE range"
    )

    items_text = []
    for idx, j in enumerate(jobs_batch, 1):
        snippet = j.get("requirements_text", "").strip()
        loc = j.get("page_location", "Unspecified")
        exempt = is_location_exempt(j.get("registry_name", j["company"]))
        if exempt or not RESTRICT_TO_ALLOWED_CITIES:
            loc_rule = f"ANYWHERE IN {TARGET_LOCATION.upper()} (any Israeli city is fine)"
        else:
            loc_rule = (f"ONLY THESE CITIES: {allowed_cities_str}. Reject any other Israeli city (e.g. Haifa, Jerusalem, "
                        "Yokneam, Beer Sheva, Netanya, and any city not in that list).")
        items_text.append(
            f"=== Job #{idx} ===\n"
            f"Company / Source: {j['company']}\n"
            f"Title: {j['title']}\n"
            f"URL: {j['url']}\n"
            f"Detected Page Location: {loc}\n"
            f"Location rule for this job: {loc_rule}\n"
            f"Full Job Description & Requirements:\n{snippet[:5500]}\n"
        )

    candidate_desc = USER_CONFIG.get("description") or f"a candidate looking for: {target_roles_str}"
    role_guidance = USER_CONFIG.get("ai_role_guidance", "")
    if max_yoe <= 2:
        stretch_def = "stretch_2_plus = minimum exactly 2 years"
        senior_def = "senior = minimum 3+ years, or senior/lead/managing a team"
    else:
        stretch_def = f"stretch_2_plus = minimum of 2 to {max_yoe} years"
        senior_def = (f"senior = minimum {max_yoe + 1}+ years, or a people-management role (team lead / head of / director / "
                      f"VP). A 'Senior' title alone is NOT senior: judge by the stated years")
    prompt = (
        f"You are an expert hiring validator. The candidate profile is: {candidate_desc}\n"
        "Carefully read the FULL text of each job posting below (English or Hebrew), paying close attention to the "
        "requirements section and location, and evaluate each posting on 4 mandatory criteria:\n\n"
        "1. Single Specific Job Vacancy (is_single_specific_job):\n"
        "   - MUST be an actual, individual, open job vacancy with a concrete description.\n"
        "   - STRICTLY REJECT: category lobbies, search results pages, recruitment-agency hubs, articles/guides/courses, "
        "generic 'Send CV / Join Talent Pool' forms, login/logout pages.\n\n"
        "2. Target Role Match (is_target_role):\n"
        f"   - MUST be one of (or clearly equivalent to) these target roles: {target_roles_str}.\n"
        f"   - STRICTLY REJECT these excluded role families: {excluded_roles_str}.\n"
        f"   {role_guidance}\n"
        "   - Judge by the DUTIES, not by single words: 'Sales Operations Analyst' is analytical, not frontline sales; "
        "'Product Operations' is not facilities operations.\n\n"
        f"3. Experience Level (is_matching_yoe) - target {min_yoe}-{max_yoe} years ({student_clause}):\n"
        f"   - Classify experience_level as exactly one of: {', '.join(ji.EXPERIENCE_LEVELS)}.\n"
        "     explicit_student = student/intern position; explicit_junior = says junior/graduate/entry-level/no experience; "
        f"explicit_0_2 = states a minimum of 0-1 years or 'up to 2 years'; {stretch_def}; "
        f"unspecified = the text does NOT state any experience requirement; {senior_def}.\n"
        "   - Quote the exact phrase you relied on in experience_evidence ('' when unspecified). NEVER infer 0-2 from silence.\n"
        f"   - is_matching_yoe is TRUE for explicit_student, explicit_junior, explicit_0_2, stretch_2_plus and unspecified; "
        f"FALSE only for senior (minimum >= {max_yoe + 1} years or clearly managerial).\n\n"
        "4. Target Location (is_matching_location):\n"
        "   - Apply the 'Location rule for this job' given with each posting. Hybrid/remote within Israel is fine.\n"
        f"   - STRICTLY REJECT positions located outside {TARGET_LOCATION} (or with (m/f/d)).\n"
        "   - If the posting does not state a city, accept it only if it is an Israeli company/office with no sign of being "
        "outside the allowed region.\n\n"
        "Candidate Postings:\n"
        f"{''.join(items_text)}\n"
        "A job is APPROVED only if ALL FOUR criteria are strictly TRUE.\n\n"
        f"Also classify every job into ONE role_category from: {', '.join(ROLE_CATEGORIES)}.\n"
        "Also rate role_match (0.0-1.0: how central the role is to the target roles) and skills_fit (0.0-1.0: overlap of "
        "the requirements with the candidate profile).\n\n"
        "Output strictly valid JSON with this exact structure:\n"
        "{\n"
        '  "evaluations": [\n'
        "    {\n"
        '      "job_index": 1,\n'
        '      "is_single_specific_job": true/false,\n'
        '      "is_target_role": true/false,\n'
        '      "is_matching_yoe": true/false,\n'
        '      "is_matching_location": true/false,\n'
        '      "approved": true/false,\n'
        '      "role_category": "one of the categories above",\n'
        '      "experience_level": "one of the levels above",\n'
        '      "experience_evidence": "exact quoted phrase or empty",\n'
        '      "role_match": 0.0,\n'
        '      "skills_fit": 0.0,\n'
        f'      "experience_summary": "short summary, e.g. Student position / 1-2 years / not stated",\n'
        '      "detected_city": "e.g. Tel Aviv",\n'
        '      "rejection_reason": "short explanation if rejected"\n'
        "    }\n"
        "  ]\n"
        "}"
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.0,
            "responseMimeType": "application/json"
        }
    }

    try:
        data = call_gemini_with_retries(payload, timeout=60)
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        parsed = json.loads(raw_text)
        return parsed.get("evaluations", [])
    except Exception as e:
        print(f"    [!] Error during batch AI verification: {e}")
        return []


# --- Excel Generation ---

def export_to_excel(confirmed_jobs, unreached_companies, filepath):
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "New Jobs with Referrals"
    ws1.append([
        "שם החברה",
        "שם המשרה",
        "קטגוריה",
        "איש קשר להגשת רפרל",
        "סוג רפרל / הגשה",
        "מיקום",
        "קישור למשרה",
        "דרישות ניסיון",
        "תאריך גילוי"
    ])

    default_exp_label = f"{USER_CONFIG['min_yoe']}-{USER_CONFIG['max_yoe']} YOE (AI Verified)"
    for nj in confirmed_jobs:
        ws1.append([
            nj["company"],
            nj["title"],
            nj.get("role_category", ""),
            nj.get("referral_contact", DEFAULT_REFERRAL_CONTACT),
            nj.get("referral_type", DEFAULT_REFERRAL_TYPE),
            nj.get("location", TARGET_LOCATION),
            nj["url"],
            nj.get("experience", default_exp_label),
            nj.get("discovered_at", datetime.now().strftime("%Y-%m-%d"))
        ])

    if unreached_companies:
        ws2 = wb.create_sheet(title="לבדיקה ידנית ורפרל ישיר")
        ws2.append(["שם החברה", "איש קשר לרפרל", "קישור / פרופיל", "סיבה"])
        for uc in unreached_companies:
            ws2.append([
                uc["name"],
                uc.get("referral_contact", ""),
                uc.get("link", ""),
                uc.get("reason", "Requires manual check")
            ])

    for sheet in wb.worksheets:
        for col in sheet.columns:
            max_len = max(len(str(cell.value or '')) for cell in col)
            col_letter = openpyxl.utils.get_column_letter(col[0].column)
            sheet.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 55)

    wb.save(filepath)
    print(f"[✓] Real-time Excel exported: {len(confirmed_jobs)} confirmed job(s) -> {filepath}")


# --- Main Execution ---

def dedupe_companies_by_target(companies):
    """Several registry names can point at the same ATS board (e.g. acquisitions) - scrape each board once."""
    seen_targets, unique = set(), []
    for c in companies:
        target = resolve_scrape_target(c).lower().rstrip("/")
        if target and target in seen_targets:
            continue
        seen_targets.add(target)
        unique.append(c)
    return unique


EVAL_CACHE_FILE = os.path.join(BASE_DIR, "data", "evaluated_jobs_cache.json")
HEALTH_FILE = os.path.join(BASE_DIR, "data", "crawl_health.json")
HEALTH_FAILURE_ALERT = 3


def candidate_key(job):
    return ji.job_key(job["company"], job["title"], job["url"], f"{job.get('page_location', '')} {job['title']}")


def record_key(rec):
    return rec.get("job_key") or ji.job_key(rec.get("company", ""), rec.get("title", ""), rec.get("url", ""),
                                            f"{rec.get('location', '')} {rec.get('title', '')}")


def load_eval_cache():
    cache = load_json(EVAL_CACHE_FILE, {"schema_version": 1, "entries": {}})
    cache.setdefault("entries", {})
    return cache


def eval_cache_fresh(cache, key, max_days):
    e = cache["entries"].get(key)
    if not e or e.get("profile") != PROFILE_NAME:
        return False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(e["checked_at"])
    except (KeyError, ValueError):
        return False
    return age.days < max_days


def remember_rejection(cache, key, job, stage, reason):
    cache["entries"][key] = {
        "profile": PROFILE_NAME, "decision": "rejected", "stage": stage, "reason": str(reason)[:300],
        "company": job["company"], "title": job["title"], "url": job["url"], "checked_at": storage.now_iso(),
    }


def prune_eval_cache(cache, max_days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_days * 2)
    keep = {}
    for k, e in cache["entries"].items():
        try:
            if datetime.fromisoformat(e["checked_at"]) >= cutoff:
                keep[k] = e
        except (KeyError, ValueError):
            pass
    cache["entries"] = keep


def update_health(health, source_name, ok, reason, n_candidates):
    s = health["sources"].setdefault(source_name, {"consecutive_failures": 0})
    s["last_checked_at"] = storage.now_iso()
    s["last_status"] = "ok" if ok else "failed"
    s["last_reason"] = reason
    s["last_candidates"] = n_candidates
    if ok:
        s["consecutive_failures"] = 0
        s["last_success_at"] = s["last_checked_at"]
    else:
        s["consecutive_failures"] = s.get("consecutive_failures", 0) + 1


def allowed_location_tokens():
    return {ji.normalize_location(c) for c in USER_CONFIG.get("allowed_cities", [])} - {"unknown", "israel", "foreign"}


def build_entry(job, ev):
    """A new seen_jobs record: legacy 8 fields + role_category + record_version-2 fields."""
    location = ev.get("detected_city") or job.get("page_location") or TARGET_LOCATION
    level = ev.get("experience_level") if ev.get("experience_level") in ji.EXPERIENCE_LEVELS else None
    evidence = ev.get("experience_evidence") or ""
    det_level, det_evidence = ji.parse_experience_requirement(job["title"], job.get("requirements_text", ""),
                                                            max_yoe=int(USER_CONFIG.get("max_yoe", 2)))
    # The AI may miss an explicit marker (e.g. "Student Position" in the title); explicit evidence beats 'unspecified'.
    if not level or (level == "unspecified" and det_level != "unspecified"):
        level, evidence = det_level, det_evidence
    loc_token = ji.normalize_location(f"{location} {job.get('page_location', '')}")
    try:
        role_match = float(ev.get("role_match", 0.7))
        skills_fit = float(ev.get("skills_fit", 0.5))
    except (TypeError, ValueError):
        role_match, skills_fit = 0.7, 0.5
    category = ev.get("role_category") if ev.get("role_category") in ROLE_CATEGORIES else "other"
    # role_category_weights lets a profile keep a role family but rank it below the core ones.
    try:
        role_match *= float((USER_CONFIG.get("role_category_weights") or {}).get(category, 1.0))
    except (TypeError, ValueError):
        pass
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    provider, jid = ji.extract_ats_job_id(job["url"])
    return {
        # --- legacy contract (index.html, Excel) ---
        "company": job["company"],
        "title": job["title"],
        "role_category": category,
        "referral_contact": job.get("referral_contact", DEFAULT_REFERRAL_CONTACT),
        "referral_type": job.get("referral_type", DEFAULT_REFERRAL_TYPE),
        "location": location,
        "url": job["url"],
        "experience": ev.get("experience_summary") or ji.EXPERIENCE_LABELS_HE.get(level, ""),
        "discovered_at": datetime.now().isoformat(),
        # --- record_version 2 ---
        "record_version": 2,
        "job_key": candidate_key(job),
        "ats_provider": provider,
        "ats_job_id": jid,
        "canonical_url": ji.canonicalize_url(job["url"]),
        "display_title": job["title"],
        "raw_title": job.get("raw_title", job["title"]),
        "location_token": loc_token,
        "status": "active",
        "status_reasons": [],
        "duplicate_of": None,
        "experience_level": level,
        "experience_evidence": evidence[:200],
        "profiles": [PROFILE_NAME],
        "first_seen_at": now,
        "last_seen_at": now,
        "quality_flags": [],
        "source_type": job.get("source_type", "career_page"),
        "source_name": job.get("source_name", job["company"]),
        "priority": job.get("priority"),
        "sector": job.get("sector"),
        "match_score": ji.match_score(role_match, level, loc_token, job.get("priority"), skills_fit, 1.0,
                                      USER_CONFIG.get("ranking_weights"), allowed_location_tokens(),
                                      eligibility_points=USER_CONFIG.get("eligibility_points")),
    }


def select_new_candidates(direct_candidates, seen_by_key, seen_hashes, eval_cache, cache_days):
    """
    Identity-based dedupe (never URL-only: shared application forms keep distinct titles).
    Already-seen jobs refresh last_seen_at (date granularity keeps bot commits small) and are skipped;
    recently rejected jobs are skipped until the evaluation cache expires.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    unique_candidates, candidate_keys = [], set()
    skipped = {"already_seen": 0, "recently_rejected": 0, "duplicate_in_run": 0, "excluded_company": 0}
    for cand in direct_candidates:
        if is_company_excluded(cand.get("company"), cand.get("registry_name")):
            skipped["excluded_company"] += 1
            continue
        key = candidate_key(cand)
        cand["job_key"] = key
        if key in seen_by_key or compute_job_hash(cand["company"], cand["title"], cand["url"]) in seen_hashes:
            skipped["already_seen"] += 1
            rec = seen_by_key.get(key)
            if rec is not None and str(rec.get("last_seen_at", ""))[:10] < today:
                rec["last_seen_at"] = storage.now_iso()
            continue
        if key in candidate_keys:
            skipped["duplicate_in_run"] += 1
            continue
        if eval_cache_fresh(eval_cache, key, cache_days):
            skipped["recently_rejected"] += 1
            continue
        candidate_keys.add(key)
        unique_candidates.append(cand)
    return unique_candidates, skipped


def collect_linkedin_candidates(seen_keys, eval_cache, cache_days, health):
    """Stage 1C: LinkedIn public guest search -> candidates with full descriptions (sequential, rate-limited)."""
    import linkedin_source as li
    settings = GLOBAL_CONFIG.get("linkedin", {})
    queries = USER_CONFIG.get("linkedin_queries", [])
    if not settings.get("enabled") or not queries:
        return []
    print(f"\n=== STAGE 1C: LinkedIn public job search ({len(queries)} queries) ===")
    cards, stats = li.search(queries, settings, is_title_candidate,
                             lambda company, loc: location_verdict(company, loc) != "reject")
    out, details = [], 0
    max_details = int(settings.get("max_detail_fetches_per_run", 150))
    blocked = stats.get("blocked")
    for c in cards:
        if is_company_excluded(c["company"]):
            continue
        info = {"name": c["company"] or "LinkedIn", "source_type": "linkedin"}
        job = make_candidate(info, c["title"], li.canonical_job_url(c["job_id"]), c["location"])
        key = candidate_key(job)
        if key in seen_keys or eval_cache_fresh(eval_cache, key, cache_days):
            continue
        if blocked or details >= max_details:
            break
        try:
            desc, criteria = li.fetch_detail(c["job_id"])
            details += 1
        except li.LinkedInBlocked as e:
            blocked = str(e)
            print(f"  [!] LinkedIn blocked detail pages ({e}) - continuing without more LinkedIn details.")
            break
        job["requirements_text"] = (f"LinkedIn criteria: {criteria}\n{desc}" if criteria else desc)[:6000]
        out.append(job)
        time.sleep(float(settings.get("delay_seconds", 2.0)))
    stats["details_fetched"] = details
    stats["candidates"] = len(out)
    update_health(health, "LinkedIn (guest search)", not blocked and stats["requests"] > 0,
                  blocked or f"{stats['requests']} searches, {stats['cards']} cards", len(out))
    print(f"[*] LinkedIn: {stats}")
    return out


def main():
    test_gemini_preflight()
    run_started = storage.now_iso()
    cache_days = int(GLOBAL_CONFIG.get("evaluation_cache_days", 14))
    force_all = os.getenv("CRAWL_ALL_PRIORITIES", "").lower() in ("1", "true", "yes")

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Profile: {PROFILE_NAME} | registry: {os.path.basename(MASTER_FILE)}")

    import watchlist
    registry = load_json(MASTER_FILE, [])
    registry, wl_sources, wl_stats = watchlist.build_watchlist_sources(registry, GLOBAL_CONFIG, force_all=force_all,
                                                                       exclude_fn=is_company_excluded)
    print(f"[*] Watchlist: {wl_stats}")
    all_sources = registry + wl_sources
    companies = dedupe_companies_by_target([c for c in all_sources if not is_company_excluded(c["name"], c.get("display_name"))])
    if len(companies) < len(dedupe_companies_by_target(all_sources)):
        print(f"[*] Excluded companies (config excluded_companies): {len(dedupe_companies_by_target(all_sources)) - len(companies)} sources skipped")
    companies_by_name = {c["name"]: c for c in companies}

    seen_jobs = load_json(SEEN_JOBS_FILE, [])
    seen_count_before = len(seen_jobs)
    seen_by_key = {}
    for rec in seen_jobs:
        seen_by_key.setdefault(record_key(rec), rec)
    seen_keys = set(seen_by_key)
    seen_hashes = {compute_job_hash(j["company"], j["title"], j["url"]) for j in seen_jobs}
    eval_cache = load_eval_cache()
    health = load_json(HEALTH_FILE, {"schema_version": 1, "sources": {}, "runs": []})
    health.setdefault("sources", {})
    health.setdefault("runs", [])

    checkpoint = load_json(CHECKPOINT_FILE, {
        "scraped_companies": [],
        "confirmed_jobs": [],
        "failed_companies": [],
        "pending_candidates": []
    })

    scraped_set = set(checkpoint.get("scraped_companies", []))
    confirmed_new_jobs = checkpoint.get("confirmed_jobs", [])
    failed_companies = checkpoint.get("failed_companies", [])
    direct_candidates = checkpoint.get("pending_candidates", [])

    if scraped_set:
        print(f"[*] Resuming from checkpoint: {len(scraped_set)}/{len(companies)} processed, {len(confirmed_new_jobs)} jobs found.")

    # --- STAGE 1: Parallel Deterministic & ATS API Scrape ---
    remaining_companies = [c for c in companies if c["name"] not in scraped_set]

    if remaining_companies:
        print(f"\n=== STAGE 1: High-Speed Parallel Scraping ({len(remaining_companies)} sources) ===")
        with ThreadPoolExecutor(max_workers=12) as executor:
            future_to_company = {executor.submit(scrape_single_company, c): c for c in remaining_companies}
            completed_count = 0
            for future in as_completed(future_to_company):
                c = future_to_company[future]
                completed_count += 1
                try:
                    c_name, success, jobs, reason, c_info = future.result()
                    update_health(health, c_name, success, reason, len(jobs))
                    if not success:
                        failed_companies.append({
                            "name": c_name,
                            "link": c_info.get("career_url") or c_info.get("link", ""),
                            "referral_contact": c_info.get("referral_contact", ""),
                            "reason": reason
                        })
                    elif jobs:
                        direct_candidates.extend(jobs)
                    scraped_set.add(c["name"])
                except Exception as exc:
                    update_health(health, c["name"], False, f"Execution error: {exc}", 0)
                    failed_companies.append({
                        "name": c["name"],
                        "link": c.get("career_url") or c.get("link", ""),
                        "referral_contact": c.get("referral_contact", ""),
                        "reason": f"Execution error: {exc}"
                    })
                    scraped_set.add(c["name"])

                if completed_count % 30 == 0 or completed_count == len(remaining_companies):
                    checkpoint["scraped_companies"] = list(scraped_set)
                    checkpoint["failed_companies"] = failed_companies
                    checkpoint["pending_candidates"] = direct_candidates
                    storage.atomic_write_json(CHECKPOINT_FILE, checkpoint)
                    print(f"  -> Progress: {completed_count}/{len(remaining_companies)} sources scraped ({len(direct_candidates)} candidates found)...")

        # STAGE 1B: Playwright Headless Fallback for JS/SPA sites
        recovered, failed_companies = scrape_spa_with_playwright(failed_companies, companies_by_name)
        if recovered:
            direct_candidates.extend(recovered)
            for name in {r["registry_name"] for r in recovered}:
                update_health(health, name, True, "Recovered with Playwright",
                              sum(1 for r in recovered if r["registry_name"] == name))

        # STAGE 1C: LinkedIn
        direct_candidates.extend(collect_linkedin_candidates(seen_keys, eval_cache, cache_days, health))
        checkpoint["failed_companies"] = failed_companies
        checkpoint["pending_candidates"] = direct_candidates
        storage.atomic_write_json(CHECKPOINT_FILE, checkpoint)
        export_to_excel(confirmed_new_jobs, failed_companies, OUTPUT_EXCEL)

    print(f"\n[Stage 1 Summary] Successful: {len(companies) - len(failed_companies)} | Un-scraped/LinkedIn: {len(failed_companies)}")
    print(f"[*] Total candidate links collected: {len(direct_candidates)}")

    unique_candidates, skipped = select_new_candidates(direct_candidates, seen_by_key, seen_hashes, eval_cache, cache_days)

    print(f"[*] Brand-new unseen candidates to process: {len(unique_candidates)} | skipped: {skipped}")
    stage_counts = {"prefilter_rejected": 0, "no_description": 0, "ai_rejected": 0, "ai_unconfirmed": 0}

    # --- STAGE 2: Parallel Page Details Fetching + Playwright + Deterministic Guards ---
    if unique_candidates:
        print(f"\n=== STAGE 2: Deep Fetching Job Details ({len(unique_candidates)} jobs) ===")
        with ThreadPoolExecutor(max_workers=8) as executor:
            fetched_candidates = list(executor.map(fetch_job_details, unique_candidates))

        fetched_candidates = fetch_missing_details_with_playwright(fetched_candidates)

        max_allowed_yoe = int(USER_CONFIG.get("max_yoe", 2))
        ai_ready_candidates = []

        for job in fetched_candidates:
            key = job["job_key"]
            txt = job.get("requirements_text", "").strip()
            if len(txt) < 150:
                stage_counts["no_description"] += 1  # transient -> not cached
                print(f"  [X] SKIPPED (No readable description): {job['company']} - {job['title']}")
                continue
            nj = ji.classify_non_job(job["title"], job["url"])
            if nj:
                remember_rejection(eval_cache, key, job, "prefilter", nj)
                stage_counts["prefilter_rejected"] += 1
                continue
            if ji.location_is_foreign(job["title"], job["url"], job.get("page_location", "")):
                remember_rejection(eval_cache, key, job, "prefilter", "non_israel_location")
                stage_counts["prefilter_rejected"] += 1
                print(f"  [X] REJECTED (Outside Israel: {job.get('page_location')}): {job['company']} - {job['title']}")
                continue
            excessive = has_excessive_min_yoe(txt, max_allowed_yoe)
            if excessive:
                remember_rejection(eval_cache, key, job, "prefilter", f"min_yoe: {excessive[1]}")
                stage_counts["prefilter_rejected"] += 1
                print(f"  [X] REJECTED (Min YOE > {max_allowed_yoe}: '{excessive[1]}'): {job['company']} - {job['title']}")
                continue
            if location_verdict(job.get("registry_name", job["company"]), job.get("page_location", "")) == "reject":
                remember_rejection(eval_cache, key, job, "prefilter", f"region: {job.get('page_location')}")
                stage_counts["prefilter_rejected"] += 1
                print(f"  [X] REJECTED (Outside allowed region: {job.get('page_location')}): {job['company']} - {job['title']}")
                continue
            ai_ready_candidates.append(job)

        print(f"[*] Candidates passing description, YOE & location pre-checks for AI review: {len(ai_ready_candidates)}")

        # --- STAGE 3: High-Precision Small-Batch AI Verification ---
        if ai_ready_candidates:
            print(f"\n=== STAGE 3: Precision Batch AI Verification ({len(ai_ready_candidates)} jobs) ===")
            # Each batch is one Gemini request. Larger batches = far fewer requests (the first run took ~2h at 4),
            # at the cost of a longer prompt per request. Configurable: "ai_batch_size" in config.json.
            batch_size = max(1, min(12, int(RAW_CONFIG.get("ai_batch_size", 8))))
            batches = [ai_ready_candidates[i:i + batch_size] for i in range(0, len(ai_ready_candidates), batch_size)]
            print(f"[*] Grouped into {len(batches)} batches of up to {batch_size} jobs each.\n")

            for b_idx, batch in enumerate(batches, 1):
                print(f"[*] Sending Batch {b_idx}/{len(batches)} ({len(batch)} jobs) to Gemini...")
                evaluations = batch_verify_jobs_with_ai(batch)
                eval_map = {ev.get("job_index"): ev for ev in evaluations}

                for idx, job in enumerate(batch, 1):
                    ev = eval_map.get(idx)
                    key = job["job_key"]
                    if ev and ev.get("approved") and ev.get("experience_level") != "senior":
                        entry = build_entry(job, ev)
                        # The AI can approve a posting whose own detected city is outside allowed_cities
                        # (it is often discovered only in the full description). Re-check deterministically.
                        if location_verdict(job.get("registry_name", job["company"]), entry["location"]) == "reject":
                            stage_counts["ai_rejected"] += 1
                            reason = f"location outside allowed cities: {entry['location']}"
                            remember_rejection(eval_cache, key, job, "post_ai_location", reason)
                            print(f"  [X] REJECTED: {job['company']} - {job['title']} (Reason: {reason})")
                            continue
                        confirmed_new_jobs.append(entry)
                        seen_jobs.append(entry)
                        seen_keys.add(key)
                        seen_by_key[key] = entry
                        seen_hashes.add(compute_job_hash(entry["company"], entry["title"], entry["url"]))
                        label = ji.EXPERIENCE_LABELS_HE.get(entry["experience_level"], "")
                        print(f"  [✓] APPROVED [{entry['role_category']} | {label} | score {entry['match_score']}]: "
                              f"{job['company']} - {job['title']} [{entry['location']}]")
                    elif ev:
                        stage_counts["ai_rejected"] += 1
                        remember_rejection(eval_cache, key, job, "ai", ev.get("rejection_reason") or "not approved")
                        print(f"  [X] REJECTED: {job['company']} - {job['title']} (Reason: {ev.get('rejection_reason')})")
                    else:
                        stage_counts["ai_unconfirmed"] += 1  # AI/network failure -> retry next run (not cached)
                        print(f"  [?] UNCONFIRMED (AI error, will retry next run): {job['company']} - {job['title']}")

                checkpoint["confirmed_jobs"] = confirmed_new_jobs
                checkpoint["pending_candidates"] = []
                storage.atomic_write_json(CHECKPOINT_FILE, checkpoint)
                storage.atomic_write_json(SEEN_JOBS_FILE, seen_jobs)
                export_to_excel(confirmed_new_jobs, failed_companies, OUTPUT_EXCEL)

    # --- STAGE 4: Finalization ---
    if len(seen_jobs) < seen_count_before:
        raise RuntimeError("Refusing to save: seen_jobs would shrink (history must never be truncated).")
    storage.atomic_write_json(SEEN_JOBS_FILE, seen_jobs)
    prune_eval_cache(eval_cache, cache_days)
    storage.atomic_write_json(EVAL_CACHE_FILE, eval_cache)

    failing = sorted((n, s["consecutive_failures"], s.get("last_reason", "")) for n, s in health["sources"].items()
                     if s.get("consecutive_failures", 0) >= HEALTH_FAILURE_ALERT)
    run_summary = {
        "started_at": run_started, "finished_at": storage.now_iso(), "profile": PROFILE_NAME,
        "sources": len(companies), "sources_failed": len(failed_companies), "watchlist": wl_stats,
        "candidates_collected": len(direct_candidates), "skipped": skipped, "new_candidates": len(unique_candidates),
        "stages": stage_counts, "new_jobs": len(confirmed_new_jobs),
        "seen_jobs_before": seen_count_before, "seen_jobs_after": len(seen_jobs),
        "sources_failing_3plus_runs": len(failing),
    }
    health["runs"] = (health["runs"] + [run_summary])[-60:]
    health["last_run"] = run_summary
    health["alerts"] = [{"source": n, "consecutive_failures": k, "last_reason": r} for n, k, r in failing]
    storage.atomic_write_json(HEALTH_FILE, health)
    export_to_excel(confirmed_new_jobs, failed_companies, OUTPUT_EXCEL)

    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)

    print(f"\n" + "=" * 60)
    print(f"[✓] CRAWL COMPLETED SUCCESSFULLY!")
    print(f"[*] Newly Discovered Jobs: {len(confirmed_new_jobs)} | seen_jobs: {seen_count_before} -> {len(seen_jobs)}")
    if failing:
        print(f"[!] {len(failing)} sources failed {HEALTH_FAILURE_ALERT}+ runs in a row (see data/crawl_health.json alerts).")
    print(f"[*] Results saved to: {OUTPUT_EXCEL}")
    print("=" * 60)


def run_check():
    """`python job_crawler.py --check`: validate config + Gemini key + data files without crawling."""
    import watchlist
    print(f"[*] config.json OK | active profile: {PROFILE_NAME}")
    registry = load_json(MASTER_FILE, [])
    _, wl_sources, wl_stats = watchlist.build_watchlist_sources(registry, GLOBAL_CONFIG, force_all=True,
                                                                exclude_fn=is_company_excluded)
    seen = load_json(SEEN_JOBS_FILE, [])
    print(f"[*] registry sources: {len(registry)} | watchlist: {wl_stats}")
    print(f"[*] seen_jobs.json: {len(seen)} records "
          f"({sum(1 for r in seen if r.get('status', 'active') == 'active')} active)")
    print(f"[*] LinkedIn: {'enabled' if GLOBAL_CONFIG.get('linkedin', {}).get('enabled') else 'disabled'} "
          f"({len(USER_CONFIG.get('linkedin_queries', []))} queries)")
    test_gemini_preflight()
    print("[✓] Everything is ready. Run `python job_crawler.py` to crawl.")


if __name__ == "__main__":
    if "--check" in sys.argv:
        run_check()
        sys.exit(0)
    try:
        main()
    except Exception as exc:
        print(f"\n[!] Unexpected exit: {exc}")
        print("Progress checkpoint and Excel are preserved.")
        sys.exit(1)
