"""
chaos-bridge — Serviço de injeção de falhas para MiniStack
==========================================================
Expõe a API de chaos engineering usada pelos testes e scripts deste laboratório.

Endpoints principais:
    GET  /_chaos/faults          — lista faults ativos
    POST /_chaos/faults          — injeta faults
    DELETE /_chaos/faults        — remove faults

Endpoints de diagnóstico:
    GET  /_chaos_bridge/health       — status do serviço
    GET  /_chaos_bridge/containers   — lista containers Docker visíveis

Arquitetura:
    Testes / scripts  →  POST /_chaos/faults  →  chaos-bridge
                                                      │
                                  ┌───────────────────┼────────────────────┐
                                  ▼                   ▼                    ▼
                           Docker API          MiniStack API         Estado local
                         (pause/unpause       (force health-       (faults ativos
                          container)           check status)        em memória)

Serviços suportados como injetores de falha:
    - "dynamodb"    → faz o proxy DynamoDB retornar ServiceUnavailable sem
                      interromper os demais serviços do MiniStack
    - "apigateway"  → registra a falha no estado interno + atualiza health check
    - "lambda"      → registra a falha no estado interno + atualiza health check
    - apigateway+lambda combinados → emula falha multi-serviço na região primária
    - endpoint lógico de failover → encaminha productApi para a primeira região saudável

Limitação conhecida do MiniStack:
    O MiniStack não executa health checks de Route53 automaticamente
    (documentado em ministack.org/docs/limitations). Para simular o failover
    DNS, o chaos-bridge força o status via /_ministack/route53/health-checks/{id}/status.
"""

import asyncio
import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import boto3
import docker
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger("chaos-bridge")

MINISTACK_ENDPOINT = os.environ.get("MINISTACK_ENDPOINT", "http://ministack:4566")
MINISTACK_CONTAINER_NAME = os.environ.get("MINISTACK_CONTAINER_NAME", "ministack-aws")
HEALTH_CHECK_POLL_INTERVAL = int(os.environ.get("HEALTH_CHECK_POLL_INTERVAL", "5"))
FAILOVER_CONNECT_TIMEOUT = float(os.environ.get("FAILOVER_CONNECT_TIMEOUT", "3"))
FAILOVER_READ_TIMEOUT = float(os.environ.get("FAILOVER_READ_TIMEOUT", "15"))
FAILOVER_ATTEMPTS_PER_REGION = int(os.environ.get("FAILOVER_ATTEMPTS_PER_REGION", "2"))
RETRYABLE_UPSTREAM_STATUS_CODES = {502, 503, 504}

# ---------------------------------------------------------------------------
# Estado em memória — faults ativos
# ---------------------------------------------------------------------------

_faults_lock = threading.Lock()
_active_faults: list[dict] = []       # [{"service": "dynamodb", "region": "us-east-1"}, …]
_paused_containers: set[str] = set()  # containers atualmente pausados por este bridge
_health_check_thread: threading.Thread | None = None
_health_check_stop = threading.Event()

# ---------------------------------------------------------------------------
# Docker client
# ---------------------------------------------------------------------------

def get_docker_client() -> docker.DockerClient:
    """Retorna cliente Docker. Falha com mensagem clara se o socket não estiver disponível."""
    try:
        client = docker.from_env()
        client.ping()
        return client
    except Exception as exc:
        log.error("Não foi possível conectar ao Docker daemon: %s", exc)
        raise RuntimeError(
            "Docker socket não disponível. Monte /var/run/docker.sock no container chaos-bridge."
        ) from exc


# ---------------------------------------------------------------------------
# AWS clients apontando para MiniStack
# ---------------------------------------------------------------------------

