#!/bin/bash
# solutions/dynamodb-outage.sh — configura infraestrutura para o cenário de DynamoDB outage
# Cria SNS Topic, SQS Queue, Lambda de processamento e event source mapping.

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

log "Creating SNS topic 'ProductEventsTopic'..."
SNS_TOPIC_ARN=$($AWS_CLI sns create-topic --name ProductEventsTopic --output json | jq -r '.TopicArn')
log "SNS topic created. ARN: $SNS_TOPIC_ARN"

log "Creating SQS queue 'ProductEventsQueue'..."
QUEUE_URL=$($AWS_CLI sqs create-queue --queue-name ProductEventsQueue --output json | jq -r '.QueueUrl')
QUEUE_ARN=$($AWS_CLI sqs get-queue-attributes \
    --queue-url $QUEUE_URL \
    --attribute-names QueueArn \
    --query 'Attributes.QueueArn' --output text)
log "SQS queue created. ARN: $QUEUE_ARN"

log "Subscribing SQS queue to SNS topic..."
$AWS_CLI sns subscribe \
    --topic-arn $SNS_TOPIC_ARN \
    --protocol sqs \
    --notification-endpoint $QUEUE_ARN >/dev/null
log "SQS queue subscribed to SNS topic."

log "Creating Lambda function 'process-product-events'..."
$AWS_CLI lambda create-function \
  --function-name process-product-events \
  --runtime java17 \
  --handler lambda.DynamoDBWriterLambda::handleRequest \
  --memory-size 1024 \
  --timeout 20 \
  --zip-file fileb://lambda-functions/target/product-lambda.jar \
  --role arn:aws:iam::000000000000:role/productRole \
  --environment 'Variables={AWS_REGION=us-east-1,AWS_ENDPOINT_HOST=ministack,AWS_DYNAMODB_ENDPOINT=http://chaos-bridge:4567/dynamodb}' >/dev/null
log "Lambda function created."

log "Creating event source mapping from SQS to Lambda..."
$AWS_CLI lambda create-event-source-mapping \
    --function-name process-product-events \
    --batch-size 10 \
    --event-source-arn $QUEUE_ARN >/dev/null
log "Event source mapping created."

log "Setting SQS queue visibility timeout..."
$AWS_CLI sqs set-queue-attributes \
    --queue-url $QUEUE_URL \
    --attributes VisibilityTimeout=10 >/dev/null
log "SQS queue attributes set."

echo
echo -e "${BLUE}Setup completed successfully.${NC}"
echo -e "${BLUE}SNS Topic ARN:${NC} $SNS_TOPIC_ARN"
echo -e "${BLUE}SQS Queue ARN:${NC} $QUEUE_ARN"
