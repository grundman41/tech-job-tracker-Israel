import json
import os

import pytest

import job_crawler as jc
import job_identity as ji
from conftest import ROOT


def _profile(name):
    return jc.resolve_profile(jc.RAW_CONFIG, name)[1]


def test_config_is_valid_and_saas_ops_is_active():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    assert cfg["active_profile"] == "iem_saas_ops"
    assert set(cfg["profiles"]) >= {"iem_saas_ops", "iem_product_operations", "software_engineering"}


def test_software_profile_is_preserved_exactly():
    swe = _profile("software_engineering")
    assert swe["target_roles"] == ["Software Development", "Backend", "Frontend", "Fullstack", "Mobile", "AI/ML",
                                   "Data Science", "Algorithms", "MLOps", "Software Automation"]
    assert swe["excluded_roles"] == ["Manual QA", "DevOps", "Customer Success", "Operations", "Technical Support",
                                     "Sales", "Marketing", "HR", "Finance"]
    f = jc.ProfileTitleFilter(swe)
    assert f.matches("Junior Backend Developer")
    assert not f.matches("Operations Analyst")
    assert not f.matches("Customer Success Manager")


@pytest.mark.parametrize("title,expected", [
    ("Operations Analyst", True),
    ("Sales Operations Analyst", True),
    ("Customer Success Operations Specialist", True),
    ("Junior Product Operations – Part-Time Student", True),
    ("AI Product Analyst student for AI Solutions Group", True),
    ("Strategic Purchasing Associate", True),
    ("מהנדס/ת תעשייה וניהול", True),
    ("Senior Revenue Operations Manager", False),
    ("Backend Developer", False),
    ("Technical Support Engineer", False),
])
def test_iem_profile_title_filter(title, expected):
    assert jc.ProfileTitleFilter(_profile("iem_product_operations")).matches(title) is expected


@pytest.mark.parametrize("company,loc,verdict", [
    ("Experis Israel", "Netanya, Center District, Israel", "reject"),   # 'Center District' must not whitelist Netanya
    ("CloudZone", "Tel Aviv District, Israel", "unknown"),             # a region, not the city: decided by the AI
    ("CloudZone", "Tel Aviv-Yafo, Tel Aviv District, Israel", "ok"),
    ("Some Startup", "Holon, Tel Aviv District, Israel", "reject"),     # the district must not whitelist Holon
    ("Some Startup", "Ramat Gan, Israel", "ok"),
    ("Some Startup", "בני ברק", "ok"),
    ("Some Startup", "Givatayim", "ok"),
    ("Some Startup", "Herzliya Pituach, Israel", "ok"),
    ("Some Startup", "Petah Tikva, Center District, Israel", "reject"),
    ("Some Startup", "Ramat HaSharon", "reject"),
    ("Some Startup", "Jerusalem, Israel", "reject"),
    ("Some Startup", "Center District, Israel", "unknown"),            # decided by the AI with the job text
    ("Nvidia", "Yokneam, Northern District, Israel", "reject"),        # no location-exempt companies in this profile
    ("Some Startup", "London, United Kingdom", "reject"),
])
def test_active_profile_location_policy(company, loc, verdict):
    assert jc.location_verdict(company, loc) == verdict


def _cand(company, title, url, loc="Tel Aviv"):
    return {"company": company, "title": title, "url": url, "page_location": loc}


def test_second_identical_scan_adds_nothing(tmp_path):
    seen_by_key, seen_hashes, cache = {}, set(), {"entries": {}}
    scan = [
        _cand("Moveo group", "Junior Full-Stack AI Engineer", "https://forms.monday.com/forms/887015da?r=use1"),
        _cand("Moveo group", "Mobile Developer", "https://forms.monday.com/forms/887015da?r=use1"),
        _cand("Next Insurance", "Frontend Engineer", "https://job-boards.greenhouse.io/nextinsurance66/jobs/7805602003"),
        _cand("Next Insurance", "Frontend EngineerKfar Saba, Israel", "https://job-boards.greenhouse.io/nextinsurance66/jobs/7805602003"),
    ]
    new1, skipped1 = jc.select_new_candidates([dict(c) for c in scan], seen_by_key, seen_hashes, cache, 14)
    assert len(new1) == 3 and skipped1["duplicate_in_run"] == 1
    for c in new1:  # simulate approval
        seen_by_key[c["job_key"]] = {"last_seen_at": "2026-01-01"}
    new2, skipped2 = jc.select_new_candidates([dict(c) for c in scan], seen_by_key, seen_hashes, cache, 14)
    assert new2 == [] and skipped2["already_seen"] == 4
    assert all(r["last_seen_at"] > "2026-01-01" for r in seen_by_key.values())


