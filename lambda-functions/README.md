# Product Lambda Functions

| Campo            | Valor                                                    |
|------------------|----------------------------------------------------------|
| **Serviços AWS** | API Gateway v1, Lambda (Java 17), DynamoDB, SNS, SQS    |
| **Emulador**     | [MiniStack](https://ministack.org/)                      |
| **Runtime**      | Java 17 (Maven)                                          |

## Descrição

Funções Lambda Java que implementam a API de produtos deste laboratório:

- `add-product` — adiciona ou atualiza um produto na tabela DynamoDB `Products`. Em caso de falha do DynamoDB, publica a mensagem no SNS Topic para processamento assíncrono.
- `get-product` — consulta um produto pelo ID.
- `healthcheck` — retorna HTTP 200 para uso nos health checks do Route53.
- `dynamodb-streams-to-lambda` — replica writes da região primária (us-east-1) para a secundária (us-west-1) via DynamoDB Streams.
- `process-product-events` — consome mensagens da SQS Queue e grava no DynamoDB após recuperação de outage.

## Pré-requisitos

- [Maven 3.8.5+](https://maven.apache.org/install.html)
- [Java 17](https://www.java.com/en/download/help/download_options.html)
- [Docker](https://docs.docker.com/get-docker/)

## Build

Execute na raiz do projeto:

```bash
cd lambda-functions && mvn clean package shade:shade
```

O JAR gerado em `lambda-functions/target/product-lambda.jar` é montado
automaticamente no container MiniStack via volume declarado no `docker-compose.yml`.