def _boto_kwargs(region: str = "us-east-1") -> dict:
    return dict(
        endpoint_url=MINISTACK_ENDPOINT,
        region_name=region,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def route53_client(region: str = "us-east-1"):
    return boto3.client("route53", **_boto_kwargs(region))


# ---------------------------------------------------------------------------
# Proxy isolado do DynamoDB
# ---------------------------------------------------------------------------

def _signed_request_region(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    match = re.search(r"/(us-[a-z]+-\d)/dynamodb/aws4_request", authorization)
    return match.group(1) if match else None


def _dynamodb_fault_active(region: str | None) -> bool:
    with _faults_lock:
        return any(
            fault.get("service") == "dynamodb"
            and (not fault.get("region") or fault.get("region") == region)
            for fault in _active_faults
        )


async def dynamodb_proxy(request: Request, path: str = "") -> Response:
    """Encaminha chamadas DynamoDB ao MiniStack ou simula indisponibilidade."""
    region = _signed_request_region(request)
    if _dynamodb_fault_active(region):
        return Response(
            content=json.dumps({
                "__type": "com.amazonaws.dynamodb.v20120810#ServiceUnavailableException",
                "message": "DynamoDB fault injected by chaos-bridge",
            }),
            status_code=503,
            media_type="application/x-amz-json-1.0",
        )

    body = await request.body()
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length"}
    }
    upstream_path = f"/{path}" if path else "/"
    try:
        upstream = requests.request(
            method=request.method,
            url=f"{MINISTACK_ENDPOINT}{upstream_path}",
            params=request.query_params,
            headers=headers,
            data=body,
            timeout=30,
        )
    except requests.RequestException as exc:
        log.error("Falha ao encaminhar chamada DynamoDB: %s", exc)
        return Response(
            content=json.dumps({"message": "DynamoDB proxy unavailable"}),
            status_code=503,
            media_type="application/x-amz-json-1.0",
        )

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in {"content-length", "transfer-encoding", "connection"}
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


# ---------------------------------------------------------------------------
# Lógica de pause/unpause de containers
# ---------------------------------------------------------------------------

def _pause_container(container_name: str) -> None:
    """Pausa um container Docker (equivalente a docker pause)."""
    try:
        docker_client = get_docker_client()
        container = docker_client.containers.get(container_name)
        if container.status == "running":
            container.pause()
            _paused_containers.add(container_name)
            log.info("Container '%s' pausado (falha injetada).", container_name)
        else:
            log.warning("Container '%s' não está running (status: %s).", container_name, container.status)
    except docker.errors.NotFound:
        log.error("Container '%s' não encontrado.", container_name)
        raise HTTPException(status_code=404, detail=f"Container '{container_name}' não encontrado.")
    except Exception as exc:
        log.error("Erro ao pausar container '%s': %s", container_name, exc)
        raise HTTPException(status_code=500, detail=str(exc))


def _unpause_container(container_name: str) -> None:
    """Despausa um container Docker."""
    try:
        docker_client = get_docker_client()
        container = docker_client.containers.get(container_name)
        if container.status == "paused":
            container.unpause()
            _paused_containers.discard(container_name)
            log.info("Container '%s' despausado (falha removida).", container_name)
        else:
            log.info("Container '%s' não estava pausado (status: %s).", container_name, container.status)
            _paused_containers.discard(container_name)
    except docker.errors.NotFound:
        log.warning("Container '%s' não encontrado ao tentar desparar.", container_name)
    except Exception as exc:
        log.error("Erro ao desparar container '%s': %s", container_name, exc)


# ---------------------------------------------------------------------------
# Forçar status de health check no MiniStack
# ---------------------------------------------------------------------------

def _force_health_check_status(healthy: bool) -> None:
    """
    Força o status de todos os Route53 health checks para Healthy ou Unhealthy.

    O MiniStack não executa health checks reais por padrão. Para que o failover
    DNS funcione nos testes, percorremos todos os health checks e tentamos forçar
    o status via endpoint interno do MiniStack.
    """
    status = "Healthy" if healthy else "Unhealthy"
    log.info("Forçando status de health checks para: %s", status)

    try:
        r53 = route53_client()
        paginator = r53.get_paginator("list_health_checks")
        health_check_ids = []
        for page in paginator.paginate():
            for hc in page.get("HealthChecks", []):
                health_check_ids.append(hc["Id"])

        if not health_check_ids:
            log.warning("Nenhum health check encontrado no MiniStack.")
            return

        for hc_id in health_check_ids:
            try:
                resp = requests.put(
                    f"{MINISTACK_ENDPOINT}/_ministack/route53/health-checks/{hc_id}/status",
                    json={"status": status},
                    timeout=5,
                )
                if resp.status_code in (200, 204):
                    log.info("Health check %s → %s", hc_id, status)
                    continue
            except Exception:
                pass

            log.warning(
                "Endpoint /_ministack/route53/health-checks/%s/status não disponível. "
                "O failover DNS pode não ocorrer automaticamente.",
                hc_id,
            )

    except Exception as exc:
        log.error("Erro ao manipular health checks: %s", exc)


# ---------------------------------------------------------------------------
# Thread de monitoramento de health checks (polling)
# ---------------------------------------------------------------------------

def _health_check_monitor() -> None:
    """
    Polling que verifica se há faults ativos e mantém os health checks
    do Route53 sincronizados com o estado do chaos-bridge.
    """
    log.info("Health check monitor iniciado.")
    while not _health_check_stop.is_set():
        with _faults_lock:
            has_apigw_lambda_fault = any(
                f.get("service") in ("apigateway", "lambda")
                for f in _active_faults
            )

        if has_apigw_lambda_fault:
            _force_health_check_status(healthy=False)
        else:
            if MINISTACK_CONTAINER_NAME not in _paused_containers:
                _force_health_check_status(healthy=True)

        _health_check_stop.wait(HEALTH_CHECK_POLL_INTERVAL)

    log.info("Health check monitor encerrado.")


def _start_health_monitor() -> None:
    global _health_check_thread, _health_check_stop
    _health_check_stop.clear()
    _health_check_thread = threading.Thread(
        target=_health_check_monitor, daemon=True, name="hc-monitor"
    )
    _health_check_thread.start()


def _stop_health_monitor() -> None:
    global _health_check_thread
    _health_check_stop.set()
    if _health_check_thread:
        _health_check_thread.join(timeout=10)
        _health_check_thread = None


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("chaos-bridge iniciando. MiniStack endpoint: %s", MINISTACK_ENDPOINT)
    log.info("Container alvo: %s", MINISTACK_CONTAINER_NAME)
    _start_health_monitor()
    yield
    log.info("chaos-bridge encerrando.")
    _stop_health_monitor()
    for name in list(_paused_containers):
        _unpause_container(name)


app = FastAPI(
    title="chaos-bridge",
    description="Serviço de injeção de falhas para laboratório de chaos engineering com MiniStack.",
    version="1.0.0",
    lifespan=lifespan,
)
app.mount(
    "/console/icons",
    StaticFiles(directory=Path(__file__).parent / "console" / "icons"),
    name="console-icons",
)
app.add_api_route("/dynamodb", dynamodb_proxy, methods=["POST"])
app.add_api_route("/dynamodb/{path:path}", dynamodb_proxy, methods=["POST"])


@app.get("/console", include_in_schema=False)
async def console() -> FileResponse:
    """Console web local para operar o laboratório sem usar a CLI."""
    return FileResponse(Path(__file__).parent / "console" / "index.html")


def _console_client(service: str, region: str = "us-east-1"):
    return boto3.client(service, **_boto_kwargs(region))


FAILOVER_REGIONS = ("us-east-1", "us-west-1")


def _region_has_gateway_fault(region: str) -> bool:
    """Indica se a região deve ser ignorada pelo endpoint lógico de failover."""
    with _faults_lock:
        return any(
            fault.get("service") in ("apigateway", "lambda")
            and (not fault.get("region") or fault.get("region") == region)
            for fault in _active_faults
        )


def _api_id_for_region(region: str) -> str | None:
    """Obtém o ID interno da productApi usando a tag estável da região."""
    apis = _console_client("apigateway", region).get_rest_apis().get("items", [])
    for api in apis:
        if api.get("tags", {}).get("_custom_id_") in {"12345", "67890"}:
            return api.get("id")
    return None


def _ordered_failover_regions(preferred_region: str | None) -> list[str]:
    """Coloca a região preferida na frente sem permitir regiões arbitrárias."""
    if preferred_region in FAILOVER_REGIONS:
        return [preferred_region, *[r for r in FAILOVER_REGIONS if r != preferred_region]]
    return list(FAILOVER_REGIONS)


def _product_api_url(region: str, api_id: str) -> str:
    return f"{MINISTACK_ENDPOINT}/restapis/{api_id}/dev/_user_request_/productApi"


async def _invoke_product_api(
    body: dict[str, Any],
    *,
    method: str,
    preferred_region: str | None = None,
) -> Response:
    """Encaminha productApi para a primeira região sem fault de API Gateway/Lambda."""
    if method not in {"GET", "POST"}:
        raise HTTPException(status_code=405, detail="Apenas GET e POST são suportados.")

    query = body.get("query", {})
    payload = body.get("payload")
    regions = _ordered_failover_regions(preferred_region)
    skipped_regions: list[str] = []

    for region in regions:
        if _region_has_gateway_fault(region):
            skipped_regions.append(region)
            continue

        try:
            api_id = await asyncio.to_thread(_api_id_for_region, region)
        except Exception as exc:
            log.warning("Não foi possível descobrir a API da região %s: %s", region, exc)
            skipped_regions.append(region)
            continue

        if not api_id:
            log.warning("Nenhuma productApi encontrada na região %s.", region)
            skipped_regions.append(region)
            continue

        upstream = None
        last_region_failure = None
        for attempt in range(1, FAILOVER_ATTEMPTS_PER_REGION + 1):
            try:
                upstream = await asyncio.to_thread(
                    requests.request,
                    method=method,
                    url=_product_api_url(region, api_id),
                    params=query if method == "GET" else None,
                    data=json.dumps(payload) if method == "POST" else None,
                    headers={"Content-Type": "application/json"} if method == "POST" else None,
                    timeout=(FAILOVER_CONNECT_TIMEOUT, FAILOVER_READ_TIMEOUT),
                )
            except requests.RequestException as exc:
                last_region_failure = f"{region} ({type(exc).__name__}, tentativa {attempt})"
                log.warning(
                    "Falha ao encaminhar productApi para %s (tentativa %s/%s): %s",
                    region,
                    attempt,
                    FAILOVER_ATTEMPTS_PER_REGION,
                    exc,
                )
                continue

            if upstream.status_code in RETRYABLE_UPSTREAM_STATUS_CODES:
                last_region_failure = f"{region} (HTTP {upstream.status_code}, tentativa {attempt})"
                log.warning(
                    "Região %s retornou %s ao executar productApi "
                    "(tentativa %s/%s).",
                    region,
                    upstream.status_code,
                    attempt,
                    FAILOVER_ATTEMPTS_PER_REGION,
                )
                continue
            break

        if upstream is None or upstream.status_code in RETRYABLE_UPSTREAM_STATUS_CODES:
            skipped_regions.append(last_region_failure or region)
            continue

        response_headers = {
            "content-type": upstream.headers.get("content-type", "application/json"),
            "x-failover-region": region,
            "x-failover-api-id": api_id,
        }
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=response_headers,
        )

    return JSONResponse(
        status_code=503,
        content={
            "message": "Nenhuma região saudável disponível para productApi.",
            "attempted_regions": regions,
            "skipped_regions": skipped_regions,
        },
    )


