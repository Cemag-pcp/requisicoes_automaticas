"""
Automação de Requisições — Innovaro ERP
Roda 24h/dia: busca registros pendentes no PostgreSQL e insere no ERP.

Fluxo por registro:
  1. Insert na grdRequisicoes
  2. Preenche campos (Classe, Requisitante, CC, Recurso, Quantidade)
  3. Alterna para TABLE_VIEW (toggleView)
  4. Marca checkbox da linha
  5. Aprova
  6. Baixar (preenche form de depósito)
  7. Grava
  8. Atualiza banco: rpa='OK', chave_innovaro=<chave gerada>
"""

import os
import re
import time
import logging
import json
import psycopg2
import psycopg2.extras
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeout

load_dotenv()

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

ERP_URL      = os.getenv("INNOVARO_URL", "http://192.168.3.140/sistema").strip()
if "://" not in ERP_URL:
    # Sem protocolo o Chrome recusa ("Cannot navigate to invalid URL").
    ERP_URL = "http://" + ERP_URL
ERP_USER     = os.getenv("INNOVARO_USERNAME", "user_almox")
ERP_PASS     = os.getenv("INNOVARO_PASSWORD", "samuel05")
SLOW_MO      = int(os.getenv("INNOVARO_SLOW_MO_MS", "250"))
LOOP_WAIT_S  = int(os.getenv("LOOP_WAIT_S", "60"))

CLASSE_MOV_DEPOSITO = "Movimentação de depositos"
DEPOSITO            = "Almox central"

# Para esta matrícula o campo do requisitante é preenchido com outro valor
# (07876932355), não com a matrícula (pedido do usuário 2026-09-24).
MATRICULA_SUBSTITUIDA = "4607"
VALOR_SUBSTITUTO      = "07876932355"

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("output/automacao.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# Logs também no Postgres (schema log_requisicao — ver log_bd.py e
# sql/log_requisicao.sql). O arquivo acima continua como reserva.
from log_bd import logbd, HandlerBD  # noqa: E402
logging.getLogger().addHandler(HandlerBD(logbd))


# ---------------------------------------------------------------------------
# Banco de dados
# ---------------------------------------------------------------------------

def conectar_bd():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        connect_timeout=10,
    )


