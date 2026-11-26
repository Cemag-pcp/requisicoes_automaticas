"""
Gravação dos logs do robô de requisições no Postgres (schema
`log_requisicao` — estrutura em sql/log_requisicao.sql).

    execucao  -> uma linha por start do robô
    tentativa -> uma linha por registro processado
    evento    -> cada linha de log (via HandlerBD, ligado ao logging)

Regra principal: o log NUNCA pode derrubar ou travar o robô. Usa uma
conexão PRÓPRIA (autocommit), separada da conexão de trabalho; qualquer
falha ao gravar é engolida (aviso no stderr) e a gravação fica pausada por
60s antes de tentar reconectar. O arquivo output/automacao.log continua
existindo como reserva.

Erros daqui NÃO usam o logger `logging` (iria gravar evento sobre falha de
gravar evento -> recursão); só `sys.stderr`.
"""

import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import psycopg2

BASE_DIR = Path(__file__).resolve().parent
PAUSA_APOS_FALHA_S = 60
RETENCAO_EVENTOS_DIAS = 90


def _versao_git() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=5,
        ).stdout.strip() or None
    except Exception:
        return None


class LogBD:
    def __init__(self):
        self.conn = None
        self.execucao_id: int | None = None
        self.tentativa_id: int | None = None
        self._iniciada = False
        self._pausado_ate = 0.0
        self._reset_tentativa()

    def _reset_tentativa(self):
        self.tentativa_id = None
        self._inicio_tentativa = 0.0
        self._etapa: str | None = None
        self._chave: str | None = None
        self._texto_modal: str | None = None

    # --- conexão / execução segura ---------------------------------------

    def _conectar(self):
        self.conn = psycopg2.connect(
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT"),
            dbname=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            connect_timeout=5,
            application_name="robo_requisicao_log",
        )
        self.conn.autocommit = True

    def _exec(self, sql: str, params: tuple = (), retorna: bool = False):
        if time.time() < self._pausado_ate:
            return None
        try:
            if self.conn is None or self.conn.closed:
                self._conectar()
            with self.conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone() if retorna else None
        except Exception as exc:
            print(f"[log_bd] falha ao gravar log no banco ({exc}); pausando {PAUSA_APOS_FALHA_S}s",
                  file=sys.stderr)
            try:
                if self.conn is not None:
                    self.conn.close()
            except Exception:
                pass
            self.conn = None
            self._pausado_ate = time.time() + PAUSA_APOS_FALHA_S
            return None

    # --- execução ----------------------------------------------------------

    def iniciar_execucao(self):
        self._iniciada = True
        self._garantir_execucao()
        self._exec(
            "DELETE FROM log_requisicao.evento WHERE criado_em < now() - make_interval(days => %s)",
            (RETENCAO_EVENTOS_DIAS,),
        )

    def _garantir_execucao(self):
        """Cria a linha de execução (também tenta de novo mais tarde, se o
        banco estava fora do ar no start)."""
        if self.execucao_id is not None or not self._iniciada:
            return
        linha = self._exec(
            "INSERT INTO log_requisicao.execucao (maquina, pid, versao) VALUES (%s, %s, %s) RETURNING id",
            (socket.gethostname(), os.getpid(), _versao_git()),
            retorna=True,
        )
        if linha:
            self.execucao_id = linha[0]

    def finalizar_execucao(self, status: str, motivo: str | None = None):
        if self.tentativa_id is not None:
            self.fechar_tentativa(None, mensagem=f"Execução finalizada ({status}) com tentativa em andamento")
        if self.execucao_id is None:
            return
        self._exec(
            "UPDATE log_requisicao.execucao SET finalizado_em = now(), status = %s, motivo_fim = %s WHERE id = %s",
            (status, motivo, self.execucao_id),
        )

    # --- tentativa ---------------------------------------------------------

    def abrir_tentativa(self, rec: dict):
        self._reset_tentativa()
        self._inicio_tentativa = time.time()
        self._garantir_execucao()
        if self.execucao_id is None:
            return
        linha = self._exec(
            """INSERT INTO log_requisicao.tentativa
                   (execucao_id, solicitacao_id, item, quantidade, classe, matricula, cc)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                self.execucao_id, rec.get("id"), _txt(rec.get("item")), rec.get("quantidade"),
                _txt(rec.get("classe_req")), _txt(rec.get("matricula")), _txt(rec.get("cc")),
            ),
            retorna=True,
        )
        if linha:
            self.tentativa_id = linha[0]

    def etapa(self, nome: str):
        """Marca a etapa atual do fluxo — vira `etapa_falha` se der erro."""
        self._etapa = nome

    def registrar_chave(self, chave: str):
        self._chave = chave
        if self.tentativa_id is not None:
            self._exec("UPDATE log_requisicao.tentativa SET chave_erp = %s WHERE id = %s",
                       (chave, self.tentativa_id))

    def registrar_texto_modal(self, texto: str):
        self._texto_modal = texto

    def fechar_tentativa(self, resultado: str | None, mensagem: str | None = None,
                         screenshot: str | None = None, chave: str | None = None):
        if self.tentativa_id is not None:
            self._exec(
                """UPDATE log_requisicao.tentativa SET
                       finalizado_em = now(), duracao_ms = %s, resultado = %s,
                       etapa_falha = %s, mensagem_erro = %s, texto_modal = %s,
                       screenshot = %s, chave_erp = COALESCE(%s, chave_erp)
                   WHERE id = %s""",
                (
                    int((time.time() - self._inicio_tentativa) * 1000), resultado,
                    None if resultado == "OK" else self._etapa, mensagem, self._texto_modal,
                    screenshot, chave or self._chave, self.tentativa_id,
                ),
            )
        self._reset_tentativa()

    # --- evento (chamado pelo HandlerBD) -----------------------------------

    def registrar_evento(self, nivel: str, mensagem: str, detalhe: str | None):
        self._garantir_execucao()
        if self.execucao_id is None:
            return
        self._exec(
            "INSERT INTO log_requisicao.evento (execucao_id, tentativa_id, nivel, mensagem, detalhe) "
            "VALUES (%s, %s, %s, %s, %s)",
            (self.execucao_id, self.tentativa_id, nivel, mensagem, detalhe),
        )


def _txt(valor):
    return None if valor is None else str(valor)


class HandlerBD(logging.Handler):
    """Manda cada linha de log para log_requisicao.evento."""

    def __init__(self, logbd: LogBD, level=logging.INFO):
        super().__init__(level)
        self.logbd = logbd

    def emit(self, record: logging.LogRecord):
        try:
            detalhe = None
            if record.exc_info:
                detalhe = logging.Formatter().formatException(record.exc_info)
            self.logbd.registrar_evento(record.levelname, record.getMessage(), detalhe)
        except Exception:
            pass  # log nunca derruba o robô


logbd = LogBD()
