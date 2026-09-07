#!/bin/bash
# solutions/route53-failover.sh — configura Route53 failover multi-região

set -e
set -o pipefail

AWS_ENDPOINT_URL=${AWS_ENDPOINT_URL:-"http://localhost:4566"}
AWS_CLI="aws --endpoint-url $AWS_ENDPOINT_URL"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

log() { echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1" >&2; }
error_log() { echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')] ERROR:${NC} $1" >&2; }
trap 'error_log "An error occurred. Exiting..."; exit 1' ERR

# Passo 1: Criar Hosted Zone
log "Criando hosted zone..."
HOSTED_ZONE_NAME="hello-ministack.local"
RAW_HOSTED_ZONE_ID=$($AWS_CLI route53 create-hosted-zone \
    --name "$HOSTED_ZONE_NAME" \
    --caller-reference "zone-$(date +%s)" | jq -r .HostedZone.Id)
CLEANED_HOSTED_ZONE_ID="${RAW_HOSTED_ZONE_ID#/hostedzone/}"

log "Hosted Zone: $HOSTED_ZONE_NAME  ID: $RAW_HOSTED_ZONE_ID"
export HOSTED_ZONE_NAME RAW_HOSTED_ZONE_ID

# Passo 2: Parâmetros da API Gateway e Health Check
PRIMARY_API_ID="12345"
SECONDARY_API_ID="67890"
PRIMARY_API_REGION="us-east-1"
HEALTH_CHECK_RESOURCE_PATH="/dev/healthcheck"

# FQDNs registrados no health check — usados como identificadores de endpoint.
# O chaos-bridge força o status do HC sem depender de resolução DNS real.
PRIMARY_API_GATEWAY_FQDN="${PRIMARY_API_ID}.execute-api.ministack.local"
HEALTH_CHECK_PORT=4566

log "Primary API FQDN: $PRIMARY_API_GATEWAY_FQDN  Port: $HEALTH_CHECK_PORT"

# Passo 3: Criar Health Check
log "Criando Route53 health check..."
HEALTH_CHECK_RESOURCE_REGION="us-west-1"
HEALTH_CHECK_ID=$($AWS_CLI route53 create-health-check \
    --caller-reference "hc-app-${PRIMARY_API_ID}-$(date +%s)" \
    --region "$HEALTH_CHECK_RESOURCE_REGION" \
    --health-check-config "{\"FullyQualifiedDomainName\": \"${PRIMARY_API_GATEWAY_FQDN}\", \"Port\": ${HEALTH_CHECK_PORT}, \"ResourcePath\": \"${HEALTH_CHECK_RESOURCE_PATH}\", \"Type\": \"HTTP\", \"RequestInterval\": 10, \"FailureThreshold\": 2}" | jq -r .HealthCheck.Id)

log "Health check criado: $HEALTH_CHECK_ID (região: $HEALTH_CHECK_RESOURCE_REGION)"
export HEALTH_CHECK_ID
# Sleep curto — o MiniStack não executa HCs reais; o chaos-bridge força o status
sleep 5

# Passo 4: Verificar status inicial
$AWS_CLI route53 get-health-check-status \
    --health-check-id "$HEALTH_CHECK_ID" \
    --region "$HEALTH_CHECK_RESOURCE_REGION" >/dev/null 2>&1 || \
    log "Status do HC não disponível ainda (normal no MiniStack)."

# Passo 5: Criar CNAME Records regionais
log "Criando CNAME records regionais..."
PRIMARY_REGIONAL_DNS_NAME="${PRIMARY_API_ID}.${HOSTED_ZONE_NAME}"
SECONDARY_REGIONAL_DNS_NAME="${SECONDARY_API_ID}.${HOSTED_ZONE_NAME}"
PRIMARY_API_TARGET_FQDN="${PRIMARY_API_ID}.execute-api.ministack.local"
SECONDARY_API_TARGET_FQDN="${SECONDARY_API_ID}.execute-api.ministack.local"

CHANGE_BATCH_REGIONAL_CNAMES_JSON=$(cat <<EOF
{
  "Comment": "Creating CNAMEs for regional API endpoints",
  "Changes": [
    {
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "$PRIMARY_REGIONAL_DNS_NAME",
        "Type": "CNAME",
        "TTL": 60,
        "ResourceRecords": [{ "Value": "$PRIMARY_API_TARGET_FQDN" }]
      }
    },
    {
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "$SECONDARY_REGIONAL_DNS_NAME",
        "Type": "CNAME",
        "TTL": 60,
        "ResourceRecords": [{ "Value": "$SECONDARY_API_TARGET_FQDN" }]
      }
    }
  ]
}
EOF
)

$AWS_CLI route53 change-resource-record-sets \
    --hosted-zone-id "$RAW_HOSTED_ZONE_ID" \
    --change-batch "$CHANGE_BATCH_REGIONAL_CNAMES_JSON" >/dev/null
log "CNAME records criados."

# Passo 6: Criar Failover Alias Records
log "Criando failover alias records..."
FAILOVER_RECORD_NAME="test.${HOSTED_ZONE_NAME}"
PRIMARY_FAILOVER_SET_ID="primary-app-${PRIMARY_API_ID}"
SECONDARY_FAILOVER_SET_ID="secondary-app-${SECONDARY_API_ID}"

CHANGE_BATCH_FAILOVER_ALIASES_JSON=$(cat <<EOF
{
  "Comment": "Creating failover alias records for $FAILOVER_RECORD_NAME",
  "Changes": [
    {
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "$FAILOVER_RECORD_NAME",
        "Type": "CNAME",
        "SetIdentifier": "$PRIMARY_FAILOVER_SET_ID",
        "Failover": "PRIMARY",
        "HealthCheckId": "$HEALTH_CHECK_ID",
        "AliasTarget": {
          "HostedZoneId": "$RAW_HOSTED_ZONE_ID",
          "DNSName": "$PRIMARY_REGIONAL_DNS_NAME",
          "EvaluateTargetHealth": true
        }
      }
    },
    {
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "$FAILOVER_RECORD_NAME",
        "Type": "CNAME",
        "SetIdentifier": "$SECONDARY_FAILOVER_SET_ID",
        "Failover": "SECONDARY",
        "AliasTarget": {
          "HostedZoneId": "$RAW_HOSTED_ZONE_ID",
          "DNSName": "$SECONDARY_REGIONAL_DNS_NAME",
          "EvaluateTargetHealth": false
        }
      }
    }
  ]
}
EOF
)

$AWS_CLI route53 change-resource-record-sets \
    --hosted-zone-id "$RAW_HOSTED_ZONE_ID" \
    --change-batch "$CHANGE_BATCH_FAILOVER_ALIASES_JSON" >/dev/null
log "Failover alias records criados."

echo
echo -e "${BLUE}Route 53 failover configurado com sucesso.${NC}"
echo -e "${BLUE}Hosted Zone:${NC} $HOSTED_ZONE_NAME"
echo -e "${BLUE}Primary FQDN:${NC} $PRIMARY_API_GATEWAY_FQDN"
echo -e "${BLUE}Health Check ID:${NC} $HEALTH_CHECK_ID"
echo -e "${BLUE}Failover Domain:${NC} $FAILOVER_RECORD_NAME"
echo
echo -e "${BLUE}Para verificar o estado do failover:${NC}"
echo "  aws --endpoint-url http://localhost:4566 route53 list-resource-record-sets --hosted-zone-id $CLEANED_HOSTED_ZONE_ID"