class BancoDados:
    """Guarda a conexão e reconecta quando ela cai — o robô roda 24h e o
    servidor derruba conexões ociosas. Sem isso, um `marcar_ok` que falha
    depois de um lançamento BEM-SUCEDIDO no ERP deixava o registro pendente
    e ele era lançado de novo na rodada seguinte (requisição duplicada)."""

    def __init__(self):
        self.conn = conectar_bd()

    def reconectar(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.conn = conectar_bd()
        log.info("Reconectado ao banco.")

    def executar(self, sql: str, params: tuple, tentativas: int = 3):
        """UPDATE com commit, reconectando e repetindo se a conexão falhar."""
        for tentativa in range(1, tentativas + 1):
            try:
                with self.conn.cursor() as cur:
                    cur.execute(sql, params)
                self.conn.commit()
                return
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
                log.warning(f"  → Banco indisponível (tentativa {tentativa}/{tentativas}): {exc}")
                if tentativa == tentativas:
                    raise
                time.sleep(5)
                try:
                    self.reconectar()
                except Exception as exc_conn:
                    log.warning(f"  → Falha ao reconectar: {exc_conn}")


class FalhaCriticaBanco(Exception):
    """Registro foi (ou pode ter sido) gravado no ERP, mas não deu pra
    registrar isso no banco. Continuar rodando lançaria de novo -> o robô
    PARA para alguém conferir."""


class ErroAposGravar(RuntimeError):
    """Falha DEPOIS de confirmar o 'Gravar' (clique em 'Sim'): o ERP pode
    ter gravado a requisição. NÃO pode ser repetida automaticamente."""

    def __init__(self, chave: str, mensagem: str):
        super().__init__(mensagem)
        self.chave = chave


def buscar_pendentes(bd: "BancoDados"):
    sql = """
        SELECT
            sr.id,
            i.codigo                            AS item,
            sr.quantidade,
            cr.nome                             AS classe_req,
            f.matricula                         AS matricula,
            cc.nome                             AS cc,
            sr.chave_innovaro,
            to_char(sr.data_solicitacao AT TIME ZONE 'America/Fortaleza', 'DD/MM/YYYY')
                                                AS data_emissao
        FROM apontamento_v2.solicitacao_almox_solicitacaorequisicao sr
        JOIN apontamento_v2.cadastro_almox_itenssolicitacao    i  ON i.id  = sr.item_id
        JOIN apontamento_v2.cadastro_almox_classerequisicao    cr ON cr.id = sr.classe_requisicao_id
        JOIN apontamento_v2.cadastro_almox_funcionario         f  ON f.id  = sr.funcionario_id
        JOIN apontamento_v2.cadastro_almox_cc                  cc ON cc.id = sr.cc_id
        WHERE (sr.rpa IS NULL OR sr.rpa != 'OK')
          AND (sr.rpa IS NULL OR sr.rpa NOT LIKE 'VERIFICAR%%')
          AND sr.data_entrega IS NOT NULL
        ORDER BY sr.id
    """
    for tentativa in (1, 2):
        try:
            with bd.conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(sql)
                linhas = cur.fetchall()
            bd.conn.commit()  # não deixar a conexão 'idle in transaction'
            return linhas
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
            if tentativa == 2:
                raise
            log.warning(f"Banco indisponível ao buscar pendentes ({exc}) — reconectando...")
            bd.reconectar()


def marcar_ok(bd: BancoDados, sr_id: int, chave_innovaro: str):
    sql = """
        UPDATE apontamento_v2.solicitacao_almox_solicitacaorequisicao
        SET rpa = 'OK', chave_innovaro = %s
        WHERE id = %s
    """
    bd.executar(sql, (chave_innovaro, sr_id))


def marcar_erro(bd: BancoDados, sr_id: int, mensagem: str):
    """Erro ANTES de gravar (ou recusa explícita do ERP): nada foi gravado,
    o registro volta pra fila e é tentado de novo na próxima rodada."""
    msg = mensagem[:490] if len(mensagem) > 490 else mensagem
    sql = """
        UPDATE apontamento_v2.solicitacao_almox_solicitacaorequisicao
        SET rpa = %s
        WHERE id = %s
    """
    bd.executar(sql, (f"ERRO: {msg}", sr_id))


def marcar_verificar(bd: BancoDados, sr_id: int, chave_innovaro: str, mensagem: str):
    """Falha DEPOIS de confirmar o 'Gravar': o ERP pode ter gravado. Marca
    'VERIFICAR: ...' (fica FORA da fila — `buscar_pendentes` ignora) e
    guarda a chave pra alguém conferir no Innovaro. Para reprocessar, limpar
    o campo rpa (NULL) manualmente."""
    msg = mensagem[:480] if len(mensagem) > 480 else mensagem
    sql = """
        UPDATE apontamento_v2.solicitacao_almox_solicitacaorequisicao
        SET rpa = %s, chave_innovaro = %s
        WHERE id = %s
    """
    bd.executar(sql, (f"VERIFICAR: {msg}", chave_innovaro, sr_id))


# ---------------------------------------------------------------------------
# Playwright — utilitários
# ---------------------------------------------------------------------------

def aguardar_erp_pronto(page: Page, wait_ms: int = 500, timeout_ms: int = 120000):
    """Aguarda o ERP sair de overlays de processamento."""
    page.wait_for_load_state("domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    inicio = time.time()
    while (time.time() - inicio) * 1000 < timeout_ms:
        ocupado = page.evaluate("""() => {
            const iframe = document.querySelector('iframe');
            const doc = iframe?.contentDocument;
            if (!doc) return false;
            const body = doc.body?.innerText || '';
            const pattern = /Explodindo|Gravando|Criando n[ií]vel|Carregando|Processando/i;
            return pattern.test(body) ||
                   !!doc.querySelector('#progressMessageBox, #statusMessageBox, [role="progressbar"]') ||
                   !!doc.querySelector('.wf-progress-dialog__content');
        }""")
        if not ocupado:
            if wait_ms > 0:
                page.wait_for_timeout(wait_ms)
            return
        page.wait_for_timeout(500)


def _buscar_chave_no_json(obj) -> str:
    """Busca recursivamente fieldName='CHAVE' e retorna o valor de display."""
    if isinstance(obj, dict):
        if obj.get('fieldName') == 'CHAVE' and obj.get('display'):
            return str(obj['display'])
        for v in obj.values():
            r = _buscar_chave_no_json(v)
            if r:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _buscar_chave_no_json(item)
            if r:
                return r
    return ''


def aguardar_grid_pronta(page: Page, grid_name: str = "grdRequisicoes", timeout_ms: int = 30000):
    """Aguarda a grid estar carregada e acessível no iframe."""
    page.wait_for_function(f"""() => {{
        const fw = document.querySelector('iframe')?.contentWindow;
        return !!fw?.Environment?.getInstance?.()?.currentProcess?.getGrid?.('{grid_name}');
    }}""", timeout=timeout_ms)


def _aguardar_grid_em_form_view(page: Page, em_form: bool = True, timeout_ms: int = 10000):
    """Aguarda a grid atingir o modo de visão esperado."""
    page.wait_for_function(f"""() => {{
        const grid = window.Environment?.getInstance?.()?.currentProcess?.getGrid?.('grdRequisicoes');
        return grid?.inFormView === {str(em_form).lower()};
    }}""", timeout=timeout_ms)


def _aguardar_sem_insercao(page: Page, timeout_ms: int = 15000):
    """Aguarda o grid sair do modo de inserção."""
    page.wait_for_function("""() => {
        const grid = window.Environment?.getInstance?.()?.currentProcess?.getGrid?.('grdRequisicoes');
        return grid ? !grid.isInserting?.() : false;
    }""", timeout=timeout_ms)


# ---------------------------------------------------------------------------
# Playwright — login e navegação
# ---------------------------------------------------------------------------

def login(page: Page):
    log.info("Fazendo login...")
    page.goto(ERP_URL, wait_until="domcontentloaded")
    page.locator("#username").fill(ERP_USER)
    page.locator("#password").fill(ERP_PASS)
    page.locator("#submit-login").click()
    # Aguarda o botão Menu aparecer — sinal de login bem-sucedido
    page.get_by_role("button", name="Menu", exact=True).wait_for(timeout=30000)
    log.info("Login realizado com sucesso.")


def _navegar_menu_requisicoes(page: Page):
    """
    Abre o menu e navega Estoque > Requisição > Requisições.
    Verifica se 'dashboardRequisições' já está visível após clicar em Estoque.
    Se não estiver, expande o submenu 'Requisição' antes de clicar.
    """
    fechar = page.get_by_role("button", name="Fechar menu", exact=True)
    if not fechar.is_visible():
        page.get_by_role("button", name="Menu", exact=True).click()
        fechar.wait_for(state="visible", timeout=10000)

    page.get_by_text("Estoque", exact=True).click()

    item_req = page.get_by_text("dashboardRequisições", exact=True)
    if not item_req.is_visible():
        page.get_by_text("Requisição", exact=True).click()
        item_req.wait_for(state="visible", timeout=8000)

    item_req.click()
    aguardar_erp_pronto(page, wait_ms=0)
    aguardar_grid_pronta(page)

    # Fechar menu — aguarda desaparecer
    fechar2 = page.get_by_role("button", name="Fechar menu", exact=True)
    if fechar2.is_visible():
        fechar2.click()
        fechar2.wait_for(state="hidden", timeout=5000)


def abrir_requisicoes(page: Page):
    """Navega até Estoque > Requisição > Requisições."""
    log.info("Abrindo tela de Requisições...")
    _navegar_menu_requisicoes(page)
    log.info("Tela de Requisições aberta.")


def fechar_aba_atual(page: Page):
    """Fecha a aba ativa do ERP, descartando dialogs abertos antes."""
    try:
        dialog = page.get_by_role("dialog")
        if dialog.is_visible():
            dialog.get_by_role("button").first.click()
            page.wait_for_selector('[role="dialog"]', state="hidden", timeout=3000)
    except Exception:
        pass

    try:
        btn_fechar = page.locator('button[aria-label="Fechar"]').first
        btn_fechar.click()
        btn_fechar.wait_for(state="hidden", timeout=5000)
    except Exception:
        pass


def reabrir_requisicoes(page: Page):
    """Fecha a aba atual e reabre Requisições via menu."""
    fechar_aba_atual(page)
    _navegar_menu_requisicoes(page)


# ---------------------------------------------------------------------------
# Playwright — fluxo de inserção de uma requisição
# ---------------------------------------------------------------------------

def _conferir_emissao(page: Page, esperado: str):
    """Relê o campo Emissão (iframe da tela) e confirma que ficou com a data
    pedida. Diferente -> erro ANTES do Gravar (seguro: volta pra fila). Se
    não achar o input pra conferir, só avisa no log."""
    valor = page.evaluate("""() => {
        const doc = document.querySelector('iframe')?.contentDocument;
        const el = doc?.querySelector('[name="EMISSAO"]');
        return el ? (el.value ?? el.innerText ?? '').trim() : null;
    }""")
    if valor is None:
        log.warning(f"  → Emissão digitada ({esperado}), mas não achei o campo EMISSAO pra conferir.")
        return
    log.info(f"  → Emissão: {valor!r} (esperado {esperado})")

    def _dma(texto: str):
        partes = re.findall(r"\d+", texto)
        if len(partes) < 3:
            return None
        d, m, a = (int(p) for p in partes[:3])
        return d, m, a % 100  # aceita ano com 2 ou 4 dígitos

    if _dma(valor) != _dma(esperado):
        raise RuntimeError(f"Campo Emissão ficou {valor!r} em vez de {esperado!r}.")


def inserir_requisicao(page: Page, rec: dict) -> str:
    """
    Insere um registro na grdRequisicoes, aprova, baixa e grava.
    Retorna a chave gerada pelo ERP.
    """
    classe  = rec["classe_req"]
    matr    = rec["matricula"]
    if str(matr).strip() == MATRICULA_SUBSTITUIDA:
        matr = VALOR_SUBSTITUTO
    cc      = rec["cc"]
    recurso = rec["item"]
    qtd     = str(int(rec["quantidade"]) if rec["quantidade"] == int(rec["quantidade"]) else rec["quantidade"])

    log.info(f"  → Inserindo: recurso={recurso}, qtd={qtd}, classe={classe}, req={matr}")

    # ── 1. Fechar menu se aberto e focar iframe ────────────────────────────
    logbd.etapa("preparar")
    fechar_menu = page.get_by_role("button", name="Fechar menu", exact=True)
    if fechar_menu.is_visible():
        fechar_menu.click()
        fechar_menu.wait_for(state="hidden", timeout=5000)
    page.locator("iframe").click()

    # ── 2. Garantir FORM_VIEW ──────────────────────────────────────────────
    em_form = page.evaluate("""() => {
        const grid = window.Environment?.getInstance?.()?.currentProcess?.getGrid?.('grdRequisicoes');
        return grid?.inFormView ?? false;
    }""")
    if not em_form:
        page.evaluate("""() => {
            const grid = window.Environment.getInstance().currentProcess.getGrid('grdRequisicoes');
            grid.toggleView();
        }""")
        _aguardar_grid_em_form_view(page, em_form=True)
    log.info("  → Grid em FORM_VIEW, iniciando insert...")

    # ── 3. Insert + captura de CHAVE via rede ─────────────────────────────
    logbd.etapa("insert")
    chave_capturada: list[str] = []

    def _on_response_insert(response):
        if '/wf/data/' not in response.url or chave_capturada:
            return
        try:
            c = _buscar_chave_no_json(response.json())
            if c:
                chave_capturada.append(c)
        except Exception:
            pass

    page.on('response', _on_response_insert)
    try:
        page.evaluate("""() => {
            const grid = window.Environment.getInstance().currentProcess.getGrid('grdRequisicoes');
            grid.emit('insert', { gridName: 'grdRequisicoes' });
        }""")
        # Aguarda até a resposta de rede com a CHAVE ser recebida (timeout 15s)
        deadline = time.time() + 15
        while not chave_capturada and time.time() < deadline:
            page.wait_for_timeout(200)
    finally:
        page.remove_listener('response', _on_response_insert)

    chave = chave_capturada[0] if chave_capturada else ''
    if not chave:
        raise RuntimeError("CHAVE não capturada na resposta de rede após insert.")
    print(f"[CHAVE] {chave}")
    log.info(f"  → Chave capturada via rede: {chave}")
    logbd.registrar_chave(chave)
    logbd.etapa("preencher")

    # ── 4. Preencher campos — Tab avança entre campos ──────────────────────
    # Após insert o ERP foca Classe diretamente (CHAVE readOnly é pulado).
    # Cada campo lookup precisa de tempo para o ERP resolver via rede.

    page.keyboard.type(classe)
    page.keyboard.press("Tab")
    aguardar_erp_pronto(page, wait_ms=1000)

    page.keyboard.type(matr)
    page.keyboard.press("Tab")
    aguardar_erp_pronto(page, wait_ms=1000)

    page.keyboard.type(cc)
    page.keyboard.press("Tab")
    aguardar_erp_pronto(page, wait_ms=1000)

    page.keyboard.type(recurso)
    page.keyboard.press("Tab")
    aguardar_erp_pronto(page, wait_ms=1500)  # ERP resolve recurso e preenche UM

    page.keyboard.press("Tab")  # Pack → pular
    page.wait_for_timeout(300)
    page.keyboard.press("Tab")  # UM → readOnly, auto
    page.wait_for_timeout(300)

    page.keyboard.type(qtd)
    page.keyboard.press("Tab")  # Quantidade -> Observação
    aguardar_erp_pronto(page, wait_ms=800)

    # Emissão = data em que o usuário solicitou (sr.data_solicitacao).
    # Ordem dos campos: QUANTIDADE -> OBSERVACAO -> EMISSAO -> EMISSAOH.
    # O ERP pré-preenche Emissão com a data de hoje — por isso Ctrl+A antes.
    data_emissao = rec.get("data_emissao")
    if data_emissao:
        logbd.etapa("emissao")
        page.keyboard.press("Tab")  # Observação (vazia) -> Emissão
        page.wait_for_timeout(300)
        page.keyboard.press("Control+A")
        page.keyboard.type(data_emissao)
        page.keyboard.press("Tab")
        aguardar_erp_pronto(page, wait_ms=800)
        _conferir_emissao(page, data_emissao)
        logbd.etapa("preencher")

    # ── 5. Confirmar edição ────────────────────────────────────────────────
    logbd.etapa("confirmar_edicao")
    confirmou = page.evaluate("""() => {
        const iframeDoc = document.querySelector('iframe')?.contentDocument;
        const btn = iframeDoc?.querySelector('[aria-label="Confirmar edição"]');
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    if not confirmou:
        page.evaluate("""() => {
            const grid = window.Environment?.getInstance?.()?.currentProcess?.getGrid?.('grdRequisicoes');
            grid?.emit?.('confirm', { gridName: 'grdRequisicoes' });
        }""")
    _aguardar_sem_insercao(page)

    # ── 6. Alternar para TABLE_VIEW ────────────────────────────────────────
    logbd.etapa("selecionar_linha")
    page.evaluate("""() => {
        const grid = window.Environment.getInstance().currentProcess.getGrid('grdRequisicoes');
        if (grid.inFormView) grid.toggleView();
    }""")
    _aguardar_grid_em_form_view(page, em_form=False)
    aguardar_erp_pronto(page, wait_ms=500)

    # ── 7. Marcar checkbox da primeira linha ──────────────────────────────
    page.mouse.click(27, 205)
    aguardar_erp_pronto(page, wait_ms=500)

    # ── 8. Aprovar — aguarda modal de confirmação ─────────────────────────
    logbd.etapa("aprovar")
    page.mouse.click(172, 131)
    page.wait_for_selector('[role="dialog"]', timeout=15000)
    _fechar_modal(page)

    # ── 9. Baixar — aguarda o botão "Continuar" do form ficar visível ────────
    logbd.etapa("baixar")
    page.mouse.click(321, 131)
    continuar = page.locator("text=Continuar").first
    continuar.wait_for(state="visible", timeout=15000)

    page.keyboard.type(CLASSE_MOV_DEPOSITO)
    page.keyboard.press("Tab")
    aguardar_erp_pronto(page, wait_ms=1500)  # aguarda dropdown do lookup
    page.keyboard.press("Enter")
    aguardar_erp_pronto(page, wait_ms=1000)

    page.keyboard.type(DEPOSITO)
    page.keyboard.press("Tab")
    aguardar_erp_pronto(page, wait_ms=1000)

    page.keyboard.type("h")
    page.keyboard.press("Tab")
    aguardar_erp_pronto(page, wait_ms=500)

    continuar.click()
    aguardar_erp_pronto(page, wait_ms=1000)

    # ── 10. Gravar — aguarda botão ficar habilitado ───────────────────────
    logbd.etapa("gravar")
    page.wait_for_function("""() => {
        const btns = [...document.querySelectorAll('button')];
        const g = btns.find(b => b.innerText?.trim() === 'Gravar');
        return g && !g.disabled;
    }""", timeout=20000)
    page.locator("button", has_text="Gravar").first.click()

    # Confirmar modal "Deseja realmente gravar..."
    sim = page.get_by_role("dialog").get_by_role("button", name="Sim")
    sim.wait_for(state="visible", timeout=8000)
    sim.click()
    logbd.etapa("pos_gravar")
    log.info("  → 'Sim' do Gravar clicado — aguardando resultado...")

    # ── PONTO SEM VOLTA ─────────────────────────────────────────────────────
    # A partir daqui o ERP pode ter gravado. Qualquer falha que NÃO seja uma
    # recusa explícita do ERP vira ErroAposGravar -> 'VERIFICAR', nunca
    # reprocessada automaticamente (era a causa das requisições duplicadas:
    # timeout/modal de progresso lido como erro -> 'ERRO' -> lançada de novo).
    try:
        # o próprio modal "Deseja realmente gravar?" precisa sumir antes,
        # senão ele seria lido como "nova confirmação"
        sim.wait_for(state="hidden", timeout=15000)
        modal = _aguardar_resultado_gravacao(page)
    except Exception as exc:
        raise ErroAposGravar(chave, f"Sem resultado claro após gravar ({exc})") from exc
    if modal is not None:
        # levanta em erro/dúvida; retorna normalmente só no modal de sucesso
        _avaliar_e_fechar_modal_pos_gravar(page, modal, chave)
        log.info(f"  ✓ ERP confirmou: {modal['texto'].splitlines()[0]!r} — chave: {chave}")
        return chave

    # Silêncio NÃO basta (id 19640, 26/11: sem modal nenhum e NÃO gravou —
    # tela idêntica à de um erro de saldo, com 'Gravar' ainda habilitado).
    # Prova positiva: depois de gravar, o 'Gravar' fica desabilitado (sem
    # alterações pendentes). Ainda habilitado -> não dá pra afirmar -> VERIFICAR.
    gravar_habilitado = _botao_gravar_habilitado(page)
    log.info(f"  → Nenhum modal em {SILENCIO_SUCESSO_MS // 1000}s após gravar; "
             f"botão Gravar habilitado={gravar_habilitado}")
    if gravar_habilitado is not False:
        raise ErroAposGravar(
            chave,
            "Sem modal após gravar, mas o botão Gravar continua habilitado (alterações pendentes) "
            "— provavelmente NÃO gravou",
        )

    log.info(f"  ✓ Fluxo concluído — chave: {chave}")
    return chave


def _fechar_modal(page: Page):
    """Fecha o modal de sucesso/confirmação e aguarda desaparecer."""
    try:
        dialog = page.get_by_role("dialog")
        btn = dialog.get_by_role("button", name="Fechar")
        if btn.count():
            btn.first.click()
        else:
            dialog.get_by_role("button").first.click()
        page.wait_for_selector('[role="dialog"]', state="hidden", timeout=5000)
    except Exception:
        pass


_RE_PROGRESSO = re.compile(r"^\s*$|aguarde|gravando|carregando|processando|^\s*\d+\s*%\s*$", re.IGNORECASE)


def _dialogs_visiveis(page: Page) -> list[dict]:
    """[{texto, tem_sim}] de cada modal visível."""
    return page.evaluate("""() => [...document.querySelectorAll('[role="dialog"], [role="alertdialog"]')]
        .filter(d => { const r = d.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
        .map(d => ({
            texto: (d.innerText || '').trim(),
            tem_sim: [...d.querySelectorAll('button')].some(b =>
                (b.getAttribute('aria-label') || b.innerText || '').trim() === 'Sim'),
        }))""")


def _erp_ocupado(page: Page) -> bool:
    """Mesma checagem de `aguardar_erp_pronto`, numa passada só."""
    return page.evaluate("""() => {
        const doc = document.querySelector('iframe')?.contentDocument;
        if (!doc) return false;
        const body = doc.body?.innerText || '';
        return /Explodindo|Gravando|Criando n[ií]vel|Carregando|Processando/i.test(body) ||
               !!doc.querySelector('#progressMessageBox, #statusMessageBox, [role="progressbar"]') ||
               !!doc.querySelector('.wf-progress-dialog__content');
    }""")


SILENCIO_SUCESSO_MS = 5000


def _botao_gravar_habilitado(page: Page) -> bool | None:
    """True/False = estado do botão 'Gravar' da barra; None = não achou."""
    return page.evaluate("""() => {
        const g = [...document.querySelectorAll('button')].find(b => b.innerText?.trim() === 'Gravar');
        return g ? !g.disabled : null;
    }""")


def _aguardar_resultado_gravacao(page: Page, timeout_ms: int = 120000,
                                 silencio_ms: int = SILENCIO_SUCESSO_MS) -> dict | None:
    """Depois do 'Sim' do Gravar o ERP mostra modais de PROGRESSO
    ('Gravando requisições...', 'Aguarde...', '0%') e depois o modal de
    resultado — 'Requisições gravadas com sucesso!' ou 'Erro ...'. Então:
    - aparece modal que não é de progresso -> devolve ele (sucesso/erro/dúvida);
    - fica `silencio_ms` seguidos SEM modal nenhum e sem o ERP ocupado
      -> devolve None (= sucesso);
    - nem um nem outro em `timeout_ms` -> TimeoutError (vira VERIFICAR)."""
    inicio = time.time()
    silencio_desde = None
    vistos: set[str] = set()
    while (time.time() - inicio) * 1000 < timeout_ms:
        modais = _dialogs_visiveis(page)
        for modal in modais:
            # registra TODO modal visto (inclusive progresso) — evidência
            # pra casos como o id 19640 (sem modal, mas não gravou)
            if modal["texto"] not in vistos:
                vistos.add(modal["texto"])
                log.info(f"  → Modal visto após gravar ({time.time() - inicio:.1f}s): {modal['texto'][:200]!r}")
        for modal in modais:
            if not _RE_PROGRESSO.search(modal["texto"]):
                return modal
        if modais or _erp_ocupado(page):
            silencio_desde = None
        elif silencio_desde is None:
            silencio_desde = time.time()
        elif (time.time() - silencio_desde) * 1000 >= silencio_ms:
            return None
        page.wait_for_timeout(300)
    raise TimeoutError(f"gravação não terminou em {timeout_ms // 1000}s (progresso não sumiu)")


def _avaliar_e_fechar_modal_pos_gravar(page: Page, modal: dict, chave: str):
    """Classifica o modal que apareceu depois do Gravar:
    - 'Requisições gravadas com sucesso!' -> retorna normalmente (OK);
    - recusa EXPLÍCITA do ERP (modal de 'Erro', ex.: 'Saldo insuficiente')
      -> RuntimeError comum: nada foi gravado, pode tentar de novo depois;
    - nova pergunta de confirmação (tem botão 'Sim', ex.: 'custo zero...
      Deseja realmente gravar?') -> ErroAposGravar (não responde sozinho);
    - qualquer outro texto -> ErroAposGravar (inesperado: conferir)."""
    texto = modal["texto"]
    log.info(f"  → Texto do modal pós-gravar: {texto!r}")
    logbd.registrar_texto_modal(texto)

    if modal["tem_sim"]:
        raise ErroAposGravar(chave, f"ERP pediu nova confirmação após gravar: {texto}")

    dialog = page.get_by_role("dialog")
    try:
        btn = dialog.get_by_role("button", name="Fechar")
        if btn.count():
            btn.first.click()
        else:
            dialog.get_by_role("button").first.click()
        page.wait_for_selector('[role="dialog"]', state="hidden", timeout=5000)
    except Exception:
        pass

    texto_min = texto.lower()
    if texto_min.startswith("erro") or "erro:" in texto_min:
        raise RuntimeError(f"ERP retornou erro após gravar: {texto}")
    if _RE_SUCESSO.search(texto):
        return  # 'Requisições gravadas com sucesso!' -> OK
    raise ErroAposGravar(chave, f"Modal inesperado após gravar: {texto}")


# Confirmado ao vivo em 26/11/2026 (id 20297, chave 107292649): no sucesso o
# ERP MOSTRA o modal 'Requisições gravadas com sucesso!' (com botão Fechar).
_RE_SUCESSO = re.compile(r"gravad[ao]s?\s+com\s+sucesso", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------

MOCK_TEST = 0
MOCK_REC = {
    "id": 0,
    "item": "700017",
    "quantidade": 1,
    "classe_req": "Req p Consumo",
    "matricula": "4357",
    "cc": "2310",
    "chave_innovaro": None,
    "data_emissao": None,
}


def ciclo(page: Page, bd: BancoDados):
    """Busca pendentes e processa um a um."""
    if MOCK_TEST:
        pendentes = [MOCK_REC]
        log.info("MOCK_TEST ativo — usando registro de teste.")
    else:
        pendentes = buscar_pendentes(bd)
    if not pendentes:
        log.info("Nenhum registro pendente.")
        return

    log.info(f"{len(pendentes)} registro(s) pendente(s).")

    for rec in pendentes:
        sr_id = rec["id"]
        logbd.abrir_tentativa(dict(rec))
        log.info(f"Processando id={sr_id} | recurso={rec['item']}")
        try:
            try:
                chave = inserir_requisicao(page, rec)
            except ErroAposGravar as exc:
                log.error(f"  ⚠ id={sr_id} PODE ter sido gravado (chave={exc.chave}) — marcado VERIFICAR: {exc}")
                logbd.fechar_tentativa("VERIFICAR", str(exc), chave=exc.chave)
                if not MOCK_TEST:
                    try:
                        marcar_verificar(bd, sr_id, exc.chave, str(exc))
                    except Exception as exc_bd:
                        raise FalhaCriticaBanco(
                            f"id={sr_id} pode ter sido gravado (chave={exc.chave}), mas não consegui "
                            f"marcar VERIFICAR no banco: {exc_bd}"
                        ) from exc_bd
            except Exception as exc:
                # Falha ANTES de confirmar o Gravar (ou recusa explícita do
                # ERP): nada gravado, volta pra fila.
                log.error(f"  ✗ Erro no id={sr_id}: {exc}", exc_info=True)
                logbd.fechar_tentativa("ERRO", str(exc))
                if not MOCK_TEST:
                    try:
                        marcar_erro(bd, sr_id, str(exc))
                    except Exception as exc_bd:
                        log.error(f"  → Não consegui marcar ERRO no banco (id={sr_id}): {exc_bd}")
            else:
                log.info(f"  ✓ id={sr_id} gravado com chave={chave}")
                logbd.fechar_tentativa("OK", chave=chave)
                if not MOCK_TEST:
                    try:
                        marcar_ok(bd, sr_id, chave)
                    except Exception as exc_bd:
                        raise FalhaCriticaBanco(
                            f"id={sr_id} FOI gravado no ERP (chave={chave}), mas não consegui marcar OK "
                            f"no banco: {exc_bd}"
                        ) from exc_bd
        finally:
            if logbd.tentativa_id is not None:  # saiu por exceção inesperada (ex.: KeyboardInterrupt)
                logbd.fechar_tentativa(None, "Tentativa interrompida")
            # Sempre fechar a aba e reabrir pelo menu, independente do que aconteceu
            try:
                reabrir_requisicoes(page)
            except Exception as e_reabrir:
                log.warning(f"  → reabrir_requisicoes falhou ({e_reabrir}), fazendo login fresh...")
                try:
                    login(page)
                    abrir_requisicoes(page)
                except Exception as e_login:
                    log.error(f"  → Falha no login fresh: {e_login}")


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logbd.iniciar_execucao()
    log.info("Iniciando automação de requisições...")
    status_fim, motivo_fim = "PARADO_ERRO", None
    try:
        _executar_robo()
        status_fim = "ENCERRADO"
    except KeyboardInterrupt:
        status_fim = "INTERROMPIDO"
    except FalhaCriticaBanco as exc:
        motivo_fim = f"FalhaCriticaBanco: {exc}"
    except BaseException as exc:
        motivo_fim = f"{type(exc).__name__}: {exc}"
        log.critical(f"Robô parou por erro inesperado: {exc}", exc_info=True)
        raise
    finally:
        logbd.finalizar_execucao(status_fim, motivo_fim)


def _executar_robo():

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=False,
            slow_mo=SLOW_MO,
        )
        context = browser.new_context(viewport={"width": 1600, "height": 900})
        page = context.new_page()

        bd = BancoDados()
        log.info("Conexão com banco estabelecida.")

        try:
            login(page)
            abrir_requisicoes(page)

            while True:
                try:
                    ciclo(page, bd)
                except FalhaCriticaBanco as exc:
                    log.critical(
                        f"PARANDO O ROBÔ para evitar requisição duplicada: {exc}. "
                        "Atualize o registro manualmente no banco antes de reiniciar."
                    )
                    raise
                except Exception as exc:
                    log.error(f"Erro no ciclo: {exc}", exc_info=True)
                    try:
                        reabrir_requisicoes(page)
                    except Exception:
                        login(page)
                        abrir_requisicoes(page)

                log.info(f"Aguardando {LOOP_WAIT_S}s para próximo ciclo...")
                time.sleep(LOOP_WAIT_S)

        except KeyboardInterrupt:
            log.info("Automação encerrada pelo usuário.")
            raise
        finally:
            try:
                bd.conn.close()
            except Exception:
                pass
            browser.close()


if __name__ == "__main__":
    main()