@app.get("/console/api/resources")
async def console_resources() -> dict[str, Any]:
    """Retorna um resumo dos recursos provisionados para a console."""
    result: dict[str, Any] = {"regions": {}, "route53": {}, "faults": []}
    for region in ("us-east-1", "us-west-1"):
        apis = _console_client("apigateway", region).get_rest_apis().get("items", [])
        tables = _console_client("dynamodb", region).list_tables().get("TableNames", [])
        functions = _console_client("lambda", region).list_functions().get("Functions", [])
        result["regions"][region] = {
            "apis": [
                {"id": api["id"], "name": api.get("name"), "tags": api.get("tags", {})}
                for api in apis
            ],
            "tables": tables,
            "functions": [fn["FunctionName"] for fn in functions],
        }

    route53 = _console_client("route53")
    result["route53"] = {
        "hosted_zones": route53.list_hosted_zones().get("HostedZones", []),
        "health_checks": route53.list_health_checks().get("HealthChecks", []),
    }
    with _faults_lock:
        result["faults"] = list(_active_faults)
    return result


@app.post("/console/api/invoke")
async def console_invoke(request: Request) -> Response:
    """Invoca a rota productApi usando o ID interno da API selecionada."""
    body = await request.json()
    region = body.get("region", "us-east-1")
    api_id = body.get("api_id")
    method = body.get("method", "GET").upper()
    query = body.get("query", {})
    payload = body.get("payload")
    if not api_id:
        raise HTTPException(status_code=400, detail="api_id é obrigatório.")
    if method not in {"GET", "POST"}:
        raise HTTPException(status_code=400, detail="Apenas GET e POST são suportados.")

    path = f"{MINISTACK_ENDPOINT}/restapis/{api_id}/dev/_user_request_/productApi"
    try:
        upstream = await asyncio.to_thread(
            requests.request,
            method=method,
            url=path,
            params=query if method == "GET" else None,
            data=json.dumps(payload) if method == "POST" else None,
            headers={"Content-Type": "application/json"} if method == "POST" else None,
            timeout=(FAILOVER_CONNECT_TIMEOUT, FAILOVER_READ_TIMEOUT),
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers={"content-type": upstream.headers.get("content-type", "application/json")},
    )


