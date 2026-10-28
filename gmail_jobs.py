#!/usr/bin/env python3
"""Scan Gmail (read-only) for job-application emails and build a tracker.

Finds application confirmations, assessments, interview invites, rejections and
offers, groups them by company, and writes:

  output/applications.csv   one row per company, latest status first
  output/emails.csv         every matched email (for auditing the parser)
  output/report.html        filterable table, open in any browser

Usage:
  python gmail_jobs.py                  # last 180 days
  python gmail_jobs.py --since 2026/06/01
  python gmail_jobs.py --refresh        # ignore cache, re-download everything
  python gmail_jobs.py --headless       # never open a browser; exit 3 if sign-in expired
"""
from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import html
import json
import os
import re
import sys
import time
import warnings
from collections import defaultdict
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CREDENTIALS = HERE / "credentials.json"   # OAuth client you download from Google Cloud
TOKEN = HERE / "token.json"               # created on first sign-in
CACHE = HERE / "cache" / "messages.json"
OUT = HERE / "output"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CACHE_VERSION = 2   # cache holds slimmed emails; parser changes don't need a re-download

# Applicant-tracking systems and job boards that send application mail.
ATS_DOMAINS = [
    "greenhouse.io", "greenhouse-mail.io", "lever.co", "myworkday.com", "workday.com",
    "smartrecruiters.com", "icims.com", "jobvite.com", "ashbyhq.com", "successfactors.com",
    "successfactors.eu", "taleo.net", "bamboohr.com", "workablemail.com", "workable.com",
    "recruitee.com", "dayforcehcm.com", "ceridian.com", "ultipro.com", "ukg.com",
    "oraclecloud.com", "avature.net", "phenompeople.com", "jazzhr.com", "breezy.hr",
    "pinpointhq.com", "teamtailor.com", "rippling.com", "paylocity.com", "adp.com",
    "hackerrank.com", "hackerrankforwork.com", "codility.com", "codesignal.com",
    "hirevue.com", "pymetrics.com", "joinhandshake.com", "talentegy.com", "eightfold.ai",
    "njoyn.com", "cegid.cloud", "shl.com", "sparkhire.com",
    "jobs-noreply@linkedin.com", "indeedapply@indeed.com",
]
# Senders whose display name / domain says nothing about the employer.
GENERIC_NAMES = {
    "workday", "greenhouse", "lever", "icims", "jobvite", "smartrecruiters", "ashby",
    "taleo", "successfactors", "bamboohr", "workable", "linkedin", "indeed", "handshake",
    "hackerrank", "codility", "codesignal", "hirevue", "no reply", "noreply", "no-reply",
    "donotreply", "do not reply", "notifications", "careers", "recruiting", "talent",
    "hiring team", "talent acquisition", "hr", "jobs", "recruitment", "oracle", "ukg",
    "student", "students", "spark hire", "myview", "system",
}
GENERIC_DOMAINS = {"gmail", "outlook", "hotmail", "yahoo", "icloud", "mail", "email", "e"}

# Merge spellings of the same employer ("JPMC", "J.P. Morgan" -> "JPMorgan Chase"). Keys are the
# normalize_company() form: lowercase, no punctuation, suffixes like Inc/Ltd/Careers removed.
# Put your own in aliases.json (see aliases.example.json).
ALIASES_FILE = HERE / "aliases.json"


def load_aliases() -> dict:
    try:
        return {k.lower(): v for k, v in json.loads(ALIASES_FILE.read_text()).items()}
    except FileNotFoundError:
        return {}
    except (ValueError, AttributeError) as e:
        sys.exit(f"Could not read {ALIASES_FILE.name}: {e}")


ALIASES = load_aliases()

# Job-alert / marketing noise that matches the search but isn't an application.
NOISE_SUBJECT = re.compile(
    r"job alert|jobs? (?:for you|you may|matching)|new jobs|recommended jobs|is hiring|"
    r"jobs? near|apply now|top jobs|people are applying|webinar|newsletter|daily digest|"
    r"complete your profile|finish (?:applying|your application)|verify your email|"
    r"mock interview|reset your password|password reset|sign.?in code|don.t forget to complete",
    re.I,
)
NOISE_SENDERS = re.compile(
    r"jobalerts-noreply@linkedin|jobs-listings@linkedin|alert@indeed|"
    r"noreply@glassdoor|ziprecruiter|messages-noreply@linkedin|hackerrankmail\.com",
    re.I,
)

