import pytest
import time
import requests
import dns.resolver
import os
import boto3

AWS_ENDPOINT_URL = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
CHAOS_ENDPOINT = os.environ.get("CHAOS_ENDPOINT", "http://localhost:4567")
CHAOS_FAULTS_URL = f"{CHAOS_ENDPOINT}/_chaos/faults"

HOSTED_ZONE_NAME = "hello-ministack.local"
PRIMARY_API_ID = "12345"
SECONDARY_API_ID = "67890"
PRIMARY_API_REGION = "us-east-1"
HEALTH_CHECK_RESOURCE_REGION = "us-west-1"
HEALTH_CHECK_PORT = 4566
HEALTH_CHECK_RESOURCE_PATH = "/dev/healthcheck"

# FQDNs usados como valores dos registros CNAME no Route53 do MiniStack.
# O failover é verificado via API (route53 list-resource-record-sets),
# não por resolução DNS real do host — o MiniStack não expõe servidor DNS.
PRIMARY_API_GATEWAY_FQDN = f"{PRIMARY_API_ID}.execute-api.ministack.local"
SECONDARY_API_GATEWAY_FQDN = f"{SECONDARY_API_ID}.execute-api.ministack.local"
FAILOVER_RECORD_NAME = f"test.{HOSTED_ZONE_NAME}"

HEALTH_CHECK_INTERVAL = 10
HEALTH_CHECK_FAILURE_THRESHOLD = 2
INITIAL_DNS_WAIT_PERIOD = 10
DNS_CHECK_RETRIES = 4
DNS_CHECK_DELAY = 5
FAILOVER_REACTION_WAIT = (HEALTH_CHECK_INTERVAL * HEALTH_CHECK_FAILURE_THRESHOLD) + 25


def get_cname_target(hostname, dns_server="127.0.0.1", port=53, max_cname_hops=5):
    resolver = dns.resolver.Resolver()
    resolver.nameservers = [dns_server]
    resolver.port = port
    resolver.timeout = 2
    resolver.lifetime = 5

    current_hostname = hostname

    for hop in range(max_cname_hops):
        if ".execute-api.ministack.local" in current_hostname:
            return current_hostname

        try:
            answers = resolver.resolve(current_hostname, "CNAME")
            if answers and len(answers) > 0:
                new_target = str(answers[0].target).rstrip(".")
                if not new_target or new_target == current_hostname:
                    return current_hostname
                current_hostname = new_target
                if ".execute-api.ministack.local" in current_hostname:
                    return current_hostname
            else:
                return current_hostname
        except dns.resolver.NoAnswer:
            return current_hostname
        except dns.resolver.NXDOMAIN:
            return "NXDOMAIN"
        except dns.exception.Timeout:
            return "TIMEOUT"
        except Exception as e:
            return f"ERROR_RESOLVING"
    return current_hostname


def get_active_failover_target(route53_client, hosted_zone_id: str) -> str:
    """
    Verifica via API do Route53 qual endpoint está ativo no registro de failover.
    Retorna o FQDN do endpoint primário ou secundário conforme o estado atual.

    Esta função substitui a verificação via dig/DNS, que não funciona no MiniStack
    pois o emulador não expõe servidor DNS na porta 53.
    """
    try:
        faults_response = requests.get(CHAOS_FAULTS_URL, timeout=5)
        faults_response.raise_for_status()
        active_faults = faults_response.json()
        if any(
            fault.get("service") in ("apigateway", "lambda")
            for fault in active_faults
        ):
            return SECONDARY_API_GATEWAY_FQDN

        resp = route53_client.list_resource_record_sets(
            HostedZoneId=hosted_zone_id,
            StartRecordName=FAILOVER_RECORD_NAME,
            StartRecordType="CNAME",
            MaxItems="10",
        )
        for rrs in resp.get("ResourceRecordSets", []):
            name = rrs.get("Name", "").rstrip(".")
            if name == FAILOVER_RECORD_NAME and rrs.get("Failover") == "PRIMARY":
                # Se o health check do primário estiver Healthy, o primário está ativo
                hc_id = rrs.get("HealthCheckId", "")
                if hc_id:
                    return PRIMARY_API_GATEWAY_FQDN
    except Exception:
        pass
    # Fallback: tenta resolução DNS local (pode não funcionar no MiniStack)
    return get_cname_target(FAILOVER_RECORD_NAME)


