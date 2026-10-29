"""P6-DATA-3 (decision-independent parts) and the P6-DATA-6 pipeline regression.

* The geocoder is a provider seam: the seven-city fixture stays for development and
  test and is refused in staging/production; Amazon Location Service Places v2 is an
  adapter behind configuration, exercised here only against a stubbed client (no
  network, no credentials).
* Lookups are validated, limited per workspace and per process, cached per tenant,
  and an answer is accepted only when it is a single, good-enough place.
* No lookup ends in a silent 404: every failure is a distinct code with a message
  that says how to continue.
* End to end, a London, UK signal no longer counts as inside a London, Ontario
  location's scout area, and the four seeded fixture markets still do.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError, NoCredentialsError
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings, get_settings
from app.db import seed as seed_mod
from app.db.models import Base
from app.db.session import get_db
from app.geography.geocoder import (
    AmazonLocationGeocoder,
    FixtureGeocoder,
    GeocodeFailure,
    GeocodeResult,
    GeocodingService,
    build_geocoder,
    build_geocoding_service,
    choose,
    classify_location_error,
    fixture_geocode,
    get_geocoding_service,
)
from app.geography.markets import MarketIdentity
from app.infra.cache import InMemoryCache
from app.jobs.pipeline import _run
from app.locations.models import BusinessLocation, GeoCoverageRule
from app.main import app
from app.opportunities.models import Opportunity
from app.organizations.models import User
from app.privacy.inventory import RetentionClass
from app.scouting_requests.models import ScoutRequest
from app.signals.models import NormalizedSignal, SignalLocationEvidence
from app.tests._auth2_support import live_bearer
from app.tests._platform_support import production_vector_kwargs

API = get_settings().api_prefix

# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

_GEOCODER_UNSET = {
    "geocoder_backend": None,
    "geocoder_aws_region": None,
    "geocoder_timeout_seconds": 5.0,
    "geocoder_cache_ttl_seconds": 7 * 24 * 3600,
}
_LIVE = {
    "secret_key": "x" * 64,
    "llm_provider": "openai",
    "llm_api_key": "k" * 20,
    "mail_backend": "ses",
    "public_web_origin": "https://app.example.com",
    "mail_from_address": "no-reply@example.com",
    "mail_ses_region": "us-east-1",
}
_PRODUCTION_STACK = {
    "app_mode": "full",
    "database_url": "postgresql+psycopg://u:p@db.invalid:5432/d",
    "queue_backend": "redis",
    "cache_backend": "redis",
    "redis_url": "redis://unused.invalid:6379/0",
    "storage_backend": "s3",
    "s3_bucket": "unused",
    # P6-PLAT-1 (strategy B): the acknowledged bounded in-process backend, as production
    # really validates it - no patched build fact.
    **production_vector_kwargs(),
    "rate_limit_backend": "redis",
    # Retention durations have no default and production requires every one (P6-PRIV-1):
    # inert test values, not a policy.
    **{c.setting_name: 30 for c in RetentionClass},
}


def _settings(environment: str, **overrides) -> Settings:
    """Geocoder fields always passed explicitly, so ambient GEOCODER_* cannot decide."""
    kwargs: dict = {"_env_file": None, "environment": environment, **_GEOCODER_UNSET}
    if environment in ("staging", "production"):
        kwargs |= _LIVE
    if environment == "production":
        kwargs |= _PRODUCTION_STACK
    kwargs.update(overrides)
    return Settings(**kwargs)


class TestGeocoderSettings:
    @pytest.mark.parametrize("environment", ["development", "test"])
    def test_unset_resolves_to_the_fixture_in_development_and_test(self, environment):
        s = _settings(environment)
        assert s.effective_geocoder_backend == "fixture"
        assert isinstance(build_geocoder(s), FixtureGeocoder)

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_unset_means_no_geocoder_in_staging_and_production(self, environment):
        s = _settings(environment)
        assert s.effective_geocoder_backend is None
        assert build_geocoder(s) is None
        assert build_geocoding_service(s, cache=InMemoryCache()).provider == "none"

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_production_refuses_the_fixture_geocoder(self, environment):
        with pytest.raises(ValidationError) as caught:
            _settings(environment, geocoder_backend="fixture")
        assert "geocoder_backend=fixture is allowed only in development or test" in str(
            caught.value
        )

    def test_fixture_geocoder_refuses_construction_outside_development_and_test(self):
        for environment in ("staging", "production"):
            with pytest.raises(GeocodeFailure) as caught:
                FixtureGeocoder(environment=environment)
            assert caught.value.reason == "not_configured"

    @pytest.mark.parametrize("environment", ["development", "staging", "production"])
    def test_aws_location_is_accepted_with_a_region(self, environment):
        s = _settings(environment, geocoder_backend="aws_location", geocoder_aws_region="eu-west-2")
        geocoder = build_geocoder(s)
        assert isinstance(geocoder, AmazonLocationGeocoder)
        assert geocoder.provider == "aws_location"

    @pytest.mark.parametrize(
        ("overrides", "needle"),
        [
            ({"geocoder_backend": "aws_location"}, "requires geocoder_aws_region"),
            ({"geocoder_aws_region": "not a region"}, "geocoder_aws_region must be"),
            ({"geocoder_timeout_seconds": 0}, "geocoder_timeout_seconds"),
            ({"geocoder_timeout_seconds": 31}, "geocoder_timeout_seconds"),
            ({"geocoder_cache_ttl_seconds": -1}, "geocoder_cache_ttl_seconds"),
            ({"geocoder_workspace_requests_per_minute": 0}, "geocoder_workspace_requests"),
            ({"geocoder_provider_burst": 0}, "geocoder_provider_burst"),
            ({"geocoder_provider_requests_per_second": 0}, "geocoder_provider_requests"),
            ({"geocoder_min_match_score": 1.5}, "geocoder_min_match_score"),
        ],
    )
    def test_bounds_are_validated(self, overrides, needle):
        with pytest.raises(ValidationError) as caught:
            _settings("development", **overrides)
        assert needle in str(caught.value)


# --------------------------------------------------------------------------- #
# Fixture geocoder
# --------------------------------------------------------------------------- #


class TestFixtureGeocoder:
    @pytest.mark.parametrize(
        ("query", "city", "country_code", "region_code"),
        [
            ("Dallas", "Dallas", "US", "US-TX"),
            ("Dallas, TX", "Dallas", "US", "US-TX"),
            ("Dallas TX", "Dallas", "US", "US-TX"),
            ("123 Main St, Dallas, US", "Dallas", "US", "US-TX"),
            ("London", "London", "GB", "GB-ENG"),
            ("London, UK", "London", "GB", "GB-ENG"),
            ("london, england", "London", "GB", "GB-ENG"),
            ("1 Marina Road, Lagos, Nigeria", "Lagos", "NG", "NG:lagos"),
            ("Nairobi, Kenya", "Nairobi", "KE", "KE:nairobi"),
        ],
    )
    def test_resolves_the_fixture_cities(self, query, city, country_code, region_code):
        result = fixture_geocode(query)
        assert result is not None
        assert (result.city, result.country_code, result.region_code) == (
            city,
            country_code,
            region_code,
        )
        assert result.provider == "fixture"

    @pytest.mark.parametrize(
        "query",
        [
            "London, Ontario",
            "London, ON",
            "London, Canada",
            "New London",
            "Londonderry",
            "Dallas, GA",
            "Paris",
            "Lagos, Portugal",
            "Portland, OR",
            "Berlin",
        ],
    )
    def test_never_answers_a_confusable_or_unknown_place_with_a_fixture_city(self, query):
        assert fixture_geocode(query) is None

    def test_result_feeds_the_market_identity(self):
        result = fixture_geocode("London")
        assert result.country == "United Kingdom"
        assert result.market == MarketIdentity("london", "GB-ENG", "GB")


# --------------------------------------------------------------------------- #
# Amazon Location Service adapter (stubbed client only)
# --------------------------------------------------------------------------- #


def _item(
    *,
    lon: float,
    lat: float,
    locality: str | None,
    region_code: str | None,
    region_name: str | None,
    code2: str,
    country: str,
    score: float,
    tz: str | None = None,
    label: str | None = None,
) -> dict:
    address: dict = {"Country": {"Code2": code2, "Code3": "XXX", "Name": country}}
    if locality:
        address["Locality"] = locality
    if region_code or region_name:
        address["Region"] = {k: v for k, v in (("Code", region_code), ("Name", region_name)) if v}
    if label:
        address["Label"] = label
    item: dict = {
        "PlaceId": "opaque-place-id",
        "PlaceType": "Locality",
        "Title": label or locality or country,
        "Address": address,
        "Position": [lon, lat],
        "MatchScores": {"Overall": score},
    }
    if tz:
        item["TimeZone"] = {"Name": tz, "Offset": "-05:00", "OffsetSeconds": -18000}
    return item


LONDON_ON = _item(
    lon=-81.2453,
    lat=42.9849,
    locality="London",
    region_code="ON",
    region_name="Ontario",
    code2="CA",
    country="Canada",
    score=0.97,
    tz="America/Toronto",
    label="London, ON, Canada",
)
LONDON_UK = _item(
    lon=-0.1278,
    lat=51.5074,
    locality="London",
    region_code="ENG",
    region_name="England",
    code2="GB",
    country="United Kingdom",
    score=0.98,
    tz="Europe/London",
    label="London, England, GBR",
)


class StubPlaces:
    """Stands in for boto3's geo-places client; records every request."""

    def __init__(self, response=None, error: BaseException | None = None):
        self.response = response if response is not None else {"ResultItems": []}
        self.error = error
        self.calls: list[dict] = []

    def geocode(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def _client_error(code: str, status: int, message: str = "secret 1 Marina Road") -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "Geocode",
    )


