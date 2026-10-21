"""Geocoding (P6-DATA-3): one seam, providers behind configuration.

Backends, selected by ``Settings.effective_geocoder_backend``:

* ``fixture`` - seven offline cities, for development and test only. Settings
  validation refuses it in staging and production, and so does its constructor.
* ``aws_location`` - Amazon Location Service Places v2 ``Geocode`` through boto3 and
  the ambient credential chain (the ECS API task role; there is no key setting). A
  result is saved on the location, so every request sets ``IntendedUse=Storage``,
  which the provider's terms require for stored results.
* none - unset in staging/production until the operator selects a provider. A
  lookup then answers ``not_configured`` and the customer types the city, state or
  province and country, which is what scouting matches on (P6-DATA-6).

:class:`GeocodingService` adds what every backend shares: query validation, a
per-workspace request limit, a process-wide provider call budget, a tenant-scoped
cache of answers, and a minimum match score. A failure is a
:class:`GeocodeFailure` carrying only a static ``reason``; a provider's own message
(which can quote the customer's address) is never logged, returned or chained.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any, Final, Literal, Protocol

from app.connectors.ratelimit import TokenBucket
from app.core.config import Settings, get_settings
from app.core.logging import get_logger, log_event
from app.geography.markets import (
    MarketIdentity,
    country_name,
    fold,
    market_from_parts,
    normalize_country,
    normalize_region,
    parse_observed,
)
from app.infra.cache import MISS, Cache, tenant_cache_key

logger = get_logger(__name__)

FailureReason = Literal[
    "invalid_query",
    "no_match",
    "ambiguous",
    "not_configured",
    "rate_limited",
    "throttled",
    "timeout",
    "provider_error",
]

#: Provider limit on ``QueryText`` (Places v2 ``Geocode``); the API schema matches it.
MAX_QUERY_LENGTH: Final = 200
#: Two answers whose match scores are this close are equally good readings.
_AMBIGUITY_MARGIN: Final = 0.05
#: How long an answer that found no single place is remembered (seconds).
_NEGATIVE_TTL_SECONDS: Final = 3600
#: Failures that are a property of the query, not of the moment, and may be cached.
_CACHEABLE_FAILURES: Final = frozenset({"no_match", "ambiguous"})


class GeocodeFailure(Exception):
    """A lookup produced no usable place. ``reason`` is the only payload."""

    def __init__(self, reason: FailureReason) -> None:
        super().__init__(reason)
        self.reason: FailureReason = reason


@dataclass(frozen=True)
class GeocodeResult:
    latitude: float
    longitude: float
    city: str | None
    state_province: str | None
    country: str | None
    timezone: str | None
    confidence: float
    #: ISO 3166-1 alpha-2, e.g. "CA".
    country_code: str | None = None
    #: Structured region key, e.g. "CA-ON" (see ``app.geography.markets``).
    region_code: str | None = None
    #: The provider's one-line description of the place.
    label: str | None = None
    provider: str = "fixture"

    @property
    def market(self) -> MarketIdentity | None:
        """The identity a location saved from this answer is matched by."""
        return market_from_parts(
            city=self.city, region=self.state_province, country=self.country_code
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GeocodeResult:
        """Rebuild a cached answer; ``ValueError`` for anything not shaped like one."""
        values = {key: data.get(key) for key in cls.__dataclass_fields__}
        for key in ("latitude", "longitude", "confidence"):
            if _number(values[key]) is None:
                raise ValueError(f"cached geocode answer lacks {key}")
        if not isinstance(values["provider"], str):
            raise ValueError("cached geocode answer lacks provider")
        for key in (
            "city",
            "state_province",
            "country",
            "timezone",
            "country_code",
            "region_code",
            "label",
        ):
            if values[key] is not None and not isinstance(values[key], str):
                raise ValueError(f"cached geocode answer has a malformed {key}")
        return cls(**values)


class Geocoder(Protocol):
    provider: str
    #: Whether this provider's terms let an answer be cached and stored.
    storable: bool

    def geocode(self, query: str) -> list[GeocodeResult]:
        """Candidate places for ``query``, best first; may raise GeocodeFailure."""
        ...


def _normalized(
    *,
    latitude: float,
    longitude: float,
    city: str | None,
    region: str | None,
    country_code: str | None,
    timezone: str | None,
    confidence: float,
    label: str | None,
    provider: str,
    country_fallback: str | None = None,
) -> GeocodeResult:
    """A result whose country and region read back to the same market identity."""
    code = normalize_country(country_code) if country_code else None
    region_key, code = normalize_region(region, code)
    return GeocodeResult(
        latitude=latitude,
        longitude=longitude,
        city=city or None,
        state_province=region or None,
        country=country_name(code) or country_fallback,
        timezone=timezone or None,
        confidence=round(confidence, 2),
        country_code=code,
        region_code=region_key,
        label=label or None,
        provider=provider,
    )


# --- fixture (development / test) --------------------------------------------------

_FIXTURE_CITIES: Final[dict[str, tuple[float, float, str, str, str, str]]] = {
    # key: (lat, lon, city, state/province, ISO country, timezone)
    "dallas": (32.7767, -96.7970, "Dallas", "Texas", "US", "America/Chicago"),
    "london": (51.5074, -0.1278, "London", "England", "GB", "Europe/London"),
    "lagos": (6.5244, 3.3792, "Lagos", "Lagos", "NG", "Africa/Lagos"),
    "nairobi": (-1.2921, 36.8219, "Nairobi", "Nairobi", "KE", "Africa/Nairobi"),
    "austin": (30.2672, -97.7431, "Austin", "Texas", "US", "America/Chicago"),
    "new york": (40.7128, -74.0060, "New York", "New York", "US", "America/New_York"),
    "manchester": (53.4808, -2.2426, "Manchester", "England", "GB", "Europe/London"),
}


def _fixture_result(key: str) -> GeocodeResult:
    lat, lon, city, state, country, tz = _FIXTURE_CITIES[key]
    return _normalized(
        latitude=lat,
        longitude=lon,
        city=city,
        region=state,
        country_code=country,
        timezone=tz,
        confidence=0.95,
        label=f"{city}, {state}, {country_name(country)}",
        provider="fixture",
    )


def fixture_geocode(query: str) -> GeocodeResult | None:
    """The fixture answer for ``query``, or ``None``.

    A comma-separated part of the query must BE a fixture city's name (never a
    substring of a longer name), and no other part may name a different country
    or region: "London, Ontario" is not London, England, and "New London" is not
    London. Used by the demo seed and by :class:`FixtureGeocoder`.
    """
    parts = [fold(p) for p in (query or "").split(",")]
    parts = [p for p in parts if p]
    for key in _FIXTURE_CITIES:
        if key not in parts:
            continue
        result = _fixture_result(key)
        others = [p for p in parts if p != key]
        if all(_consistent(p, result) for p in others):
            return result
    # "Dallas TX" / "London UK": no comma, but a structured reading names the city.
    identity = parse_observed(query) if len(parts) == 1 else None
    if identity is not None and identity.city in _FIXTURE_CITIES:
        result = _fixture_result(identity.city)
        if (identity.country is None or identity.country == result.country_code) and (
            identity.region is None or identity.region == result.region_code
        ):
            return result
    return None


def _consistent(part: str, result: GeocodeResult) -> bool:
    """False when ``part`` names a country or region other than the result's."""
    country = normalize_country(part)
    if country is not None:
        return country == result.country_code
    region, region_country = normalize_region(part, None)
    if region is not None:
        return region == result.region_code
    if result.country_code:
        local_region, _ = normalize_region(part, result.country_code)
        if local_region and not local_region.startswith(f"{result.country_code}:"):
            return local_region == result.region_code
    return True


