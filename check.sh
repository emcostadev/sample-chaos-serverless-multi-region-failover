#!/bin/bash
# check.sh — script de verificação manual do laboratório de chaos engineering

set -e
set -o pipefail

AWS_ENDPOINT_URL=${AWS_ENDPOINT_URL:-"http://localhost:4566"}
CHAOS_ENDPOINT=${CHAOS_ENDPOINT:-"http://localhost:4567"}
AWS_CLI="aws --endpoint-url $AWS_ENDPOINT_URL"

PRIMARY_API_ID="12345"
SECONDARY_API_ID="67890"
PRIMARY_API_REGION="us-east-1"
HEALTH_CHECK_RESOURCE_REGION="us-west-1"
HOSTED_ZONE_NAME="hello-ministack.local"
FAILOVER_RECORD_NAME="test.${HOSTED_ZONE_NAME}"

# API Gateway path-style (MiniStack)
PRIMARY_API_URL="http://localhost:4566/restapis/${PRIMARY_API_ID}/dev/_user_request_"

echo "--- Step 1: Verifica recursos implantados ---"
echo "Verificando tabela DynamoDB na região primária (us-east-1)..."
$AWS_CLI dynamodb describe-table --table-name Products --region us-east-1 --query 'Table.TableStatus' --output text
echo

echo "--- Step 2: Testa endpoint da API Gateway primária ---"
echo "Chamando ${PRIMARY_API_URL}/productApi ..."
curl --silent --show-error --connect-timeout 10 \
  -X POST "${PRIMARY_API_URL}/productApi" \
  -H 'Content-Type: application/json' \
  -d '{"id": "check-prod-1", "name": "Check Product", "price": "9.99", "description": "Teste de verificação"}' \
  && echo
echo

echo "--- Step 3: Verifica registro DNS de failover via Route53 API ---"
HOSTED_ZONE_ID=$($AWS_CLI route53 list-hosted-zones \
    --query "HostedZones[?Name=='${HOSTED_ZONE_NAME}.'].Id" \
    --output text | sed 's|/hostedzone/||')
echo "Hosted Zone ID: $HOSTED_ZONE_ID"

echo "Record sets de failover para ${FAILOVER_RECORD_NAME}:"
$AWS_CLI route53 list-resource-record-sets \
    --hosted-zone-id "$HOSTED_ZONE_ID" \
    --query "ResourceRecordSets[?Name=='${FAILOVER_RECORD_NAME}.']" \
    --output json
echo

echo "--- Step 4: Obtém ID do health check ---"
HEALTH_CHECK_ID=$($AWS_CLI route53 list-health-checks \
    --query "HealthChecks[?HealthCheckConfig.FullyQualifiedDomainName=='${PRIMARY_API_ID}.execute-api.ministack.local'].Id" \
    --output text --region "$HEALTH_CHECK_RESOURCE_REGION")
echo "Health Check ID: $HEALTH_CHECK_ID"
export HEALTH_CHECK_ID
echo

echo "--- Step 5: Simula falha de APIGateway + Lambda na região primária ---"
echo "Injetando faults via chaos-bridge em ${CHAOS_ENDPOINT}..."
curl -s -X POST "${CHAOS_ENDPOINT}/_chaos/faults" \
    -H 'Content-Type: application/json' \
    -d "[
        {\"service\": \"apigateway\", \"region\": \"${PRIMARY_API_REGION}\"},
        {\"service\": \"lambda\", \"region\": \"${PRIMARY_API_REGION}\"}
    ]" | python3 -m json.tool
echo

echo "Aguardando detecção de falha pelo Route53 (~35 segundos)..."
sleep 35

echo "--- Step 6: Verifica DNS de failover após injeção ---"
echo "Record sets após falha (deve indicar SECONDARY como ativo):"
$AWS_CLI route53 list-resource-record-sets \
    --hosted-zone-id "$HOSTED_ZONE_ID" \
    --query "ResourceRecordSets[?Name=='${FAILOVER_RECORD_NAME}.']" \
    --output json
echo

echo "--- Step 7: Status do health check ---"
$AWS_CLI route53 get-health-check-status \
    --health-check-id "$HEALTH_CHECK_ID" \
    --region "$HEALTH_CHECK_RESOURCE_REGION" 2>/dev/null || \
    echo "Status via chaos-bridge: $(curl -s ${CHAOS_ENDPOINT}/_chaos/faults)"
echo

echo "--- Step 8: Remove faults (simula recuperação da região primária) ---"
curl -s -X DELETE "${CHAOS_ENDPOINT}/_chaos/faults" \
    -H 'Content-Type: application/json' \
    -d '[]' | python3 -m json.tool
echo

echo "Aguardando recuperação (~35 segundos)..."
sleep 35

echo "--- Step 9: Verifica failback para a região primária ---"
echo "Record sets após recuperação (deve indicar PRIMARY como ativo):"
$AWS_CLI route53 list-resource-record-sets \
    --hosted-zone-id "$HOSTED_ZONE_ID" \
    --query "ResourceRecordSets[?Name=='${FAILOVER_RECORD_NAME}.']" \
    --output json
echo

echo "Script de verificação finalizado."