class TestAmazonLocationGeocoder:
    def test_request_is_bounded_and_marked_for_storage(self):
        stub = StubPlaces({"ResultItems": [LONDON_ON]})
        geocoder = AmazonLocationGeocoder(region="eu-west-2", timeout_seconds=5, client=stub)
        geocoder.geocode("London, Ontario")
        assert stub.calls == [
            {
                "QueryText": "London, Ontario",
                "MaxResults": 5,
                "IntendedUse": "Storage",
                "AdditionalFeatures": ["TimeZone"],
                "Language": "en",
            }
        ]

    def test_answer_is_normalised_into_the_market_identity(self):
        stub = StubPlaces({"ResultItems": [LONDON_ON]})
        [result] = AmazonLocationGeocoder(
            region="eu-west-2", timeout_seconds=5, client=stub
        ).geocode("London, Ontario")
        assert result == GeocodeResult(
            latitude=42.9849,
            longitude=-81.2453,
            city="London",
            state_province="Ontario",
            country="Canada",
            timezone="America/Toronto",
            confidence=0.97,
            country_code="CA",
            region_code="CA-ON",
            label="London, ON, Canada",
            provider="aws_location",
        )
        assert result.market == MarketIdentity("london", "CA-ON", "CA")

    def test_an_unlisted_region_is_kept_by_its_english_name(self):
        lagos = _item(
            lon=3.3792,
            lat=6.5244,
            locality="Lagos",
            region_code="LA",
            region_name="Lagos",
            code2="NG",
            country="Nigeria",
            score=0.9,
        )
        [result] = AmazonLocationGeocoder(
            region="eu-west-2", timeout_seconds=5, client=StubPlaces({"ResultItems": [lagos]})
        ).geocode("Lagos, Nigeria")
        assert (result.state_province, result.region_code) == ("Lagos", "NG:lagos")
        # The saved location matches evidence that states only city and country.
        assert result.market == MarketIdentity("lagos", "NG:lagos", "NG")

    def test_malformed_items_are_skipped(self):
        bad = [
            "not a dict",
            {"Position": "nowhere"},
            {"Position": [1.0]},
            {"Position": [float("nan"), 1.0]},
            {"Position": [200.0, 1.0]},
            {"Position": [True, 1.0]},
        ]
        stub = StubPlaces({"ResultItems": [*bad, LONDON_UK]})
        results = AmazonLocationGeocoder(
            region="eu-west-2", timeout_seconds=5, client=stub
        ).geocode("London")
        assert [r.country_code for r in results] == ["GB"]

    @pytest.mark.parametrize(
        ("error", "reason"),
        [
            (_client_error("ThrottlingException", 429), "throttled"),
            (_client_error("AccessDeniedException", 403), "not_configured"),
            (_client_error("ValidationException", 400), "invalid_query"),
            (_client_error("InternalServerException", 500), "provider_error"),
            (ConnectTimeoutError(endpoint_url="https://geo-places.invalid"), "timeout"),
            (NoCredentialsError(), "not_configured"),
            (RuntimeError("1 Marina Road"), "provider_error"),
        ],
    )
    def test_failures_become_static_reasons_without_the_provider_message(self, error, reason):
        stub = StubPlaces(error=error)
        geocoder = AmazonLocationGeocoder(region="eu-west-2", timeout_seconds=5, client=stub)
        with pytest.raises(GeocodeFailure) as caught:
            geocoder.geocode("1 Marina Road, Lagos")
        assert caught.value.reason == reason
        assert str(caught.value) == reason
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert classify_location_error(error) == reason

    def test_construction_never_builds_a_client(self, monkeypatch):
        import app.geography.geocoder as geocoder_mod

        def _refuse(**_kwargs):
            raise AssertionError("client built at construction")

        monkeypatch.setattr(geocoder_mod, "_build_location_client", _refuse)
        AmazonLocationGeocoder(region="eu-west-2", timeout_seconds=5)