# Body patterns, ordered most-advanced first; a message gets the first stage that matches.
STAGES = [
    ("Offer", [
        r"pleased to (?:extend|offer)", r"extend (?:you )?an offer", r"offer letter",
        r"offer of employment", r"congratulations[^.]{0,60}(?:offer|selected)",
    ]),
    ("Rejected", [
        r"unfortunately,?\s+(?:we|after|upon|at this time|the (?:position|role)|your (?:application|profile))",
        r"unable to (?:move|proceed) forward", r"(?:position|role) you applied (?:for|to) has (?:now )?been closed",
        r"regret to inform", r"not (?:be )?moving forward with (?:your|you)",
        r"(?:move|moving|proceed|proceeding) (?:forward )?with other candidates",
        r"decided to (?:pursue|proceed with|move forward with) other",
        r"will not be (?:proceeding|progressing)", r"not been selected",
        r"no longer (?:under consideration|being considered)",
        r"(?:position|role) has (?:been filled|been closed)", r"(?:have|has) filled the (?:position|role)",
        r"not to proceed with your application", r"we have decided not to",
        r"(?:decided|chosen) not to (?:move forward|proceed|progress|advance)",
    ]),
    ("Interview", [
        r"invite you (?:to|for) (?:an? )?(?:interview|conversation|call|chat|phone screen)",
        r"(?:schedule|set up|book|arrange) (?:an? |your )?(?:interview|phone screen|call with)",
        r"interview (?:invitation|invite|request|confirmation|details|scheduled)",
        r"like to (?:speak|chat|talk|meet) with you", r"next round", r"calendly\.com",
    ]),
    ("Assessment", [
        r"hackerrank", r"codility", r"codesignal", r"hirevue", r"pymetrics",
        r"(?:online|technical|coding|skills?) (?:assessment|test|challenge)",
        r"complete (?:the|an|this|your) assessment", r"take-home",
    ]),
    ("Applied", [
        r"thank(?:s| you)(?: so much| very much)? for (?:your )?(?:recent )?appl", r"application (?:has been |was )?(?:received|submitted)",
        r"(?:received|receiving) your (?:job )?application", r"your application (?:to|for|with|was sent)",
        r"application confirmation", r"successfully (?:applied|submitted|received)",
        r"thank(?:s| you) for (?:your )?interest in", r"we(?:'ve|’ve| have) got your application",
        r"confirm(?:ing)? (?:receipt|that we received)", r"thank(?:s| you) for (?:considering|submitting|completing)",
        r"merci (?:pour|de) votre candidature",
    ]),
]
STAGE_RE = dict((name, re.compile("|".join(pats), re.I)) for name, pats in STAGES)
STAGE_RANK = {name: i for i, (name, _) in enumerate(reversed(STAGES))}  # Applied=0 ... Offer=4

# Subjects are short and deliberate, so they are trusted over body text.
SUBJECT_STAGES = [
    ("Offer", r"offer (?:letter|of employment)|\byour offer\b"),
    ("Rejected", r"\bregret\b|unsuccessful|not selected|(?:position|role) (?:has been )?filled"),
    ("Interview", r"interview (?:request|invitation|invite|confirmation|schedul)|video interview|"
                  r"(?:schedule|book)\b.{0,30}\binterview|phone screen"),
    ("Assessment", r"assessment|\btest\b|hackerrank|codility|codesignal|hirevue|coding challenge"),
    ("Applied", r"thank(?:s| you)(?: so much| very much)? for (?:your )?(?:recent )?(?:applying|application|submitting|completing)|"
                r"application (?:received|submitted|confirmation|acknowledg|success)|received your (?:job )?application|"
                r"successfully (?:applied|submitted)|got your .{0,40}application|application was sent to|"
                r"started your job application|merci (?:pour|de) votre candidature|job application received"),
]
SUBJECT_RE = [(name, re.compile(p, re.I)) for name, p in SUBJECT_STAGES]

