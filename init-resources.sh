#!/bin/sh
# init-resources.sh
# Provisionamento inicial da infraestrutura AWS dentro do MiniStack.
# Executado automaticamente pelo hook init/ready.d ao iniciar o container.

set -e

apk add --no-cache jq

AWS_CLI="aws --endpoint-url ${AWS_ENDPOINT_URL:-http://localhost:4566}"

# ---------------------------------------------------
# Região: us-east-1
# ---------------------------------------------------

echo "Create DynamoDB table (us-east-1)..."
$AWS_CLI dynamodb create-table \
  --table-name Products \
  --attribute-definitions AttributeName=id,AttributeType=S \
  --key-schema AttributeName=id,KeyType=HASH \
  --provisioned-throughput ReadCapacityUnits=5,WriteCapacityUnits=5 \
  --region us-east-1

$AWS_CLI dynamodb update-table \
  --table-name Products \
  --stream-specification StreamEnabled=true,StreamViewType=NEW_AND_OLD_IMAGES \
  --region us-east-1

echo "DynamoDB stream Lambda (us-east-1)..."
$AWS_CLI lambda create-function \
  --function-name dynamodb-streams-to-lambda \
  --runtime java17 \
  --handler dynamodb_streams.DynamoDBStreamHandler::handleRequest \
  --memory-size 256 \
  --timeout 30 \
  --zip-file fileb:///etc/localstack/init/ready.d/target/product-lambda.jar \
  --role arn:aws:iam::000000000000:role/productRole \
  --environment 'Variables={AWS_REGION=us-east-1,AWS_ENDPOINT_HOST=ministack,AWS_DYNAMODB_ENDPOINT=http://chaos-bridge:4567/dynamodb}' \
  --region us-east-1

export STREAM_ARN=$($AWS_CLI dynamodb describe-table --table-name Products --region us-east-1 | jq -r '.Table.LatestStreamArn')
$AWS_CLI lambda create-event-source-mapping \
  --function-name dynamodb-streams-to-lambda \
  --event-source-arn $STREAM_ARN \
  --starting-position LATEST

echo "Add Product Lambda (us-east-1)..."
$AWS_CLI lambda create-function \
  --function-name add-product \
  --runtime java17 \
  --handler lambda.AddProduct::handleRequest \
  --memory-size 512 \
  --timeout 30 \
  --zip-file fileb:///etc/localstack/init/ready.d/target/product-lambda.jar \
  --region us-east-1 \
  --role arn:aws:iam::000000000000:role/productRole \
  --environment 'Variables={AWS_REGION=us-east-1,AWS_ENDPOINT_HOST=ministack,AWS_DYNAMODB_ENDPOINT=http://chaos-bridge:4567/dynamodb}'

echo "Get Product Lambda (us-east-1)..."
$AWS_CLI lambda create-function \
  --function-name get-product \
  --runtime java17 \
  --handler lambda.GetProduct::handleRequest \
  --memory-size 512 \
  --timeout 30 \
  --zip-file fileb:///etc/localstack/init/ready.d/target/product-lambda.jar \
  --region us-east-1 \
  --role arn:aws:iam::000000000000:role/productRole \
  --environment 'Variables={AWS_REGION=us-east-1,AWS_ENDPOINT_HOST=ministack,AWS_DYNAMODB_ENDPOINT=http://chaos-bridge:4567/dynamodb}'

echo "Healthcheck Lambda (us-east-1)..."
$AWS_CLI lambda create-function \
  --function-name healthcheck \
  --runtime python3.11 \
  --handler healthcheck.lambda_handler \
  --memory-size 512 \
  --timeout 30 \
  --zip-file fileb:///etc/localstack/init/ready.d/healthcheck.zip \
  --region us-east-1 \
  --role arn:aws:iam::000000000000:role/productRole

export REST_API_ID=$($AWS_CLI apigateway create-rest-api \
  --name quote-api-gateway \
  --tags '{"_custom_id_":"12345"}' \
  --region us-east-1 | jq -r '.id')

