import os

import requests


CHAOS_ENDPOINT = os.environ.get("CHAOS_ENDPOINT", "http://localhost:4567")
FAILOVER_URL = f"{CHAOS_ENDPOINT}/failover/product"
FAULTS_URL = f"{CHAOS_ENDPOINT}/_chaos/faults"


def _clear_faults() -> None:
    response = requests.delete(FAULTS_URL, json=[], timeout=10)
    response.raise_for_status()


def test_logical_failover_routes_to_secondary_when_primary_is_unhealthy():
    _clear_faults()
    try:
        # Warm the secondary Lambda before injecting the primary fault.
        warmup = requests.get(
            FAILOVER_URL,
            params={"preferred_region": "us-west-1", "id": "failover-warmup"},
            timeout=120,
        )
        assert warmup.status_code in {200, 404}

        response = requests.post(
            FAULTS_URL,
            json=[
                {"service": "apigateway", "region": "us-east-1"},
                {"service": "lambda", "region": "us-east-1"},
            ],
            timeout=10,
        )
        response.raise_for_status()

        response = requests.get(
            FAILOVER_URL,
            params={"preferred_region": "us-east-1", "id": "failover-warmup"},
            timeout=120,
        )

        assert response.status_code in {200, 404}
        assert response.headers["x-failover-region"] == "us-west-1"
        assert response.headers["x-failover-api-id"]
    finally:
        _clear_faults()


def test_logical_failover_returns_503_when_both_regions_are_unhealthy():
    _clear_faults()
    try:
        response = requests.post(
            FAULTS_URL,
            json=[
                {"service": "apigateway", "region": "us-east-1"},
                {"service": "lambda", "region": "us-east-1"},
                {"service": "apigateway", "region": "us-west-1"},
            ],
            timeout=10,
        )
        response.raise_for_status()

        response = requests.get(
            FAILOVER_URL,
            params={"preferred_region": "us-east-1", "id": "failover-unavailable"},
            timeout=10,
        )

        assert response.status_code == 503
        assert "Nenhuma região saudável" in response.json()["message"]
    finally:
        _clear_faults()
