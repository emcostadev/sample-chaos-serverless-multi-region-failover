export AWS_ACCESS_KEY_ID ?= test
export AWS_SECRET_ACCESS_KEY ?= test
export AWS_DEFAULT_REGION=us-east-1
export AWS_ENDPOINT_URL ?= http://localhost:4566
export CHAOS_ENDPOINT ?= http://localhost:4567
SHELL := /bin/bash

usage:			## Exibe esta tabela de ajuda
	@echo "| Target                 | Description                                                       |"
	@echo "|------------------------|-------------------------------------------------------------------|"
	@fgrep -h "##" $(MAKEFILE_LIST) | fgrep -v fgrep | sed -e 's/:.*##\s*/##/g' | awk -F'##' '{ printf "| %-22s | %-65s |\n", $$1, $$2 }'


check:			## Verifica pré-requisitos (docker, mvn, java, aws, python3)
	@command -v docker > /dev/null 2>&1 || { echo "Docker não instalado."; exit 1; }
	@command -v mvn > /dev/null 2>&1 || { echo "Maven não instalado."; exit 1; }
	@command -v java > /dev/null 2>&1 || { echo "Java não instalado."; exit 1; }
	@command -v aws > /dev/null 2>&1 || { echo "AWS CLI não instalado."; exit 1; }
	@command -v python3 > /dev/null 2>&1 || { echo "Python 3 não instalado."; exit 1; }
	@echo "Todos os pré-requisitos estão disponíveis."

install:		## Instala todas as dependências (mvn build + pip)
	@echo "Instalando dependências..."
	cd lambda-functions && mvn clean package shade:shade;
	cd tests && pip3 install -r requirements-dev.txt;
	@echo "Dependências instaladas com sucesso."

test:			## Executa todos os testes (pytest)
	@echo "Executando testes..."
	python3 -m pytest tests/ -v
	@echo "Testes concluídos."

deploy:			## Provisiona recursos de chaos engineering (SNS/SQS + Route53 failover)
	@echo "Provisionando soluções de chaos..."
	./solutions/dynamodb-outage.sh
	./solutions/route53-failover.sh
	@echo "Soluções provisionadas com sucesso."

start:			## Sobe MiniStack + chaos-bridge
	docker compose up --build --detach --wait

stop:			## Para todos os containers
	docker compose down

logs:			## Salva logs em logs.txt
	docker compose logs > logs.txt

ready:			## Aguarda MiniStack e chaos-bridge estarem prontos
	@echo "Aguardando MiniStack ficar pronto..."
	@while [[ "$$(curl -s http://localhost:4566/_localstack/health | python3 -c 'import sys,json; d=json.load(sys.stdin); r=d.get("ready_scripts", {}); print("ok" if r.get("status") == "completed" and r.get("failed", 0) == 0 else "nok")' 2>/dev/null)" != "ok" ]]; do \
		echo "MiniStack não está pronto, aguardando..."; \
		sleep 3; \
	done
	@echo "MiniStack pronto!"
	@echo "Aguardando chaos-bridge ficar pronto..."
	@while [[ "$$(curl -s http://localhost:4567/_chaos_bridge/health | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("status","nok"))' 2>/dev/null)" != "ok" ]]; do \
		echo "chaos-bridge não está pronto, aguardando..."; \
		sleep 3; \
	done
	@echo "chaos-bridge pronto!"

chaos-status:		## Exibe faults de chaos ativos
	@echo "Faults ativos:"
	@curl -s http://localhost:4567/_chaos/faults | python3 -m json.tool

chaos-inject:		## Injeta falha (make chaos-inject SERVICE=dynamodb REGION=us-east-1)
	@curl -s -X POST http://localhost:4567/_chaos/faults \
		-H 'Content-Type: application/json' \
		-d '[{"service": "$(or $(SERVICE),dynamodb)", "region": "$(or $(REGION),us-east-1)"}]' | python3 -m json.tool

chaos-clear:		## Remove todos os faults ativos
	@curl -s -X DELETE http://localhost:4567/_chaos/faults \
		-H 'Content-Type: application/json' \
		-d '[]' | python3 -m json.tool

.PHONY: usage check install start ready deploy test logs stop chaos-status chaos-inject chaos-clear