def test_legacy_records_block_rediscovery_of_same_posting(legacy_records):
    seen_by_key = {jc.record_key(r): r for r in legacy_records}
    hashes = {jc.compute_job_hash(r["company"], r["title"], r["url"]) for r in legacy_records}
    again = [_cand("Next Insurance", "Frontend Engineer", "https://job-boards.greenhouse.io/nextinsurance66/jobs/7805602003", "Kfar Saba")]
    new, skipped = jc.select_new_candidates(again, seen_by_key, hashes, {"entries": {}}, 14)
    assert new == [] and skipped["already_seen"] == 1


def test_recently_rejected_jobs_are_not_resent_to_ai():
    cache = {"entries": {}}
    c = _cand("Wix", "Business Analyst", "https://jobs.smartrecruiters.com/Wix2/744000123456")
    jc.remember_rejection(cache, jc.candidate_key(c), c, "ai", "requires 4 years")
    new, skipped = jc.select_new_candidates([dict(c)], {}, set(), cache, 14)
    assert new == [] and skipped["recently_rejected"] == 1


def test_build_entry_keeps_legacy_contract_and_labels_unspecified():
    job = _cand("Finout", "Junior Product Operations – Part-Time Student",
                "https://jobs.ashbyhq.com/finout/41bf1175-c1b9-4a22-8240-82cebddc59d1")
    job.update({"requirements_text": "x" * 200, "source_type": "ats_api", "priority": "P1"})
    e = jc.build_entry(job, {"approved": True, "role_category": "product", "experience_level": "explicit_student",
                             "experience_evidence": "Part-Time Student", "role_match": 0.9, "skills_fit": 0.8,
                             "detected_city": "Tel Aviv"})
    for f in ("company", "title", "location", "url", "experience", "discovered_at", "referral_contact", "referral_type"):
        assert f in e
    assert e["status"] == "active" and e["profiles"] == ["iem_saas_ops"]
    assert e["job_key"].startswith("ats:ashby:")
    e2 = jc.build_entry(dict(job, title="Business Analyst"), {"approved": True})
    assert e2["experience_level"] == "unspecified"
    assert e2["experience"] == ji.EXPERIENCE_LABELS_HE["unspecified"] == "לא צוין"


@pytest.mark.parametrize("company,excluded", [
    ("Bank Leumi", True), ("Bank Hapoalim", True), ("Israel Discount Bank", True), ("One Zero Bank", True),
    ("Mizrahi Tefahot", True), ("בנק לאומי", True),
    # defense companies are NOT excluded in the active profile (only banks)
    ("Elbit Systems", False), ("Rafael Advanced Defense Systems", False),
    # must NOT be excluded (substring traps)
    ("Unit", False), ("Isracard", False), ("Bankai Labs", False), ("Raday", False), ("Cymotive Technologies", False),
    ("Wix", False), ("Payoneer", False), ("Melio", False), ("Leumit Health Services", False),
])
def test_excluded_companies(company, excluded):
    assert jc.is_company_excluded(company) is excluded


def test_excluded_company_candidates_never_reach_ai():
    cands = [_cand("Bank Leumi", "Project Coordinator", "https://www.linkedin.com/jobs/view/4400000001"),
             _cand("Bank Hapoalim", "Business Analyst", "https://www.linkedin.com/jobs/view/4400000002"),
             _cand("Wix", "Business Analyst", "https://www.linkedin.com/jobs/view/4400000003")]
    new, skipped = jc.select_new_candidates(cands, {}, set(), {"entries": {}}, 14)
    assert [c["company"] for c in new] == ["Wix"] and skipped["excluded_company"] == 2


@pytest.mark.parametrize("title", ["רכז/ת פרויקטים לבנק מוביל", "PMO לחברה ביטחונית", "Project Coordinator - Security Clearance required",
                                   "Defense Program Analyst"])
def test_bank_and_defense_job_board_titles_are_rejected(title):
    assert not jc.ProfileTitleFilter(_profile("iem_product_operations")).matches(title)


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


def test_gemini_preflight_falls_back_when_model_is_retired(monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, headers))
        if "gemini-3.5-flash-lite" in url:
            return _Resp(404, {"error": {"message": "model not found"}})
        return _Resp(200, {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]})

    monkeypatch.setattr(jc.requests, "post", fake_post)
    monkeypatch.setattr(jc, "GEMINI_API_KEY", "secret-test-key")
    monkeypatch.setattr(jc, "MODEL_NAME", "gemini-3.5-flash-lite")
    assert jc.test_gemini_preflight() is True
    assert jc.MODEL_NAME == "gemini-3.1-flash-lite"
    assert all("secret-test-key" not in url for url, _ in calls), "API key must not appear in the URL"
    assert all(h["x-goog-api-key"] == "secret-test-key" for _, h in calls)


def test_gemini_preflight_stops_on_bad_key(monkeypatch):
    monkeypatch.setattr(jc.requests, "post", lambda *a, **k: _Resp(403, {"error": {"message": "API key not valid"}}))
    monkeypatch.setattr(jc, "GEMINI_API_KEY", "bad")
    with pytest.raises(SystemExit):
        jc.test_gemini_preflight()


# --- iem_saas_ops: wide IEM + SaaS-operations search, up to 4 years ---