class FixtureGeocoder:
    """Offline answers from seven fixture cities. Development and test only."""

    provider = "fixture"
    storable = True

    def __init__(self, *, environment: str) -> None:
        if environment not in ("development", "test"):
            raise GeocodeFailure("not_configured")

    def geocode(self, query: str) -> list[GeocodeResult]:
        result = fixture_geocode(query)
        return [result] if result else []


# --- Amazon Location Service (Places v2) -------------------------------------------

#: Places v2 error codes meaning "slow down".
_THROTTLED_CODES: Final = frozenset({"ThrottlingException", "TooManyRequestsException"})
#: Codes meaning this deployment cannot call the provider (missing grant, bad key).
_NOT_CONFIGURED_CODES: Final = frozenset(
    {"AccessDeniedException", "UnrecognizedClientException", "InvalidSignatureException"}
)


def classify_location_error(exc: BaseException) -> FailureReason:
    """Map a Places/botocore failure to a static reason. Only the exception type and
    the service's error *code* are read; the message (which can quote the query) is
    never inspected."""
    try:
        from botocore import exceptions as botocore_exceptions
    except ImportError:  # pragma: no cover - boto3 is absent only in base installs
        return "provider_error"

    if isinstance(
        exc,
        (
            botocore_exceptions.ReadTimeoutError,
            botocore_exceptions.ConnectTimeoutError,
            botocore_exceptions.EndpointConnectionError,
        ),
    ):
        return "timeout"
    if isinstance(
        exc,
        (
            botocore_exceptions.NoCredentialsError,
            botocore_exceptions.PartialCredentialsError,
            botocore_exceptions.NoRegionError,
        ),
    ):
        return "not_configured"
    if isinstance(exc, botocore_exceptions.ClientError):
        response = exc.response if isinstance(exc.response, dict) else {}
        code = (response.get("Error") or {}).get("Code")
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if code in _THROTTLED_CODES or status == 429:
            return "throttled"
        if code in _NOT_CONFIGURED_CODES or status == 403:
            return "not_configured"
        if code == "ValidationException" or status == 400:
            return "invalid_query"
        return "provider_error"
    return "provider_error"