# --------------------------------------------------------------------------- #
# Choosing one answer
# --------------------------------------------------------------------------- #


def _result(city: str, region: str, code: str, confidence: float) -> GeocodeResult:
    return GeocodeResult(
        latitude=0.0,
        longitude=0.0,
        city=city,
        state_province=region,
        country=code,
        timezone=None,
        confidence=confidence,
        country_code=code,
        provider="test",
    )


class TestChoose:
    def test_two_equally_good_different_places_are_ambiguous(self):
        with pytest.raises(GeocodeFailure) as caught:
            choose(
                [_result("London", "England", "GB", 0.98), _result("London", "ON", "CA", 0.97)],
                min_score=0.7,
            )
        assert caught.value.reason == "ambiguous"

    def test_a_clear_winner_is_chosen(self):
        best = choose(
            [_result("London", "ON", "CA", 0.97), _result("London", "England", "GB", 0.71)],
            min_score=0.7,
        )
        assert best.country_code == "CA"

    def test_duplicate_answers_for_one_place_are_not_ambiguous(self):
        best = choose(
            [_result("Dallas", "TX", "US", 0.95), _result("Dallas", "Texas", "US", 0.94)],
            min_score=0.7,
        )
        assert best.city == "Dallas"

    def test_weak_answers_are_no_match(self):
        with pytest.raises(GeocodeFailure) as caught:
            choose([_result("Dallas", "TX", "US", 0.5)], min_score=0.7)
        assert caught.value.reason == "no_match"
        with pytest.raises(GeocodeFailure):
            choose([], min_score=0.7)