@pytest.mark.parametrize("title,expected", [
    ("Operations Analyst", True),
    ("Senior Operations Analyst", True),          # 'Senior' alone is allowed (2-4 YOE); the AI judges the stated years
    ("Business Operations Manager", True),
    ("Customer Success Manager", True),
    ("Data Analyst", True),
    ("FP&A Analyst", True),
    ("Support Operations Specialist", True),
    ("מנהל/ת תפעול", True),
    ("Head of Operations", False),
    ("Director of Business Operations", False),
    ("Operations Team Lead", False),
    ("SDR", False),
    ("Product Marketing Manager", False),
    ("Technical Support Engineer", False),
    ("Backend Developer", False),
    ("רכז/ת פרויקטים לבנק מוביל", False),
])
def test_saas_ops_title_filter(title, expected):
    assert jc.ProfileTitleFilter(_profile("iem_saas_ops")).matches(title) is expected


@pytest.mark.parametrize("text,level", [
    ("Requirements: 3-4 years of experience in operations", "stretch_2_plus"),
    ("At least 4 years of experience", "stretch_2_plus"),
    ("5+ years of experience", "senior"),
    ("1-2 years of experience", "explicit_0_2"),
])
def test_experience_range_follows_max_yoe(text, level):
    assert ji.parse_experience_requirement("Operations Analyst", text, max_yoe=4)[0] == level
    # the default (0-2) behaviour is unchanged
    if level == "stretch_2_plus" and "4 years" in text:
        assert ji.parse_experience_requirement("Operations Analyst", text)[0] == "senior"


def test_senior_title_is_not_senior_when_range_reaches_4_years():
    assert ji.parse_experience_requirement("Senior Operations Analyst", "", max_yoe=4)[0] == "unspecified"
    assert ji.parse_experience_requirement("Senior Operations Analyst", "")[0] == "senior"
    assert ji.parse_experience_requirement("Head of Operations", "", max_yoe=4)[0] == "senior"


def test_profile_eligibility_points_rank_2_plus_above_junior():
    pts = _profile("iem_saas_ops")["eligibility_points"]
    hi = ji.match_score(1.0, "stretch_2_plus", "tel aviv", None, 0.8, 1.0, None, {"tel aviv"}, eligibility_points=pts)
    lo = ji.match_score(1.0, "explicit_student", "tel aviv", None, 0.8, 1.0, None, {"tel aviv"}, eligibility_points=pts)
    assert hi > lo


@pytest.mark.parametrize("title,expected", [
    ("Business Operations Analyst", True),
    ("Customer Operations Specialist", True),
    ("Solutions Consultant", True),
    ("Implementation Manager", True),
    ("Data Analyst", True),
    ("FP&A Analyst", True),
    # families the user asked to drop
    ("Revenue Operations Manager", False),
    ("Sales Operations Analyst", False),
    ("Marketing Operations Manager", False),
    ("Deal Desk Analyst", False),
    ("Billing Operations Specialist", False),
    ("Fraud Operations Analyst", False),
    ("Workforce Management Analyst", False),
    ("Chief of Staff", False),
    ("Salesforce Administrator", False),
    ("Automation Specialist", False),
    ("Process Improvement Engineer", False),
    ("Industrial Engineer", False),
    ("Supply Chain Analyst", False),
    ("Procurement Specialist", False),
    ("Partnerships Manager", False),
    ("Management Consultant", False),
    ("רכז/ת רכש", False),
])
def test_saas_ops_dropped_role_families(title, expected):
    assert jc.ProfileTitleFilter(_profile("iem_saas_ops")).matches(title) is expected


def test_data_and_finance_rank_below_core_roles():
    job = _cand("Wix", "Data Analyst", "https://jobs.smartrecruiters.com/Wix2/744000999")
    job.update({"requirements_text": "x" * 200})
    ev = {"approved": True, "experience_level": "stretch_2_plus", "role_match": 0.9, "skills_fit": 0.8,
          "detected_city": "Tel Aviv"}
    data = jc.build_entry(job, dict(ev, role_category="data_analytics"))
    ops = jc.build_entry(dict(job, title="Operations Analyst"), dict(ev, role_category="operations"))
    assert data["match_score"] < ops["match_score"]


@pytest.mark.parametrize("title", ["Operations Student", "Student Position - Business Analyst", "Product Management Intern",
                                   "Operations Intern", "משרת סטודנט/ית לתפעול", "מתמחה בתפעול"])
def test_student_and_intern_roles_are_rejected(title):
    assert not jc.ProfileTitleFilter(_profile("iem_saas_ops")).matches(title)


@pytest.mark.parametrize("url", [
    "https://www.alljobs.co.il/Employer/HP/Default.aspx?cid=367437",
    "https://www.drushim.co.il/companies/1234/",
])
def test_employer_landing_pages_are_not_jobs(url):
    assert ji.classify_non_job("Technical Product Manager", url) == "company_profile_page"
    assert not jc.looks_like_job_url(url) if hasattr(jc, "looks_like_job_url") else True