def _build_location_client(*, region: str, timeout_seconds: float) -> Any:
    """A bounded, timeout-guarded boto3 Places v2 client. Lazy import; any failure
    to build one means the backend is not configured (raised without context)."""
    try:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "geo-places",
            region_name=region,
            config=Config(
                connect_timeout=timeout_seconds,
                read_timeout=timeout_seconds,
                retries={"mode": "standard", "max_attempts": 2},
            ),
        )
    except Exception:
        client = None
    if client is None:
        raise GeocodeFailure("not_configured")
    return client


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


class AmazonLocationGeocoder:
    """Amazon Location Service Places v2 ``Geocode``.

    Takes an injected ``client`` (tests) or builds one lazily on the first lookup,
    so constructing it never imports ``boto3`` or resolves credentials.
    """

    provider = "aws_location"
    #: Every request is made with IntendedUse=Storage, so answers may be stored.
    storable = True

    def __init__(
        self,
        *,
        region: str,
        timeout_seconds: float,
        client: Any = None,
        max_results: int = 5,
    ) -> None:
        self._client = client
        self._client_lock = threading.Lock()
        self._region = region
        self._timeout_seconds = timeout_seconds
        self._max_results = max_results

    def _get_client(self) -> Any:
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = _build_location_client(
                        region=self._region, timeout_seconds=self._timeout_seconds
                    )
        return self._client

    def geocode(self, query: str) -> list[GeocodeResult]:
        client = self._get_client()
        try:
            response = client.geocode(
                QueryText=query,
                MaxResults=self._max_results,
                # Results are saved on the location: the provider's terms require
                # the Storage intended use for any stored result.
                IntendedUse="Storage",
                AdditionalFeatures=["TimeZone"],
                Language="en",
            )
        except Exception as exc:
            reason = classify_location_error(exc)
        else:
            items = response.get("ResultItems") if isinstance(response, dict) else None
            return [r for item in items or [] if (r := self._result(item)) is not None]
        # Raised outside the except block so the provider exception (whose message
        # can quote the query) is not attached as context.
        raise GeocodeFailure(reason)

    def _result(self, item: Any) -> GeocodeResult | None:
        if not isinstance(item, dict):
            return None
        position = item.get("Position")
        if not isinstance(position, list) or len(position) != 2:
            return None
        lon, lat = _number(position[0]), _number(position[1])
        if lon is None or lat is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        address = item.get("Address") if isinstance(item.get("Address"), dict) else {}
        country = address.get("Country") if isinstance(address.get("Country"), dict) else {}
        region = address.get("Region") if isinstance(address.get("Region"), dict) else {}
        timezone = item.get("TimeZone") if isinstance(item.get("TimeZone"), dict) else {}
        scores = item.get("MatchScores") if isinstance(item.get("MatchScores"), dict) else {}
        score = _number(scores.get("Overall"))
        return _normalized(
            latitude=lat,
            longitude=lon,
            city=_text(address.get("Locality")),
            # The English name (Language="en") matches how evidence spells a region
            # ("Lagos", not ISO 3166-2 "LA"); known subdivisions map either way.
            region=_text(region.get("Name")) or _text(region.get("Code")),
            country_code=_text(country.get("Code2")),
            timezone=_text(timezone.get("Name")),
            confidence=min(1.0, max(0.0, score)) if score is not None else 0.0,
            label=_text(address.get("Label")) or _text(item.get("Title")),
            provider=self.provider,
            country_fallback=_text(country.get("Name")),
        )


