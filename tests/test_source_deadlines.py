"""Source request budgets with a virtual clock and no network or database."""
from types import SimpleNamespace

import pytest
import requests
from urllib3.util import Timeout

from tourfinder.sources import joinup, waavo


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []
        self.oversleep = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds + self.oversleep


class Session:
    def __init__(self, outcomes):
        self.headers = {}
        self.outcomes = iter(outcomes)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = next(self.outcomes)
        if callable(outcome):
            outcome = outcome()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def response(status=200, payload=None):
    def raise_for_status():
        if status >= 400:
            raise requests.HTTPError(f"fixture HTTP {status}")
    return SimpleNamespace(status_code=status, raise_for_status=raise_for_status,
                           json=lambda: {"fixture": True} if payload is None else payload)


@pytest.fixture(params=["joinup", "waavo"])
def source(request, monkeypatch):
    clock = Clock()
    if request.param == "joinup":
        module, client_cls, error, blocked, deadline_error, attempts, backoff = (
            joinup, joinup.JoinUpClient, joinup.JoinUpError, joinup.JoinUpBlockedError,
            joinup.JoinUpDeadlineError, 5, 20)
    else:
        module, client_cls, error, blocked, deadline_error, attempts, backoff = (
            waavo, waavo.WaavoClient, waavo.WaavoError, waavo.WaavoBlockedError,
            waavo.WaavoDeadlineError, 4, 5)
    monkeypatch.setattr(module, "time", clock)
    monkeypatch.setattr(module, "random", SimpleNamespace(uniform=lambda *_: 0))

    def setup(outcomes, *, delay=0, deadline=None):
        session = Session(outcomes)
        client = client_cls(delay=delay, session=session, deadline=deadline)
        call = (lambda: client._get("tour/tours", fixture="yes")) if module is joinup else (
            lambda: client._get(fixture="yes"))
        return client, session, call

    return SimpleNamespace(clock=clock, setup=setup, error=error, blocked=blocked,
                           deadline_error=deadline_error, attempts=attempts, backoff=backoff)


@pytest.mark.parametrize("failure", [requests.Timeout, requests.ConnectionError, 429, 503])
def test_retry_exhaustion_has_no_final_backoff(source, failure):
    outcomes = [(response(failure) if isinstance(failure, int) else failure("fixture"))
                for _ in range(source.attempts)]
    client, session, call = source.setup(outcomes)
    with pytest.raises(source.error, match="retries exhausted"):
        call()
    backoff = 5 if isinstance(failure, int) else source.backoff
    assert source.clock.sleeps == [backoff * n for n in range(1, source.attempts)]
    assert len(session.calls) == client.requests_made == source.attempts
    assert all(kwargs["timeout"] == 60 for _, kwargs in session.calls)


@pytest.mark.parametrize("deadline", [-1, 0, 1, 2])
def test_expired_or_insufficient_throttle_budget_never_sleeps_or_requests(source, deadline):
    client, session, call = source.setup([], delay=2, deadline=deadline)
    with pytest.raises(source.deadline_error, match="partial: collection time budget exhausted"):
        call()
    assert source.clock.sleeps == []
    assert session.calls == []
    assert client.requests_made == 0


def test_scheduler_oversleep_prevents_request(source):
    source.clock.oversleep = 10
    client, session, call = source.setup([], delay=1, deadline=5)
    with pytest.raises(source.deadline_error):
        call()
    assert source.clock.sleeps == [1]
    assert session.calls == []
    assert client.requests_made == 0


@pytest.mark.parametrize("deadline,remaining,per_phase", [(8, 6, 6), (200, 198, 60)])
def test_request_timeout_uses_remaining_budget_after_throttle(source, deadline, remaining, per_phase):
    client, session, call = source.setup([response()], delay=2, deadline=deadline)
    assert call() == {"fixture": True}
    timeout = session.calls[0][1]["timeout"]
    assert isinstance(timeout, Timeout)
    assert timeout.total == remaining
    assert timeout.connect_timeout == per_phase
    timeout.start_connect()
    assert 0 < timeout.read_timeout <= per_phase
    assert source.clock.sleeps == [2]
    assert client.requests_made == 1


@pytest.mark.parametrize("http_failure", [False, True])
def test_backoff_that_cannot_fit_stops_without_sleep_or_retry(source, http_failure):
    outcome = response(503) if http_failure else requests.ConnectionError("fixture")
    client, session, call = source.setup([outcome], deadline=5)
    with pytest.raises(source.deadline_error):
        call()
    assert len(session.calls) == client.requests_made == 1
    assert source.clock.sleeps == []


def test_request_failure_after_deadline_does_not_retry(source):
    def late_failure():
        source.clock.now = 6
        return requests.Timeout("fixture")
    client, session, call = source.setup([late_failure], deadline=5)
    with pytest.raises(source.deadline_error):
        call()
    assert len(session.calls) == client.requests_made == 1
    assert source.clock.sleeps == []


def test_backoff_oversleep_stops_before_next_throttle(source):
    source.clock.oversleep = 100
    client, session, call = source.setup([requests.Timeout("fixture")], deadline=100)
    with pytest.raises(source.deadline_error):
        call()
    assert source.clock.sleeps == [source.backoff]
    assert len(session.calls) == client.requests_made == 1


def test_next_throttle_must_also_fit_after_backoff(source):
    client, session, call = source.setup(
        [requests.Timeout("fixture")], delay=1, deadline=source.backoff + 1.5)
    with pytest.raises(source.deadline_error):
        call()
    assert source.clock.sleeps == [1, source.backoff]
    assert len(session.calls) == client.requests_made == 1


def test_success_arriving_late_is_preserved_but_no_next_request_is_started(source):
    def late_success():
        source.clock.now = 6
        return response(payload={"acquired": [1, 2]})
    client, session, call = source.setup([late_success], deadline=5)
    assert call() == {"acquired": [1, 2]}
    with pytest.raises(source.deadline_error):
        call()
    assert len(session.calls) == client.requests_made == 1
    assert source.clock.sleeps == []


def test_403_immediately_aborts_even_when_response_arrives_after_deadline(source):
    def blocked():
        source.clock.now = 6
        return response(403)
    client, session, call = source.setup([blocked], deadline=5)
    with pytest.raises(source.blocked):
        call()
    assert len(session.calls) == client.requests_made == 1
    assert source.clock.sleeps == []


def test_other_http_errors_are_not_retried(source):
    client, session, call = source.setup([response(400)], deadline=100)
    with pytest.raises(requests.HTTPError):
        call()
    assert len(session.calls) == client.requests_made == 1
    assert source.clock.sleeps == []


def test_successful_retry_keeps_original_throttle_and_backoff(source):
    client, session, call = source.setup([response(503), response()], delay=1, deadline=30)
    assert call() == {"fixture": True}
    assert source.clock.sleeps == [1, 5, 1]
    assert len(session.calls) == client.requests_made == 2
    assert session.calls[1][1]["timeout"].total == 23
