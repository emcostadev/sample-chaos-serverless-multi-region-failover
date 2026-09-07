package lambda;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.net.URI;
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.core.client.config.ClientOverrideConfiguration;
import software.amazon.awssdk.core.retry.RetryPolicy;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.dynamodb.DynamoDbClient;
import software.amazon.awssdk.services.sns.SnsClient;

public class ProductApi {

  // AWS_ENDPOINT_HOST é injetado pelo docker-compose.yml (valor: "ministack").
  // Permite que as Lambdas Java construam o endpoint do SDK sem depender de
  // nomes proprietários de ferramentas de emulação.
  protected static final String AWS_ENDPOINT_HOST = System.getenv("AWS_ENDPOINT_HOST");
  protected static final String AWS_DYNAMODB_ENDPOINT = System.getenv("AWS_DYNAMODB_ENDPOINT");
  protected static final String AWS_REGION = System.getenv("AWS_REGION");
  protected static final String topicArn = "arn:aws:sns:us-east-1:000000000000:ProductEventsTopic";
  protected ObjectMapper objectMapper = new ObjectMapper();

  // Política de retry customizada
  RetryPolicy customRetryPolicy = RetryPolicy.builder()
          .numRetries(3)
          .build();

  ClientOverrideConfiguration clientOverrideConfig = ClientOverrideConfiguration.builder()
          .retryPolicy(customRetryPolicy)
          .build();

  protected SnsClient snsClient = SnsClient.builder()
      .endpointOverride(URI.create(String.format("http://%s:4566", AWS_ENDPOINT_HOST)))
      .credentialsProvider(
          StaticCredentialsProvider.create(AwsBasicCredentials.create("test", "test")))
      .region(Region.of(AWS_REGION))
      .build();

  protected DynamoDbClient ddb = DynamoDbClient.builder()
      .endpointOverride(URI.create(
          AWS_DYNAMODB_ENDPOINT != null && !AWS_DYNAMODB_ENDPOINT.isBlank()
              ? AWS_DYNAMODB_ENDPOINT
              : String.format("http://%s:4566", AWS_ENDPOINT_HOST)))
      .credentialsProvider(
          StaticCredentialsProvider.create(AwsBasicCredentials.create("test", "test")))
      .region(Region.of(AWS_REGION))
      .endpointDiscoveryEnabled(true)
      .overrideConfiguration(clientOverrideConfig)
      .build();
}