# --------------------------------------------------------------------------- #
# The service: validation, limits, cache
# --------------------------------------------------------------------------- #


class CountingGeocoder:
    provider = "counting"
    storable = True

    def __init__(self, results=None, error: str | None = None):
        self.results = results if results is not None else []
        self.error = error
        self.queries: list[str] = []

    def geocode(self, query):
        self.queries.append(query)
        if self.error:
            raise GeocodeFailure(self.error)
        return list(self.results)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _service(geocoder, *, cache=None, ttl=3600, per_minute=30, burst=10, rate=5.0, clock=None):
    return GeocodingService(
        geocoder,
        cache=cache if cache is not None else InMemoryCache(),
        cache_ttl_seconds=ttl,
        min_score=0.7,
        workspace_requests_per_minute=per_minute,
        provider_burst=burst,
        provider_requests_per_second=rate,
        clock=clock or Clock(),
    )


DALLAS = _result("Dallas", "TX", "US", 0.95)


class TestGeocodingService:
    def test_resolved_answers_are_cached_per_tenant(self):
        geocoder = CountingGeocoder([DALLAS])
        cache = InMemoryCache()
        service = _service(geocoder, cache=cache)
        first = service.geocode("Dallas, TX", organization_id="o1", workspace_id="w1")
        again = service.geocode("  dallas,   tx ", organization_id="o1", workspace_id="w1")
        assert first == again == DALLAS
        assert geocoder.queries == ["Dallas, TX"]
        service.geocode("Dallas, TX", organization_id="o2", workspace_id="w2")
        assert len(geocoder.queries) == 2  # another tenant never reads this tenant's entry
        # The address itself is never part of a cache key.
        assert all("dallas" not in key.lower() for key in cache._store)

    def test_no_match_and_ambiguous_answers_are_remembered(self):
        geocoder = CountingGeocoder([])
        service = _service(geocoder)
        for _ in range(2):
            with pytest.raises(GeocodeFailure) as caught:
                service.geocode("Atlantis", organization_id="o", workspace_id="w")
            assert caught.value.reason == "no_match"
        assert geocoder.queries == ["Atlantis"]

    def test_transient_failures_are_not_remembered(self):
        geocoder = CountingGeocoder(error="timeout")
        service = _service(geocoder)
        for _ in range(2):
            with pytest.raises(GeocodeFailure):
                service.geocode("Dallas", organization_id="o", workspace_id="w")
        assert len(geocoder.queries) == 2

    def test_cache_disabled_with_zero_ttl(self):
        geocoder = CountingGeocoder([DALLAS])
        service = _service(geocoder, ttl=0)
        service.geocode("Dallas", organization_id="o", workspace_id="w")
        service.geocode("Dallas", organization_id="o", workspace_id="w")
        assert len(geocoder.queries) == 2

    def test_an_unavailable_cache_is_a_miss_not_a_failure(self):
        class BrokenCache(InMemoryCache):
            def get(self, key, default=None):
                raise RuntimeError("redis down")

            def set(self, key, value, ttl_seconds=None):
                raise RuntimeError("redis down")

        service = _service(CountingGeocoder([DALLAS]), cache=BrokenCache())
        assert service.geocode("Dallas", organization_id="o", workspace_id="w") == DALLAS

    def test_a_corrupt_cache_entry_is_a_miss(self):
        geocoder = CountingGeocoder([DALLAS])
        cache = InMemoryCache()
        service = _service(geocoder, cache=cache)
        service.geocode("Dallas", organization_id="o", workspace_id="w")
        [key] = list(cache._store)
        cache.set(key, {"failure": "made_up"})
        assert service.geocode("Dallas", organization_id="o", workspace_id="w") == DALLAS
        cache.set(key, {"unexpected": 1})
        assert service.geocode("Dallas", organization_id="o", workspace_id="w") == DALLAS

    def test_workspace_limit_per_minute(self):
        clock = Clock()
        service = _service(CountingGeocoder([DALLAS]), per_minute=2, clock=clock)
        for _ in range(2):
            service.geocode("Dallas", organization_id="o", workspace_id="w")
        with pytest.raises(GeocodeFailure) as caught:
            service.geocode("Dallas", organization_id="o", workspace_id="w")
        assert caught.value.reason == "rate_limited"
        # Another workspace has its own window; the next minute starts afresh.
        service.geocode("Dallas", organization_id="o", workspace_id="other")
        clock.now += 60
        service.geocode("Dallas", organization_id="o", workspace_id="w")

    def test_provider_budget_is_process_wide(self):
        clock = Clock()
        geocoder = CountingGeocoder([DALLAS])
        service = _service(geocoder, ttl=0, burst=2, rate=1.0, clock=clock)
        service.geocode("Dallas", organization_id="o", workspace_id="a")
        service.geocode("Dallas", organization_id="o", workspace_id="b")
        with pytest.raises(GeocodeFailure) as caught:
            service.geocode("Dallas", organization_id="o", workspace_id="c")
        assert caught.value.reason == "throttled"
        clock.now += 1.0
        service.geocode("Dallas", organization_id="o", workspace_id="c")
        assert len(geocoder.queries) == 3

    @pytest.mark.parametrize("query", ["", " ", "a", "x" * 201, "\x00\x01"])
    def test_invalid_queries_never_reach_the_provider(self, query):
        geocoder = CountingGeocoder([DALLAS])
        with pytest.raises(GeocodeFailure) as caught:
            _service(geocoder).geocode(query, organization_id="o", workspace_id="w")
        assert caught.value.reason == "invalid_query"
        assert geocoder.queries == []

    def test_control_characters_are_stripped_before_the_provider_sees_them(self):
        geocoder = CountingGeocoder([DALLAS])
        _service(geocoder).geocode("Dallas,\r\nTX", organization_id="o", workspace_id="w")
        assert geocoder.queries == ["Dallas, TX"]

    def test_no_geocoder_means_not_configured(self):
        with pytest.raises(GeocodeFailure) as caught:
            _service(None).geocode("Dallas", organization_id="o", workspace_id="w")
        assert caught.value.reason == "not_configured"


