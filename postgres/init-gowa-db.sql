-- Inicializacao do banco do GOWA. Roda uma unica vez, no primeiro
-- init do volume do Postgres (quando ${POSTGRES_DATA_DIR} esta vazio).

CREATE DATABASE gowa_db;
