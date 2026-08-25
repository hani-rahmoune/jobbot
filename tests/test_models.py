from __future__ import annotations

import pytest
from pydantic import ValidationError

from jobbot.models import Job


def _make_job(**overrides: object) -> Job:
    fields = {
        "company": "Acme Corp",
        "title": "Ingénieur Logiciel",
        "location": "Paris, France",
        "contract_type": "internship",
        "url": "https://example.com/jobs/1",
        "source": "greenhouse",
        "external_id": "1",
    }
    fields.update(overrides)
    return Job(**fields)


def test_job_id_stable_across_identical_constructions() -> None:
    assert _make_job().job_id == _make_job().job_id


def test_job_id_differs_when_url_differs() -> None:
    # external_id=None forces the fallback formula, which includes the url.
    job1 = _make_job(external_id=None, url="https://example.com/jobs/1")
    job2 = _make_job(external_id=None, url="https://example.com/jobs/2")
    assert job1.job_id != job2.job_id


def test_job_id_ignores_accent_and_case_differences_in_title_and_location() -> None:
    job1 = _make_job(external_id=None, title="Développeur", location="Paris, France")
    job2 = _make_job(external_id=None, title="DEVELOPPEUR", location="PARIS, FRANCE")
    assert job1.job_id == job2.job_id


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_empty_or_whitespace_title_raises(blank: str) -> None:
    with pytest.raises(ValidationError):
        _make_job(title=blank)


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_empty_or_whitespace_company_raises(blank: str) -> None:
    with pytest.raises(ValidationError):
        _make_job(company=blank)


def test_content_fingerprint_changes_when_title_differs_by_a_real_word() -> None:
    job1 = _make_job(title="Ingénieur Logiciel Junior")
    job2 = _make_job(title="Ingénieur Logiciel Senior")
    assert job1.content_fingerprint != job2.content_fingerprint


def test_content_fingerprint_unchanged_when_text_past_600_chars_differs() -> None:
    shared_prefix = "Description du poste. " * 30  # well over 600 chars
    assert len(shared_prefix) > 600
    job1 = _make_job(description=shared_prefix + "Mentions légales A.")
    job2 = _make_job(description=shared_prefix + "Une toute autre mention légale B.")
    assert job1.content_fingerprint == job2.content_fingerprint


def test_content_fingerprint_identical_for_near_duplicate_repost(
    greenhouse_payload, greenhouse_source
) -> None:
    jobs = {job.external_id: job for job in greenhouse_source.parse(greenhouse_payload["jobs"])}
    original, repost = jobs["1001"], jobs["1008"]
    assert original.content_fingerprint == repost.content_fingerprint


def test_job_id_different_for_near_duplicate_repost(
    greenhouse_payload, greenhouse_source
) -> None:
    jobs = {job.external_id: job for job in greenhouse_source.parse(greenhouse_payload["jobs"])}
    original, repost = jobs["1001"], jobs["1008"]
    assert original.job_id != repost.job_id