def build_geocoder(settings: Settings) -> Geocoder | None:
    """The configured backend, or ``None`` when no geocoder is configured."""
    backend = settings.effective_geocoder_backend
    if backend == "fixture":
        return FixtureGeocoder(environment=settings.environment)
    if backend == "aws_location" and settings.geocoder_aws_region:
        return AmazonLocationGeocoder(
            region=settings.geocoder_aws_region,
            timeout_seconds=settings.geocoder_timeout_seconds,
        )
    return None


# --- shared policy: validation, limits, cache, choice --------------------------------


def _clean_query(query: str) -> str:
    cleaned = " ".join("".join(ch if ch.isprintable() else " " for ch in query or "").split())
    if len(cleaned) < 2 or len(cleaned) > MAX_QUERY_LENGTH:
        raise GeocodeFailure("invalid_query")
    return cleaned


def choose(results: list[GeocodeResult], *, min_score: float) -> GeocodeResult:
    """The single acceptable answer, or a failure.

    Answers below ``min_score`` are discarded. When the best two remaining answers
    score within ``_AMBIGUITY_MARGIN`` of each other and are different places
    ("London" -> London, England and London, Ontario), the query is ambiguous: the
    customer is asked to qualify it rather than given a silent guess.
    """
    usable = [r for r in results if r.confidence >= min_score]
    if not usable:
        raise GeocodeFailure("no_match")
    usable.sort(key=lambda r: r.confidence, reverse=True)
    best = usable[0]
    for other in usable[1:]:
        if best.confidence - other.confidence > _AMBIGUITY_MARGIN:
            break
        if other.market != best.market:
            raise GeocodeFailure("ambiguous")
    return best


