-- Base de logs do robô de requisições (requisicao/main.py).
-- Schema próprio no mesmo Postgres de apontamento_v2.
--
--   execucao 1 ── N tentativa 1 ── N evento
--   (cada start     (cada registro      (cada linha de log;
--    do robô)        processado)          tentativa_id nulo = evento
--                                          da execução, ex.: login)
--
-- Idempotente: pode rodar de novo sem apagar nada.

CREATE SCHEMA IF NOT EXISTS log_requisicao;

CREATE TABLE IF NOT EXISTS log_requisicao.execucao (
    id              bigserial PRIMARY KEY,
    maquina         varchar(100) NOT NULL,
    pid             integer      NOT NULL,
    versao          varchar(40),                       -- commit do git em execução
    iniciado_em     timestamptz  NOT NULL DEFAULT now(),
    finalizado_em   timestamptz,
    status          varchar(20)  NOT NULL DEFAULT 'RODANDO',
                    -- RODANDO | ENCERRADO | INTERROMPIDO | PARADO_ERRO
    motivo_fim      text
);

CREATE TABLE IF NOT EXISTS log_requisicao.tentativa (
    id              bigserial PRIMARY KEY,
    execucao_id     bigint       NOT NULL REFERENCES log_requisicao.execucao(id),
    solicitacao_id  integer      NOT NULL,             -- apontamento_v2.solicitacao_almox_solicitacaorequisicao.id
    item            varchar(50),
    quantidade      numeric,
    classe          varchar(100),
    matricula       varchar(50),
    cc              varchar(100),
    chave_erp       varchar(255),                      -- chave capturada no Insert
    iniciado_em     timestamptz  NOT NULL DEFAULT now(),
    finalizado_em   timestamptz,
    duracao_ms      integer,
    resultado       varchar(20),                       -- OK | ERRO | VERIFICAR (nulo = em andamento/abortada)
    etapa_falha     varchar(50),                       -- preparar | insert | preencher | ... | gravar | pos_gravar
    mensagem_erro   text,
    texto_modal     text,                              -- texto do modal de resultado após 'Gravar'
    screenshot      text
);

CREATE TABLE IF NOT EXISTS log_requisicao.evento (
    id              bigserial PRIMARY KEY,
    execucao_id     bigint       NOT NULL REFERENCES log_requisicao.execucao(id),
    tentativa_id    bigint       REFERENCES log_requisicao.tentativa(id),
    criado_em       timestamptz  NOT NULL DEFAULT now(),
    nivel           varchar(10)  NOT NULL,             -- INFO | WARNING | ERROR | CRITICAL
    mensagem        text         NOT NULL,
    detalhe         text                               -- traceback, quando houver
);

CREATE INDEX IF NOT EXISTS ix_tentativa_solicitacao ON log_requisicao.tentativa (solicitacao_id);
CREATE INDEX IF NOT EXISTS ix_tentativa_resultado   ON log_requisicao.tentativa (resultado, iniciado_em);
CREATE INDEX IF NOT EXISTS ix_tentativa_execucao    ON log_requisicao.tentativa (execucao_id);
CREATE INDEX IF NOT EXISTS ix_evento_execucao       ON log_requisicao.evento (execucao_id, criado_em);
CREATE INDEX IF NOT EXISTS ix_evento_tentativa      ON log_requisicao.evento (tentativa_id);
CREATE INDEX IF NOT EXISTS ix_evento_criado_em      ON log_requisicao.evento (criado_em);

-- Consultas úteis --------------------------------------------------------
-- Suspeitas de duplicata (mais de uma chave gravada/possivelmente gravada):
--   SELECT solicitacao_id, array_agg(chave_erp ORDER BY iniciado_em) AS chaves, count(*)
--   FROM log_requisicao.tentativa
--   WHERE resultado IN ('OK', 'VERIFICAR')
--   GROUP BY solicitacao_id HAVING count(*) > 1;
--
-- Histórico de um registro:
--   SELECT * FROM log_requisicao.tentativa WHERE solicitacao_id = 13843 ORDER BY iniciado_em;
--   SELECT e.* FROM log_requisicao.evento e JOIN log_requisicao.tentativa t ON t.id = e.tentativa_id
--   WHERE t.solicitacao_id = 13843 ORDER BY e.criado_em;
--
-- Erros por dia e etapa:
--   SELECT iniciado_em::date, etapa_falha, resultado, count(*) FROM log_requisicao.tentativa
--   WHERE resultado <> 'OK' GROUP BY 1, 2, 3 ORDER BY 1 DESC;
