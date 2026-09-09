package lambda;

import com.amazonaws.services.lambda.runtime.Context;
import com.amazonaws.services.lambda.runtime.RequestHandler;
import com.amazonaws.services.lambda.runtime.events.APIGatewayProxyRequestEvent;
import com.amazonaws.services.lambda.runtime.events.APIGatewayProxyResponseEvent;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import java.math.BigDecimal;
import java.util.HashMap;
import java.util.Map;
import software.amazon.awssdk.awscore.exception.AwsServiceException;
import software.amazon.awssdk.services.dynamodb.model.AttributeValue;
import software.amazon.awssdk.services.dynamodb.model.ConditionalCheckFailedException;
import software.amazon.awssdk.services.dynamodb.model.DynamoDbException;
import software.amazon.awssdk.services.dynamodb.model.PutItemRequest;
import software.amazon.awssdk.services.sns.model.PublishRequest;

public class AddProduct extends ProductApi implements
    RequestHandler<APIGatewayProxyRequestEvent, APIGatewayProxyResponseEvent> {

  private static final String TABLE_NAME = "Products";

  @Override
  public APIGatewayProxyResponseEvent handleRequest(APIGatewayProxyRequestEvent requestEvent,
      Context context) {
    try {
      Map<String, String> productData = parseProduct(requestEvent.getBody());
      ddb.putItem(createPutItemRequest(productData));
      return response(200, "Product added/updated successfully.");
    } catch (ConditionalCheckFailedException e) {
      return response(409, "Product with the given ID already exists.");
    } catch (DynamoDbException e) {
      context.getLogger().log("DynamoDB error: " + e.getMessage());
      try {
        return publishForRecovery(requestEvent.getBody(), context);
      } catch (Exception recoveryError) {
        context.getLogger().log("Could not enqueue product for recovery: "
            + recoveryError.getMessage());
        return response(503, "DynamoDB is unavailable and the recovery queue is unavailable.");
      }
    } catch (IllegalArgumentException e) {
      return response(400, e.getMessage());
    } catch (JsonProcessingException e) {
      return response(400, "Request body must be a valid JSON object.");
    } catch (AwsServiceException e) {
      context.getLogger().log("AWS service error: " + e.getMessage());
      return response(500, "An AWS service error occurred.");
    } catch (RuntimeException e) {
      context.getLogger().log("Runtime exception: " + e.getMessage());
      return response(500, "Internal server error.");
    } catch (Exception e) {
      context.getLogger().log("Unexpected error: " + e.getMessage());
      return response(500, "Internal server error.");
    }
  }

  private Map<String, String> parseProduct(String body) throws JsonProcessingException {
    if (body == null || body.isBlank()) {
      throw new IllegalArgumentException("Request body is required.");
    }

    JsonNode product = objectMapper.readTree(body);
    if (product == null || !product.isObject()) {
      throw new IllegalArgumentException("Request body must be a JSON object.");
    }

    Map<String, String> productData = new HashMap<>();
    productData.put("id", requiredText(product, "id"));
    productData.put("name", requiredText(product, "name"));
    productData.put("description", requiredText(product, "description"));

    JsonNode price = product.get("price");
    if (price == null || price.isNull() || (!price.isNumber() && !price.isTextual())) {
      throw new IllegalArgumentException("Field 'price' must be a number.");
    }
    try {
      productData.put("price", new BigDecimal(price.asText()).toPlainString());
    } catch (NumberFormatException e) {
      throw new IllegalArgumentException("Field 'price' must be a number.");
    }
    return productData;
  }

  private String requiredText(JsonNode product, String field) {
    JsonNode value = product.get(field);
    if (value == null || value.isNull() || !value.isTextual() || value.asText().isBlank()) {
      throw new IllegalArgumentException(
          "Field '" + field + "' is required and must be a non-empty string.");
    }
    return value.asText();
  }

  private PutItemRequest createPutItemRequest(Map<String, String> productData) {
    HashMap<String, AttributeValue> itemValues = new HashMap<>();
    itemValues.put("id", AttributeValue.builder().s(productData.get("id")).build());
    itemValues.put("name", AttributeValue.builder().s(productData.get("name")).build());
    itemValues.put("price", AttributeValue.builder().n(productData.get("price")).build());
    itemValues.put("description", AttributeValue.builder().s(productData.get("description")).build());

    return PutItemRequest.builder()
        .tableName(TABLE_NAME)
        .item(itemValues)
        .conditionExpression("attribute_not_exists(id) OR id = :id")
        .expressionAttributeValues(
            Map.of(":id", AttributeValue.builder().s(productData.get("id")).build()))
        .build();
  }

  private APIGatewayProxyResponseEvent publishForRecovery(String body, Context context) {
    context.getLogger().log("Sending product to recovery queue: " + body);
    snsClient.publish(PublishRequest.builder().message(body).topicArn(topicArn).build());
    return response(200, "A DynamoDB error occurred. Message sent to queue.");
  }

  private APIGatewayProxyResponseEvent response(int statusCode, String body) {
    return new APIGatewayProxyResponseEvent().withStatusCode(statusCode)
        .withBody(body)
        .withIsBase64Encoded(false)
        .withHeaders(Map.of("Content-Type", "application/json"));
  }
}
