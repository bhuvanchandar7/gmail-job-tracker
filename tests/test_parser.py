"""Offline tests for the email parser. No Gmail account needed.

Run:  python tests/test_parser.py      (or: python -m pytest tests)
All emails below are made up, written in the styles real ATS systems use.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gmail_jobs as g  # noqa: E402

g.ALIASES.clear()   # test the parser itself, not your personal aliases.json


def email(subject, sender, body, date="2026-10-06T10:00-06:00"):
    return {"id": subject[:20], "thread_id": "t", "date": date, "subject": subject,
            "from": sender, "snippet": body[:80], "body": body}


def parse(subject, sender, body):
    return g.parse_message(email(subject, sender, body))


def test_lever_confirmation():
    r = parse("Thank you for applying to Acme Robotics", "Acme Robotics Hiring Team <no-reply@hire.lever.co>",
              "Hi Sam, thanks for applying for the Data Science Co-op position. We will review your application.")
    assert (r["stage"], r["company"]) == ("Applied", "Acme Robotics")


def test_linkedin_easy_apply():
    r = parse("Sam, your application was sent to Globex", "LinkedIn <jobs-noreply@linkedin.com>",
              "Your application was sent to Globex. Data Analyst Intern Globex Toronto, ON")
    assert (r["stage"], r["company"]) == ("Applied", "Globex")


def test_workday_tenant_becomes_company():
    r = parse("Application Received", "gnb@myworkday.com",
              "We have received your application for Machine Learning Developer Co-op.")
    assert (r["stage"], r["company"]) == ("Applied", "GNB")


def test_rejection():
    r = parse("Update on your application", "Initech Recruiting <no-reply@us.greenhouse-mail.io>",
              "Thank you for your interest in the Data Analyst role. Unfortunately, we have decided "
              "to move forward with other candidates.")
    assert (r["stage"], r["company"]) == ("Rejected", "Initech")


def test_position_filled_is_rejection():
    r = parse("Thanks for your interest in Umbrella", "UMBRELLA <no-reply@hire.lever.co>",
              "Thank you for applying to the Analyst Co-op role. We wanted to let you know that we have "
              "filled the position, and the posting is now closed.")
    assert r["stage"] == "Rejected"


def test_confirmation_boilerplate_is_not_rejection():
    # "Unfortunately" here is about not replying to everyone, not a rejection.
    r = parse("Thanks for applying to Hooli!", "Hooli Talent Team <no-reply@ashbyhq.com>",
              "Thank you for applying to the ML Intern role at Hooli! We'll be in touch if your "
              "qualifications match. Unfortunately, due to the high volume of interest, we can't reply to everyone.")
    assert r["stage"] == "Applied"


def test_confirmation_mentioning_interview_tips_stays_applied():
    r = parse("You have successfully submitted your application", "Talent <talent@vandelay.com>",
              "Thank you for applying. While you wait, visit our careers page for tips on acing your interview.")
    assert r["stage"] == "Applied"


def test_assessment_from_test_platform():
    r = parse("Coding assessment invitation", "HackerRank <support@hackerrankforwork.com>",
              "You have been invited to take the coding assessment for Vandelay.")
    assert (r["stage"], r["company"]) == ("Assessment", "Vandelay")


def test_interview_and_recruiter_name_maps_to_company():
    r = parse("Business Analyst Student - Video Interview", "Jane Smith <JSmith@abc.com>",
              "After reviewing your resume we would like you to complete a one-way video interview.")
    assert (r["stage"], r["company"]) == ("Interview", "ABC")


def test_ats_display_name_is_not_a_person():
    r = parse("Thank you for applying!", '"Wayne Capital @ icims" <waynecapital+autoreply@talent.icims.com>',
              "Thank you for your application to the Data Science Intern position.")
    assert r["company"] == "Wayne Capital"


def test_job_alerts_are_ignored():
    assert parse("Data Scientist jobs for you", "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
                 "Apply now") is None


def test_role_extraction():
    r = parse("We received your application", "Stark Industries <noreply@starkindustries.com>",
              "Thank you for applying to Stark Industries! We appreciate the time you took to apply for the "
              "AI/ML Research Co-op position.")
    assert r["role"] == "AI/ML Research Co-op"


def test_summary_keeps_latest_real_stage():
    recs = [
        g.parse_message(email("Thank you for applying", "Globex Careers <jobs@globex.com>",
                              "Thank you for applying to Globex.", date="2026-09-01T10:00-06:00")),
        g.parse_message(email("Update", "Globex Careers <jobs@globex.com>",
                              "Unfortunately, we have decided not to move forward.", date="2026-09-10T10:00-06:00")),
        g.parse_message(email("Thank you for applying", "Globex Careers <jobs@globex.com>",
                              "Thank you for applying to Globex.", date="2026-09-20T10:00-06:00")),
    ]
    [row] = g.summarize(recs)
    assert row["status"] == "Rejected" and row["emails"] == 3


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError:
            failed += 1
            print(f"  FAIL  {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