export PARENT_ID=$($AWS_CLI apigateway get-resources --rest-api-id $REST_API_ID --region=us-east-1 | jq -r '.items[0].id')
export RESOURCE_ID=$($AWS_CLI apigateway create-resource --rest-api-id $REST_API_ID --parent-id $PARENT_ID --path-part "productApi" --region=us-east-1 | jq -r '.id')
export HEALTHCHECK_RESOURCE_ID=$($AWS_CLI apigateway create-resource --rest-api-id $REST_API_ID --parent-id $PARENT_ID --path-part "healthcheck" --region=us-east-1 | jq -r '.id')

$AWS_CLI apigateway put-method --rest-api-id $REST_API_ID --resource-id $RESOURCE_ID --http-method GET \
  --request-parameters "method.request.path.productApi=true" --authorization-type "NONE" --region=us-east-1
$AWS_CLI apigateway put-method --rest-api-id $REST_API_ID --resource-id $RESOURCE_ID --http-method POST \
  --request-parameters "method.request.path.productApi=true" --authorization-type "NONE" --region=us-east-1
$AWS_CLI apigateway update-method --rest-api-id $REST_API_ID --resource-id $RESOURCE_ID --http-method GET \
  --patch-operations "op=replace,path=/requestParameters/method.request.querystring.param,value=true" --region=us-east-1

$AWS_CLI apigateway put-integration --rest-api-id $REST_API_ID --resource-id $RESOURCE_ID --http-method POST \
  --type AWS_PROXY --integration-http-method POST \
  --uri arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/arn:aws:lambda:us-east-1:000000000000:function:add-product/invocations \
  --passthrough-behavior WHEN_NO_MATCH --region=us-east-1
$AWS_CLI apigateway put-integration --rest-api-id $REST_API_ID --resource-id $RESOURCE_ID --http-method GET \
  --type AWS_PROXY --integration-http-method GET \
  --uri arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/arn:aws:lambda:us-east-1:000000000000:function:get-product/invocations \
  --passthrough-behavior WHEN_NO_MATCH --region=us-east-1

$AWS_CLI apigateway put-method --rest-api-id $REST_API_ID --resource-id $HEALTHCHECK_RESOURCE_ID --http-method GET \
  --request-parameters "method.request.path.healthcheck=true" --authorization-type "NONE" --region=us-east-1
$AWS_CLI apigateway put-integration --rest-api-id $REST_API_ID --resource-id $HEALTHCHECK_RESOURCE_ID --http-method GET \
  --type AWS_PROXY --integration-http-method POST \
  --uri arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/arn:aws:lambda:us-east-1:000000000000:function:healthcheck/invocations \
  --passthrough-behavior WHEN_NO_MATCH --region=us-east-1

$AWS_CLI apigateway create-deployment --rest-api-id $REST_API_ID --stage-name dev --region=us-east-1

# ---------------------------------------------------
# Região: us-west-1
# ---------------------------------------------------

echo "Create DynamoDB table (us-west-1)..."
$AWS_CLI dynamodb create-table \
  --table-name Products \
  --attribute-definitions AttributeName=id,AttributeType=S \
  --key-schema AttributeName=id,KeyType=HASH \
  --provisioned-throughput ReadCapacityUnits=5,WriteCapacityUnits=5 \
  --region us-west-1

echo "Add Product Lambda (us-west-1)..."
$AWS_CLI lambda create-function \
  --function-name add-product \
  --runtime java17 \
  --handler lambda.AddProduct::handleRequest \
  --memory-size 512 \
  --timeout 30 \
  --zip-file fileb:///etc/localstack/init/ready.d/target/product-lambda.jar \
  --region us-west-1 \
  --role arn:aws:iam::000000000000:role/productRole \
  --environment 'Variables={AWS_REGION=us-west-1,AWS_ENDPOINT_HOST=ministack,AWS_DYNAMODB_ENDPOINT=http://chaos-bridge:4567/dynamodb}'