@app.api_route("/failover/product", methods=["GET", "POST"])
async def failover_product(request: Request) -> Response:
    """
    Endpoint lógico de failover para productApi.

    O corpo opcional pode conter ``query``, ``payload`` e ``preferred_region``.
    A região preferida é tentada primeiro; faults de API Gateway/Lambda fazem
    a região ser ignorada e o tráfego seguir para a próxima.
    """
    raw_body = await request.body()
    if raw_body:
        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Body deve ser um JSON válido.") from exc
    else:
        body = {
            "preferred_region": request.query_params.get("preferred_region"),
            "query": {
                key: value
                for key, value in request.query_params.items()
                if key != "preferred_region"
            },
        }

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body deve ser um objeto JSON.")

    return await _invoke_product_api(
        body,
        method=request.method.upper(),
        preferred_region=body.get("preferred_region"),
    )


# ---------------------------------------------------------------------------
# Endpoints da API de chaos
# ---------------------------------------------------------------------------

@app.get("/_chaos/faults")
async def get_faults() -> JSONResponse:
    """Lista todos os faults ativos."""
    with _faults_lock:
        return JSONResponse(content=list(_active_faults))


@app.post("/_chaos/faults")
async def inject_faults(request: Request) -> JSONResponse:
    """
    Injeta faults de serviço.

    Body: [{"service": "dynamodb", "region": "us-east-1"}, ...]

    Comportamento:
    - "dynamodb"    → ativa o fault no proxy isolado do DynamoDB
    - "apigateway"  → força health checks Unhealthy
    - "lambda"      → força health checks Unhealthy
    """
    try:
        body: list[dict[str, Any]] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body deve ser uma lista JSON de faults.")

    if not isinstance(body, list):
        raise HTTPException(status_code=400, detail="Body deve ser uma lista JSON.")

    log.info("Injetando faults: %s", body)

    with _faults_lock:
        existing = {(f["service"], f.get("region", "")) for f in _active_faults}
        for fault in body:
            key = (fault.get("service", ""), fault.get("region", ""))
            if key not in existing:
                _active_faults.append(fault)
                existing.add(key)
        current_faults = list(_active_faults)

    has_dynamodb = any(f.get("service") == "dynamodb" for f in body)
    has_apigw_or_lambda = any(f.get("service") in ("apigateway", "lambda") for f in body)

    if has_dynamodb:
        log.info("Fault DynamoDB → ativando indisponibilidade no proxy DynamoDB")

    if has_apigw_or_lambda:
        log.info("Fault APIGateway/Lambda → forçando health checks Unhealthy")
        threading.Thread(
            target=_force_health_check_status,
            args=(False,),
            daemon=True,
        ).start()

    return JSONResponse(content=current_faults)


