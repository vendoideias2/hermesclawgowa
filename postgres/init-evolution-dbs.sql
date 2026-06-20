-- Inicializacao dos bancos do Evolution Go. Roda uma unica vez, no primeiro
-- init do volume do Postgres (quando ${POSTGRES_DATA_DIR} esta vazio), via psql
-- (logado no banco POSTGRES_DB=postgres). Padrao alinhado ao init-db.sql oficial
-- do Evolution Go, + extensao 'vector' (pgvector) habilitada por precaucao.

CREATE DATABASE evogo_auth;
CREATE DATABASE evogo_users;

-- Habilita a extensao 'vector' em cada banco (no-op se nao for usada; exige a
-- imagem pgvector/pgvector). \c troca de banco dentro do mesmo run do psql.
\connect evogo_auth
CREATE EXTENSION IF NOT EXISTS vector;

\connect evogo_users
CREATE EXTENSION IF NOT EXISTS vector;