# Sentences that only describe what *might* happen ("if you are invited to an interview").
CONDITIONAL = re.compile(
    r"\bif\b|should you|ahead of|in the event|\btips?\b|\bmay\b|\bmight\b|upcoming|future|"
    r"\bonce\b|whether|selected for|move forward in the process|what.s next|our process",
    re.I,
)
BODY_WINDOW = 1500   # stage language that matters is near the top; legends/FAQs come later

# "...at Acme Robotics", "applying to Globex", "your application was sent to Initech"
_TOK = r"[A-Z0-9][\w&'’\-]*(?:\.[A-Za-z0-9]+)*"   # allows "Super.com", not a sentence-ending period
_CO = r"(?P<c>" + _TOK + r"(?:\s+(?:&\s+|of\s+)?" + _TOK + r"){0,5})"
COMPANY_PATTERNS = [
    re.compile(r"application was sent to\s+" + _CO),
    re.compile(r"(?:submitted|sent) to\s+" + _CO),
    re.compile(r"(?:applying|applied|application|candidacy)\s+(?:to|with|at)\s+" + _CO),
    re.compile(r"(?:position|role|opportunity|opening|job|internship|co-?op)\s+(?:at|with)\s+" + _CO),
    re.compile(r"(?:interest in|considering)\s+(?:a career (?:at|with)\s+)?" + _CO),
    re.compile(r"\bat\s+" + _CO + r"(?=\s*[!.,|:\-–—(]|\s*$)"),
    re.compile(r"(?:assessment|test|challenge|invitation|invited)\s+(?:for|from|by)\s+" + _CO),
    re.compile(r"(?:from|join) (?:the )?" + _CO + r"\s+(?:team|talent|recruiting)"),
]
COMPANY_STOP = re.compile(
    r"^(?:the|our|a|an|this|that|your|you|us|we|it|position|role|job|team|company|"
    r"opportunity|application|interest|careers?|linkedin|indeed|workday|greenhouse|lever|"
    r"hackerrank|time|least|all|any|each|present|this time|our company|joining|working)\b",
    re.I,
)

_ROLE = r"(?P<r>[A-Z0-9][\w/&,+()'’.:\-–— ]{2,100}?)"
ROLE_PATTERNS = [   # most reliable first
    re.compile(r"(?:for|to|in) the " + _ROLE + r"\s+(?:position|role|opportunity|opening|posting|job)\b"),
    re.compile(r"(?:position|role) of\s+" + _ROLE + r"(?=\s+(?:at|with)\s|[.!\n]|$)"),
    re.compile(r"(?:application|applying|applied) (?:for|to)(?: the| job:?)?\s+" + _ROLE
               + r"(?=\s+(?:at|with|position|role)\s|[.!\n|]|$)"),
    re.compile(r"(?:job title|position|role)\s*:\s*" + _ROLE + r"(?=\n|\s{2}|$)", re.I),
    re.compile(r"(?:Application|Acknowledgement|Status)\s+(?:for\s+)?[-–:]\s*" + _ROLE + r"(?=\s*$|\s+\|)"),
]
ROLE_BAD = re.compile(
    r"^(?:this|that|your|our|the|a|an|any|other|open|future|similar|next steps?)\b|"
    r"\b(?:we|you|your|thank|taking|thrilled|interested|joining|location|dear|hello|hi)\b",
    re.I,
)
REQ_ID = re.compile(r"\(?\b(?:R|JR|REQ|Req ID|Job ID|J\d{4})[\s#:-]*\d[\w-]*\)?\s*[-–:]?\s*|\s[-–]\s\d{4,6}\b|\(\d{4,6}\)", re.I)


# ----------------------------------------------------------------------------- parsing

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip(" \t\n-–—:|,.!")


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>|</li>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return html.unescape(raw)