@app.delete("/_chaos/faults")
async def clear_faults(request: Request) -> JSONResponse:
    """
    Remove faults.

    Body: [] → remove todos
    Body: [{"service": "dynamodb", ...}] → remove apenas os listados
    """
    try:
        body: list[dict[str, Any]] = await request.json()
    except Exception:
        body = []

    log.info("Removendo faults: %s", body if body else "TODOS")

    with _faults_lock:
        if not body:
            _active_faults.clear()
        else:
            to_remove = {(f.get("service", ""), f.get("region", "")) for f in body}
            _active_faults[:] = [
                f for f in _active_faults
                if (f.get("service", ""), f.get("region", "")) not in to_remove
            ]
        remaining = list(_active_faults)

    with _faults_lock:
        still_has_dynamodb = any(f.get("service") == "dynamodb" for f in remaining)
        still_has_apigw_lambda = any(f.get("service") in ("apigateway", "lambda") for f in remaining)

    if not still_has_dynamodb:
        log.info("Sem faults DynamoDB → proxy DynamoDB liberado")

    if not still_has_apigw_lambda:
        log.info("Sem faults APIGateway/Lambda → forçando health checks Healthy")
        threading.Thread(
            target=_force_health_check_status,
            args=(True,),
            daemon=True,
        ).start()

    return JSONResponse(content=remaining)


# ---------------------------------------------------------------------------
# Diagnóstico
# ---------------------------------------------------------------------------

@app.get("/_chaos_bridge/health")
async def health() -> dict:
    """Health check do chaos-bridge."""
    paused = list(_paused_containers)
    with _faults_lock:
        faults = list(_active_faults)

    ministack_ok = False
    try:
        resp = requests.get(f"{MINISTACK_ENDPOINT}/_ministack/health", timeout=3)
        ministack_ok = resp.status_code == 200
    except Exception:
        pass

    docker_ok = False
    try:
        get_docker_client()
        docker_ok = True
    except Exception:
        pass

    return {
        "status": "ok",
        "ministack_reachable": ministack_ok,
        "docker_reachable": docker_ok,
        "active_faults": faults,
        "paused_containers": paused,
        "ministack_container": MINISTACK_CONTAINER_NAME,
        "ministack_endpoint": MINISTACK_ENDPOINT,
    }


@app.get("/_chaos_bridge/containers")
async def list_containers() -> dict:
    """Lista containers Docker visíveis (diagnóstico)."""
    try:
        docker_client = get_docker_client()
        containers = [
            {"name": c.name, "status": c.status, "id": c.short_id}
            for c in docker_client.containers.list(all=True)
        ]
        return {"containers": containers}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