class _WindowLimiter:
    """Fixed one-minute window per key, in process memory (per replica)."""

    def __init__(self, *, limit: int, clock: Callable[[], float], max_keys: int = 10_000):
        self._limit = limit
        self._clock = clock
        self._max_keys = max_keys
        self._windows: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        window = int(self._clock() // 60)
        with self._lock:
            if len(self._windows) >= self._max_keys:
                self._windows = {k: v for k, v in self._windows.items() if v[0] == window}
            start, count = self._windows.get(key, (window, 0))
            if start != window:
                start, count = window, 0
            if count >= self._limit:
                return False
            self._windows[key] = (start, count + 1)
            return True


class GeocodingService:
    """Validation, limits and caching around one :class:`Geocoder` (or none)."""

    def __init__(
        self,
        geocoder: Geocoder | None,
        *,
        cache: Cache,
        cache_ttl_seconds: int,
        min_score: float,
        workspace_requests_per_minute: int,
        provider_burst: int,
        provider_requests_per_second: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.geocoder = geocoder
        self._cache = cache
        self._ttl = cache_ttl_seconds
        self._min_score = min_score
        self._workspace_limit = _WindowLimiter(limit=workspace_requests_per_minute, clock=clock)
        self._provider_bucket = TokenBucket(
            capacity=provider_burst, refill_per_second=provider_requests_per_second, clock=clock
        )
        self._bucket_lock = threading.Lock()

    @property
    def provider(self) -> str:
        return self.geocoder.provider if self.geocoder is not None else "none"

    def geocode(self, query: str, *, organization_id: str, workspace_id: str) -> GeocodeResult:
        cleaned = _clean_query(query)
        if not self._workspace_limit.allow(f"{organization_id}:{workspace_id}"):
            self._record("rate_limited")
            raise GeocodeFailure("rate_limited")
        if self.geocoder is None:
            self._record("not_configured")
            raise GeocodeFailure("not_configured")

        key = self._cache_key(cleaned, organization_id, workspace_id)
        cached = self._cache_get(key)
        if isinstance(cached, dict):
            failure = cached.get("failure")
            if failure in _CACHEABLE_FAILURES:
                self._record(failure, cached=True)
                raise GeocodeFailure(failure)
            try:
                result = GeocodeResult.from_dict(cached)
            except (TypeError, ValueError):
                result = None  # a corrupt or foreign entry is a miss
            if result is not None:
                self._record("resolved", cached=True)
                return result

        with self._bucket_lock:
            admitted = self._provider_bucket.try_acquire()
        if not admitted:
            self._record("throttled")
            raise GeocodeFailure("throttled")
        try:
            result = choose(self.geocoder.geocode(cleaned), min_score=self._min_score)
        except GeocodeFailure as failure:
            if failure.reason in _CACHEABLE_FAILURES:
                self._cache_set(key, {"failure": failure.reason}, _NEGATIVE_TTL_SECONDS)
            self._record(failure.reason)
            raise
        self._cache_set(key, result.to_dict(), self._ttl)
        self._record("resolved")
        return result

    def _cache_key(self, query: str, organization_id: str, workspace_id: str) -> str:
        # The address itself never becomes part of a cache key.
        digest = hashlib.sha256(fold(query).encode()).hexdigest()
        return tenant_cache_key(organization_id, workspace_id, "geocode", self.provider, digest)

    def _cache_get(self, key: str) -> Any:
        if self._ttl <= 0 or not getattr(self.geocoder, "storable", False):
            return MISS
        try:
            return self._cache.get(key, MISS)
        except Exception:  # best-effort: an unavailable cache is a miss
            return MISS

    def _cache_set(self, key: str, value: dict, ttl: int) -> None:
        if self._ttl <= 0 or not getattr(self.geocoder, "storable", False):
            return
        try:
            self._cache.set(key, value, ttl_seconds=min(ttl, self._ttl))
        except Exception:  # best-effort
            pass

    def _record(self, outcome: str, *, cached: bool = False) -> None:
        log_event(
            logger,
            "geocode.lookup",
            outcome=outcome,
            provider=self.provider,
            cached=cached,
        )


def build_geocoding_service(settings: Settings, *, cache: Cache) -> GeocodingService:
    try:
        geocoder = build_geocoder(settings)
    except GeocodeFailure:
        geocoder = None
    return GeocodingService(
        geocoder,
        cache=cache,
        cache_ttl_seconds=settings.geocoder_cache_ttl_seconds,
        min_score=settings.geocoder_min_match_score,
        workspace_requests_per_minute=settings.geocoder_workspace_requests_per_minute,
        provider_burst=settings.geocoder_provider_burst,
        provider_requests_per_second=settings.geocoder_provider_requests_per_second,
    )


@lru_cache
def get_geocoding_service() -> GeocodingService:
    """Process singleton (FastAPI dependency; tests override it)."""
    from app.infra.cache import cache

    return build_geocoding_service(get_settings(), cache=cache)