def body_text(payload: dict) -> str:
    """Prefer text/plain; fall back to stripped text/html."""
    plain, rich = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data:
            txt = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
            if mime == "text/plain":
                plain.append(txt)
            elif mime == "text/html":
                rich.append(html_to_text(txt))
        for p in part.get("parts", []) or []:
            walk(p)

    walk(payload)
    text = "\n".join(plain) if plain else "\n".join(rich)
    text = re.sub(r"[͏​-‏­﻿]", "", text)    # invisible preheader padding
    return re.sub(r"[ \t\r\f\v\xa0]+", " ", text)


def classify(subject: str, body: str) -> str | None:
    window = body[:BODY_WINDOW]
    factual = " ".join(s for s in re.split(r"(?<=[.!?])\s+|\n+", window) if not CONDITIONAL.search(s))
    subject_stage = next((name for name, rx in SUBJECT_RE if rx.search(subject)), None)
    if subject_stage and subject_stage != "Applied":
        return subject_stage
    # A clear confirmation subject only yields to a real rejection/offer near the top.
    for name in ("Offer", "Rejected"):
        if STAGE_RE[name].search(factual):
            return name
    if subject_stage == "Applied":
        return "Applied"
    for name in ("Interview", "Assessment"):
        if STAGE_RE[name].search(factual):
            return name
    if STAGE_RE["Applied"].search(f"{subject}\n{window}"):
        return "Applied"
    return None


def normalize_company(name: str) -> str:
    n = name.lower().replace("'", "").replace("’", "")
    n = re.sub(r"\b(inc|incorporated|ltd|limited|llc|llp|corp|corporation|co|company|plc|"
               r"canada|group|holdings|careers?|recruiting|recruitment|talent acquisition|hiring team|"
               r"jobs|hr|team|via workday|workday|the|human resources?|family of companies)\b", " ", n)
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


def canonical(name: str) -> str:
    return ALIASES.get(normalize_company(name), name)


def _looks_like_person(name: str, addr: str) -> bool:
    words = name.split()
    if not 2 <= len(words) <= 3 or not all(w.isalpha() and w[0].isupper() for w in words):
        return False
    local, _, domain = addr.partition("@")
    # Recruiters write from the company's own domain, never an ATS autoreply address.
    if "+" in local or any(domain.endswith(d) for d in ATS_DOMAINS if "@" not in d):
        return False
    local = re.sub(r"[^a-z]", "", local)
    return words[-1].lower() in local or words[0].lower() in local


def _company_from_domain(domain: str) -> str | None:
    if any(domain.endswith(d) for d in ATS_DOMAINS if "@" not in d):
        return None
    parts = [p for p in domain.split(".") if p not in {"com", "ca", "org", "net", "io", "co", "ai",
                                                        "us", "uk", "edu", "gov", "hr", "jobs", "careers"}]
    if not parts or parts[-1] in GENERIC_DOMAINS:
        return None
    label = re.sub(r"-?(?:recruitment|recruiting|careers|jobs)$", "", parts[-1])
    return label.upper() if len(label) <= 4 else label.title()


def company_from_sender(from_header: str) -> str | None:
    name, addr = parseaddr(from_header)
    addr = addr.lower()
    if re.fullmatch(r"\S+@\S+\.\w+", name):   # e.g. "jane.doe@acme.com" <helpdesk@njoyn.com>
        name, addr = "", name.lower()
    name = re.sub(r"\s+(?:via|@)\s.*$", "", name, flags=re.I)
    name = re.sub(r"\s*[-–—/]?\s*Carri[eè]res?\b.*$", "", name, flags=re.I)
    name = re.sub(r"workday|notifications?|no.?reply", " ", name, flags=re.I)
    name = re.sub(r"\b(?:Recruiting|Recruitment|Careers?|Talent(?: Acquisition)?(?: Team)?|Hiring Team|"
                  r"Jobs|HR|Human Resources?|People(?: Team| Ops)?|Team|University Recruiting|Campus)\b",
                  "", name, flags=re.I)
    name = _clean(re.sub(r"['’]s\b", "", name))
    name = re.sub(r"^The\s+", "", name)
    local, _, domain = addr.partition("@")
    if name and name.lower() not in GENERIC_NAMES and not _looks_like_person(name, addr):
        return name
    # Workday sends from <tenant>@myworkday.com; the tenant is usually the company.
    if domain.endswith("myworkday.com") and local not in {"noreply", "no-reply", "donotreply"}:
        tenant = local.replace("_", " ")
        return tenant.upper() if len(tenant) <= 4 else tenant.title()
    return _company_from_domain(domain)