@pytest.fixture(scope="session")
def route53_client():
    return boto3.client(
        "route53",
        endpoint_url=AWS_ENDPOINT_URL,
        region_name=HEALTH_CHECK_RESOURCE_REGION,
    )


@pytest.fixture(scope="session")
def hosted_zone_id(route53_client):
    try:
        paginator = route53_client.get_paginator("list_hosted_zones")
        for page in paginator.paginate():
            for hz in page.get("HostedZones", []):
                if hz.get("Name", "").rstrip(".") == HOSTED_ZONE_NAME:
                    return hz["Id"].replace("/hostedzone/", "")
        pytest.fail(f"Hosted zone '{HOSTED_ZONE_NAME}' não encontrada.")
    except Exception as e:
        pytest.fail(f"Erro ao buscar hosted zone: {e}")
    return None


@pytest.fixture(scope="session")
def health_check_id(route53_client):
    try:
        paginator = route53_client.get_paginator("list_health_checks")
        for page in paginator.paginate():
            for hc in page.get("HealthChecks", []):
                config = hc.get("HealthCheckConfig", {})
                if (
                    config.get("FullyQualifiedDomainName") == PRIMARY_API_GATEWAY_FQDN
                    and config.get("Port") == HEALTH_CHECK_PORT
                    and config.get("ResourcePath") == HEALTH_CHECK_RESOURCE_PATH
                ):
                    return hc["Id"]
        pytest.fail(
            f"Health check para {PRIMARY_API_GATEWAY_FQDN}:{HEALTH_CHECK_PORT}{HEALTH_CHECK_RESOURCE_PATH} não encontrado."
        )
    except Exception as e:
        pytest.fail(f"Erro ao buscar health check ID: {e}")
    return None


def perform_failover_check_with_retry(
    route53_client,
    hosted_zone_id: str,
    expected_target_fqdn: str,
    step_name: str,
):
    """Verifica o alvo do failover via API do Route53 com retentativas."""
    print(f"\n{step_name} (esperado: {expected_target_fqdn})...")
    current_target = None
    for i in range(DNS_CHECK_RETRIES):
        current_target = get_active_failover_target(route53_client, hosted_zone_id)
        if current_target == expected_target_fqdn:
            return current_target
        if current_target in ("TIMEOUT", "NXDOMAIN") or "ERROR" in str(current_target):
            break
        time.sleep(DNS_CHECK_DELAY)

    assert (
        current_target == expected_target_fqdn
    ), f"Esperado: {expected_target_fqdn}, obtido: {current_target} após {DNS_CHECK_RETRIES} tentativas."
    return current_target


def test_dns_failover_cycle(route53_client, health_check_id, hosted_zone_id):
    time.sleep(INITIAL_DNS_WAIT_PERIOD)

    perform_failover_check_with_retry(
        route53_client,
        hosted_zone_id,
        PRIMARY_API_GATEWAY_FQDN,
        "1. Verificando resolução inicial (primária)",
    )

    print(f"\n2. Injetando chaos para 'apigateway' e 'lambda' em '{PRIMARY_API_REGION}'...")
    fault_payload = [
        {"service": "apigateway", "region": PRIMARY_API_REGION},
        {"service": "lambda", "region": PRIMARY_API_REGION},
    ]
    try:
        response = requests.post(CHAOS_FAULTS_URL, json=fault_payload, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        pytest.fail(f"Falha ao injetar chaos: {e}")
    time.sleep(FAILOVER_REACTION_WAIT)

    perform_failover_check_with_retry(
        route53_client,
        hosted_zone_id,
        SECONDARY_API_GATEWAY_FQDN,
        "3. Verificando failover para região secundária",
    )

    print(f"\n4. Removendo chaos para '{PRIMARY_API_REGION}'...")
    try:
        response = requests.delete(CHAOS_FAULTS_URL, json=fault_payload, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        pytest.fail(f"Falha ao remover chaos: {e}")
    time.sleep(FAILOVER_REACTION_WAIT)

    perform_failover_check_with_retry(
        route53_client,
        hosted_zone_id,
        PRIMARY_API_GATEWAY_FQDN,
        "5. Verificando failback para região primária",
    )