echo "Get Product Lambda (us-west-1)..."
$AWS_CLI lambda create-function \
  --function-name get-product \
  --runtime java17 \
  --handler lambda.GetProduct::handleRequest \
  --memory-size 512 \
  --timeout 30 \
  --zip-file fileb:///etc/localstack/init/ready.d/target/product-lambda.jar \
  --region us-west-1 \
  --role arn:aws:iam::000000000000:role/productRole \
  --environment 'Variables={AWS_REGION=us-west-1,AWS_ENDPOINT_HOST=ministack,AWS_DYNAMODB_ENDPOINT=http://chaos-bridge:4567/dynamodb}'

echo "Healthcheck Lambda (us-west-1)..."
$AWS_CLI lambda create-function \
  --function-name healthcheck \
  --runtime python3.11 \
  --handler healthcheck.lambda_handler \
  --memory-size 512 \
  --timeout 30 \
  --zip-file fileb:///etc/localstack/init/ready.d/healthcheck.zip \
  --region us-west-1 \
  --role arn:aws:iam::000000000000:role/productRole

export REST_API_ID=$($AWS_CLI apigateway create-rest-api \
  --name quote-api-gateway \
  --tags '{"_custom_id_":"67890"}' \
  --region us-west-1 | jq -r '.id')

export PARENT_ID=$($AWS_CLI apigateway get-resources --rest-api-id $REST_API_ID --region=us-west-1 | jq -r '.items[0].id')
export RESOURCE_ID=$($AWS_CLI apigateway create-resource --rest-api-id $REST_API_ID --parent-id $PARENT_ID --path-part "productApi" --region=us-west-1 | jq -r '.id')
export HEALTHCHECK_RESOURCE_ID=$($AWS_CLI apigateway create-resource --rest-api-id $REST_API_ID --parent-id $PARENT_ID --path-part "healthcheck" --region=us-west-1 | jq -r '.id')

$AWS_CLI apigateway put-method --rest-api-id $REST_API_ID --resource-id $RESOURCE_ID --http-method GET \
  --request-parameters "method.request.path.productApi=true" --authorization-type "NONE" --region=us-west-1
$AWS_CLI apigateway put-method --rest-api-id $REST_API_ID --resource-id $RESOURCE_ID --http-method POST \
  --request-parameters "method.request.path.productApi=true" --authorization-type "NONE" --region=us-west-1
$AWS_CLI apigateway update-method --rest-api-id $REST_API_ID --resource-id $RESOURCE_ID --http-method GET \
  --patch-operations "op=replace,path=/requestParameters/method.request.querystring.param,value=true" --region=us-west-1

$AWS_CLI apigateway put-integration --rest-api-id $REST_API_ID --resource-id $RESOURCE_ID --http-method POST \
  --type AWS_PROXY --integration-http-method POST \
  --uri arn:aws:apigateway:us-west-1:lambda:path/2015-03-31/functions/arn:aws:lambda:us-west-1:000000000000:function:add-product/invocations \
  --passthrough-behavior WHEN_NO_MATCH --region=us-west-1
$AWS_CLI apigateway put-integration --rest-api-id $REST_API_ID --resource-id $RESOURCE_ID --http-method GET \
  --type AWS_PROXY --integration-http-method GET \
  --uri arn:aws:apigateway:us-west-1:lambda:path/2015-03-31/functions/arn:aws:lambda:us-west-1:000000000000:function:get-product/invocations \
  --passthrough-behavior WHEN_NO_MATCH --region=us-west-1

$AWS_CLI apigateway put-method --rest-api-id $REST_API_ID --resource-id $HEALTHCHECK_RESOURCE_ID --http-method GET \
  --request-parameters "method.request.path.healthcheck=true" --authorization-type "NONE" --region=us-west-1
$AWS_CLI apigateway put-integration --rest-api-id $REST_API_ID --resource-id $HEALTHCHECK_RESOURCE_ID --http-method GET \
  --type AWS_PROXY --integration-http-method POST \
  --uri arn:aws:apigateway:us-west-1:lambda:path/2015-03-31/functions/arn:aws:lambda:us-west-1:000000000000:function:healthcheck/invocations \
  --passthrough-behavior WHEN_NO_MATCH --region=us-west-1

$AWS_CLI apigateway create-deployment --rest-api-id $REST_API_ID --stage-name dev --region=us-west-1