def company_from_text(subject: str, body: str) -> str | None:
    for source in (subject, body[:2500]):
        for rx in COMPANY_PATTERNS:
            for m in rx.finditer(source):
                c = _clean(m.group("c"))
                c = re.split(r"\s+(?:for|and|has|have|is|was|team|we|on|in|as|to|regarding)\b", c)[0]
                c = _clean(c)
                if 2 <= len(c) <= 50 and not COMPANY_STOP.match(c):
                    return c
    return None


def role_from_text(subject: str, body: str, company: str = "") -> str | None:
    co = normalize_company(company)
    for rx in ROLE_PATTERNS:
        for source in (body[:2500], subject):
            for m in rx.finditer(source):
                r = REQ_ID.sub(" ", m.group("r"))
                r = _clean(re.split(r"\s+(?:at|with)\s+[A-Z]", r)[0])
                if (3 <= len(r) <= 90 and not ROLE_BAD.search(r)
                        and r.count("(") == r.count(")") and not r.isdigit()
                        and not (co and (co in normalize_company(r) or normalize_company(r) in co))):
                    return r
    return None


def slim_message(msg: dict) -> dict:
    """The parts of a Gmail message the parser needs; this is what gets cached."""
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    try:
        date = parsedate_to_datetime(headers["date"]).astimezone()
    except Exception:
        date = dt.datetime.fromtimestamp(int(msg["internalDate"]) / 1000).astimezone()
    return {
        "id": msg["id"],
        "thread_id": msg["threadId"],
        "date": date.isoformat(timespec="minutes"),
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        "snippet": html.unescape(msg.get("snippet", "")),
        "body": body_text(msg["payload"])[:6000],
    }


def parse_message(m: dict) -> dict | None:
    subject, sender, body = m["subject"], m["from"], m["body"]
    if NOISE_SUBJECT.search(subject) or NOISE_SENDERS.search(sender):
        return None
    stage = classify(subject, body)
    if not stage:
        return None
    board = re.search(r"linkedin|indeed", sender, re.I)
    company = ((company_from_text(subject, body) if board else None)
               or company_from_sender(sender) or company_from_text(subject, body) or "(unknown)")
    return {
        "id": m["id"],
        "thread_id": m["thread_id"],
        "date": m["date"],
        "stage": stage,
        "company": canonical(company),
        "role": role_from_text(subject, body, canonical(company)) or "",
        "subject": subject,
        "from": sender,
        "snippet": m["snippet"],
        "link": f"https://mail.google.com/mail/u/0/#all/{m['thread_id']}",
    }


# ----------------------------------------------------------------------------- gmail

AUTH_EXPIRED = 3   # exit code when a sign-in is needed but --headless forbids opening a browser


def gmail_service(headless: bool = False):
    # macOS system Python 3.9 triggers end-of-life / LibreSSL warnings from the Google libs.
    warnings.filterwarnings("ignore", category=FutureWarning)
    warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit("Missing packages. Run:  pip install -r requirements.txt")

    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES) if TOKEN.exists() else None
    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = True
            except RefreshError:
                creds = None              # refresh token revoked or expired (7 days in Testing mode)
        if not refreshed:
            if headless:
                print("GMAIL_AUTH_EXPIRED: Google sign-in has expired. Run gmail_jobs.py once "
                      "in a terminal to sign in again.", file=sys.stderr)
                sys.exit(AUTH_EXPIRED)
            if not CREDENTIALS.exists():
                sys.exit(f"Missing {CREDENTIALS.name}. See README.md, step 1.")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN.write_text(creds.to_json())
        os.chmod(TOKEN, 0o600)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def build_query(since: str) -> str:
    froms = " OR ".join(ATS_DOMAINS)
    subjects = ("applying OR application OR applied OR candidacy OR interview OR assessment "
                'OR "your interest" OR "next steps" OR offer')
    return f"after:{since} -from:me -in:chats -in:spam (subject:({subjects}) OR from:({froms}))"