# --------------------------------------------------------------------------- #
# HTTP route + the end-to-end pipeline regression (throwaway seeded database)
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("geocoding")
    engine = create_engine(
        f"sqlite:///{tmp / 'geo.db'}", connect_args={"check_same_thread": False}, future=True
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    original = seed_mod.SessionLocal
    seed_mod.SessionLocal = factory
    try:
        seed_mod.seed(reset=True)

        def _override_get_db():
            s = factory()
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        app.dependency_overrides[get_db] = _override_get_db
        with factory() as s:
            user = s.scalar(select(User).where(User.email == seed_mod.DEMO_EMAIL))
            location = s.scalar(select(BusinessLocation).limit(1))
            ids = {
                "user": user.id,
                "workspace": location.workspace_id,
                "organization": location.organization_id,
                "brand": location.brand_id,
            }
        ids["auth"] = {"Authorization": f"Bearer {live_bearer(ids['user'])}"}
        yield factory, ids
    finally:
        app.dependency_overrides.clear()
        seed_mod.SessionLocal = original
        engine.dispose()


def _post_geocode(world, service: GeocodingService, query: str):
    _, ids = world
    app.dependency_overrides[get_geocoding_service] = lambda: service
    try:
        # No lifespan: startup would check the default database's schema.
        return TestClient(app).post(
            f"{API}/geocode",
            json={"query": query},
            params={"workspace_id": ids["workspace"]},
            headers=ids["auth"],
        )
    finally:
        app.dependency_overrides.pop(get_geocoding_service, None)


class TestGeocodeRoute:
    def test_resolved_answer_shape(self, world):
        service = _service(FixtureGeocoder(environment="test"))
        r = _post_geocode(world, service, "London, UK")
        assert r.status_code == 200, r.text
        assert r.json() == {
            "latitude": 51.5074,
            "longitude": -0.1278,
            "city": "London",
            "state_province": "England",
            "country": "United Kingdom",
            "timezone": "Europe/London",
            "confidence": 0.95,
            "country_code": "GB",
            "region_code": "GB-ENG",
            "label": "London, England, United Kingdom",
            "provider": "fixture",
        }

    @pytest.mark.parametrize(
        ("service_geocoder", "query", "status", "code", "needle"),
        [
            (CountingGeocoder([]), "Atlantis", 422, "location_unresolvable", "couldn't find"),
            (
                CountingGeocoder(
                    [_result("London", "ON", "CA", 0.97), _result("London", "England", "GB", 0.98)]
                ),
                "London",
                422,
                "location_ambiguous",
                "more than one place",
            ),
            (None, "Dallas", 503, "geocoder_not_configured", "not available"),
            (CountingGeocoder(error="timeout"), "Dallas", 503, "geocoder_unavailable", "Try"),
            (CountingGeocoder(error="throttled"), "Dallas", 503, "geocoder_unavailable", "Try"),
            (
                CountingGeocoder(error="provider_error"),
                "Dallas",
                503,
                "geocoder_unavailable",
                "Try",
            ),
            (CountingGeocoder(error="invalid_query"), "Dallas", 422, "geocode_invalid_query", ""),
        ],
    )
    def test_every_failure_has_a_code_and_a_way_on(
        self, world, service_geocoder, query, status, code, needle
    ):
        r = _post_geocode(world, _service(service_geocoder), query)
        assert r.status_code == status, r.text
        error = r.json()["error"]
        assert error["code"] == code
        assert needle in error["message"]
        if code != "geocode_invalid_query":
            assert "city, state or province and country" in error["message"]
        assert r.status_code != 404

    def test_rate_limited(self, world):
        service = _service(CountingGeocoder([DALLAS]), per_minute=1)
        assert _post_geocode(world, service, "Dallas").status_code == 200
        r = _post_geocode(world, service, "Dallas")
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "geocode_rate_limited"

    def test_query_length_is_bounded_by_the_schema(self, world):
        r = _post_geocode(world, _service(CountingGeocoder([DALLAS])), "x" * 201)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "validation_error"


def _add_location(s, ids, *, key: str, city, region, country, coverage: dict) -> str:
    location = BusinessLocation(
        id=f"loc-{key}",
        organization_id=ids["organization"],
        workspace_id=ids["workspace"],
        brand_id=ids["brand"],
        name=f"Location {key}",
        city=city,
        state_province=region,
        country=country,
        is_active=True,
    )
    s.add(location)
    s.add(
        GeoCoverageRule(
            id=f"geo-{key}",
            organization_id=ids["organization"],
            workspace_id=ids["workspace"],
            location_id=location.id,
            **coverage,
        )
    )
    return location.id


def _scout(s, ids, *, key: str, location_id: str, market: str) -> str:
    request = ScoutRequest(
        id=f"scout-{key}",
        organization_id=ids["organization"],
        workspace_id=ids["workspace"],
        brand_id=ids["brand"],
        location_id=location_id,
        name=f"Scout {key}",
        status="queued",
        source_types=[],
        keywords=["coffee", "cold brew", "loyalty", "oat milk", "latte"],
        resolved_market=market,
    )
    s.add(request)
    s.flush()
    return request.id


def _inside_flags(s, request_id: str) -> tuple[set, set]:
    evidence = s.execute(
        select(SignalLocationEvidence.inside_scout_area, SignalLocationEvidence.resolved_market)
        .join(NormalizedSignal, NormalizedSignal.id == SignalLocationEvidence.normalized_signal_id)
        .where(NormalizedSignal.scout_request_id == request_id)
    ).all()
    opportunities = s.execute(
        select(Opportunity.inside_scout_area, Opportunity.decision).where(
            Opportunity.scout_request_id == request_id
        )
    ).all()
    return {tuple(r) for r in evidence}, {tuple(r) for r in opportunities}


class TestPipelineMarketIsolation:
    def test_seeded_fixture_markets_stay_inside_their_own_scout_area(self, world):
        factory, _ = world
        with factory() as s:
            rows = s.execute(
                select(Opportunity.resolved_market, Opportunity.inside_scout_area)
            ).all()
        assert {m for m, _ in rows} == {"Dallas, TX", "London, UK", "Lagos, NG", "Nairobi, KE"}
        assert all(inside for _, inside in rows)

    @pytest.mark.parametrize(
        "coverage",
        [
            # As the location dialog saves it: radius, no market list.
            {"coverage_type": "radius", "radius_miles": 25, "included_markets": []},
            # A bare "London" typed for the Ontario location.
            {"coverage_type": "city", "included_markets": ["London"]},
            {"coverage_type": "state", "included_markets": []},
        ],
        ids=["radius-unlisted", "bare-london", "state"],
    )
    def test_london_uk_signals_are_outside_a_london_ontario_location(self, world, coverage):
        factory, ids = world
        key = f"on-{coverage['coverage_type']}-{len(coverage['included_markets'])}"
        with factory() as s:
            location_id = _add_location(
                s,
                ids,
                key=key,
                city="London",
                region="Ontario",
                country="Canada",
                coverage=coverage,
            )
            # The request market names London, UK, so the fixture connector returns
            # London, UK signals: exactly the blend the coverage rule must refuse.
            request_id = _scout(s, ids, key=key, location_id=location_id, market="London, UK")
            stats = _run(s, request_id)
            s.commit()
            evidence, opportunities = _inside_flags(s, request_id)
        assert stats["scanned"] > 0 and stats["opportunities"] > 0
        assert {market for _, market in evidence} - {None} == {"London, UK"}
        assert not any(inside for inside, _ in evidence)
        assert opportunities and not any(inside for inside, _ in opportunities)
        assert {decision for _, decision in opportunities} <= {"monitor", "stay_silent", "block"}

    def test_the_same_signals_are_inside_a_london_england_location(self, world):
        factory, ids = world
        with factory() as s:
            location_id = _add_location(
                s,
                ids,
                key="uk",
                city="London",
                region="England",
                country="United Kingdom",
                coverage={"coverage_type": "radius", "radius_miles": 25, "included_markets": []},
            )
            request_id = _scout(s, ids, key="uk", location_id=location_id, market="London, UK")
            _run(s, request_id)
            s.commit()
            evidence, opportunities = _inside_flags(s, request_id)
        assert all(inside for inside, market in evidence if market is not None)
        assert opportunities and all(inside for inside, _ in opportunities)

    def test_a_location_without_a_country_scopes_nothing(self, world):
        factory, ids = world
        with factory() as s:
            location_id = _add_location(
                s,
                ids,
                key="nocountry",
                city="London",
                region=None,
                country=None,
                coverage={"coverage_type": "radius", "radius_miles": 25, "included_markets": []},
            )
            request_id = _scout(
                s, ids, key="nocountry", location_id=location_id, market="London, UK"
            )
            _run(s, request_id)
            s.commit()
            evidence, opportunities = _inside_flags(s, request_id)
        assert not any(inside for inside, _ in evidence)
        assert not any(inside for inside, _ in opportunities)