def list_ids(svc, query: str) -> list[str]:
    ids, token = [], None
    while True:
        resp = execute(svc.users().messages().list(userId="me", q=query, maxResults=500, pageToken=token))
        ids += [m["id"] for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token:
            return ids


def execute(request, attempts: int = 7):
    """Run a Gmail API request, waiting out the per-minute quota (403/429 rateLimitExceeded)."""
    from googleapiclient.errors import HttpError

    for attempt in range(attempts):
        try:
            return request.execute()
        except HttpError as e:
            limited = e.resp.status in (403, 429) and "rate" in str(e).lower()
            if not limited or attempt == attempts - 1:
                raise
            wait = min(15 * 2 ** attempt, 120)
            print(f"\n  Gmail rate limit hit, waiting {wait}s...", flush=True)
            time.sleep(wait)


def load_cache() -> dict:
    try:
        data = json.loads(CACHE.read_text())
        return data["messages"] if data.get("version") == CACHE_VERSION else {}
    except Exception:
        return {}


def save_cache(messages: dict) -> None:
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps({"version": CACHE_VERSION, "messages": messages}))


# ----------------------------------------------------------------------------- report

def summarize(records: list[dict]) -> list[dict]:
    by_co = defaultdict(list)
    names = {}
    for r in records:
        key = normalize_company(r["company"]) or "(unknown)"
        by_co[key].append(r)
        names.setdefault(key, r["company"])
    rows = []
    for key, msgs in by_co.items():
        msgs.sort(key=lambda r: r["date"])
        # Latest email decides the status, but a plain confirmation never undoes a later stage.
        status = max(msgs, key=lambda r: (STAGE_RANK[r["stage"]] > 0, r["date"]))["stage"]
        applied = next((m["date"] for m in msgs if m["stage"] == "Applied"), msgs[0]["date"])
        roles = list(dict.fromkeys(m["role"] for m in msgs if m["role"]))
        rows.append({
            "company": names[key],
            "roles": "; ".join(roles),
            "status": status,
            "applied": applied[:10],
            "last_update": msgs[-1]["date"][:10],
            "emails": len(msgs),
            "last_subject": msgs[-1]["subject"],
            "link": msgs[-1]["link"],
        })
    rows.sort(key=lambda r: r["last_update"], reverse=True)
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def write_html(path: Path, rows: list[dict], since: str) -> None:
    counts = defaultdict(int)
    for r in rows:
        counts[r["status"]] += 1
    order = ["Applied", "Assessment", "Interview", "Offer", "Rejected"]
    chips = "".join(f'<button data-s="{s}">{s} <b>{counts[s]}</b></button>' for s in order if counts[s])
    e = html.escape
    body = "".join(
        f'<tr data-s="{e(r["status"])}"><td>{e(r["company"])}</td><td>{e(r["roles"])}</td>'
        f'<td><span class="st {e(r["status"].lower())}">{e(r["status"])}</span></td>'
        f'<td>{e(r["applied"])}</td><td>{e(r["last_update"])}</td><td>{r["emails"]}</td>'
        f'<td><a href="{e(r["link"])}" target="_blank" rel="noopener">{e(r["last_subject"])}</a></td></tr>'
        for r in rows
    )
    path.write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Job Applications</title>
<style>
:root{{--bg:#fafaf8;--fg:#1d1d1b;--mute:#6b6b66;--line:#e4e3de;--card:#fff;--acc:#2f5bd3}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe6;--mute:#9a9992;--line:#2c2c2a;--card:#1d1d1c;--acc:#8aa8ff}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,system-ui,sans-serif}}
main{{max-width:1200px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px;margin:0 0 4px}} p{{color:var(--mute);margin:0 0 16px}}
.bar{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}}
button,input{{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px}}
button{{cursor:pointer}} button.on{{border-color:var(--acc);color:var(--acc)}} input{{flex:1;min-width:180px}}
.wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--card)}}
table{{border-collapse:collapse;width:100%}} th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{font-size:12px;color:var(--mute);font-weight:600;cursor:pointer;white-space:nowrap}} td:nth-child(4),td:nth-child(5){{white-space:nowrap}}
a{{color:var(--acc);text-decoration:none}} a:hover{{text-decoration:underline}}
.st{{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid currentColor;white-space:nowrap}}
.applied{{color:var(--mute)}} .assessment{{color:#b7791f}} .interview{{color:#2f855a}} .offer{{color:#2f5bd3}} .rejected{{color:#c53030}}
</style></head><body><main>
<h1>Job applications</h1><p>{len(rows)} companies from Gmail since {e(since)}. Generated {dt.date.today()}.</p>
<div class="bar"><button data-s="" class="on">All <b>{len(rows)}</b></button>{chips}<input id="q" placeholder="Search company or role"></div>
<div class="wrap"><table><thead><tr><th>Company</th><th>Role(s)</th><th>Status</th><th>Applied</th><th>Last update</th><th>Emails</th><th>Latest email</th></tr></thead>
<tbody>{body}</tbody></table></div></main>
<script>
let s="";const q=document.getElementById("q"),rows=[...document.querySelectorAll("tbody tr")];
function f(){{const t=q.value.toLowerCase();rows.forEach(r=>r.hidden=(s&&r.dataset.s!==s)||!r.textContent.toLowerCase().includes(t))}}
document.querySelectorAll(".bar button").forEach(b=>b.onclick=()=>{{s=b.dataset.s;document.querySelectorAll(".bar button").forEach(x=>x.classList.toggle("on",x===b));f()}});
q.oninput=f;
document.querySelectorAll("th").forEach((th,i)=>{{let asc=false;th.onclick=()=>{{asc=!asc;const tb=document.querySelector("tbody");
[...tb.rows].sort((a,b)=>a.cells[i].textContent.localeCompare(b.cells[i].textContent,undefined,{{numeric:true}})*(asc?1:-1)).forEach(r=>tb.appendChild(r))}}}});
</script></body></html>""")


# ----------------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default=(dt.date.today() - dt.timedelta(days=180)).strftime("%Y/%m/%d"),
                    help="only emails after this date, YYYY/MM/DD (default: 180 days ago)")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache and re-download")
    ap.add_argument("--headless", action="store_true",
                    help=f"never open a browser to sign in; exit with code {AUTH_EXPIRED} instead (for scheduled runs)")
    args = ap.parse_args()

    svc = gmail_service(headless=args.headless)
    query = build_query(args.since)
    ids = list_ids(svc, query)
    cache = {} if args.refresh else load_cache()
    new = [i for i in ids if i not in cache]
    print(f"{len(ids)} candidate emails since {args.since} ({len(new)} new to fetch)")

    for n, mid in enumerate(new, 1):
        msg = execute(svc.users().messages().get(userId="me", id=mid, format="full"))
        time.sleep(0.4)                          # stay under the per-minute quota
        cache[mid] = slim_message(msg)
        if n % 25 == 0 or n == len(new):
            print(f"  fetched {n}/{len(new)}", end="\r")
            save_cache(cache)
    print()
    save_cache(cache)

    parsed = (parse_message(cache[i]) for i in ids)
    records = sorted((r for r in parsed if r), key=lambda r: r["date"], reverse=True)
    apps = summarize(records)
    OUT.mkdir(exist_ok=True)
    write_csv(OUT / "emails.csv", records)
    write_csv(OUT / "applications.csv", apps)
    write_html(OUT / "report.html", apps, args.since)

    counts = defaultdict(int)
    for a in apps:
        counts[a["status"]] += 1
    print(f"{len(records)} job emails -> {len(apps)} companies: "
          + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    print(f"Open {OUT / 'report.html'}")


if __name__ == "__main__":
    main()
