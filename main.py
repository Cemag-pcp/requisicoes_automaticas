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
import time
import logging
import json
import psycopg2
import psycopg2.extras
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeout

load_dotenv()

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

ERP_URL      = os.getenv("INNOVARO_URL", "http://192.168.3.140/sistema")
ERP_USER     = os.getenv("INNOVARO_USERNAME", "user_almox")
ERP_PASS     = os.getenv("INNOVARO_PASSWORD", "samuel05")
SLOW_MO      = int(os.getenv("INNOVARO_SLOW_MO_MS", "250"))
LOOP_WAIT_S  = int(os.getenv("LOOP_WAIT_S", "60"))

CLASSE_MOV_DEPOSITO = "Movimentação de depositos"
DEPOSITO            = "Almox central"

OUTPUT_DIR = Path("output/screenshots")
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


def buscar_pendentes(conn):
    sql = """
        SELECT
            sr.id,
            i.codigo                            AS item,
            sr.quantidade,
            cr.nome                             AS classe_req,
            f.matricula                         AS matricula,
            cc.nome                             AS cc,
            sr.chave_innovaro
        FROM apontamento_v2.solicitacao_almox_solicitacaorequisicao sr
        JOIN apontamento_v2.cadastro_almox_itenssolicitacao    i  ON i.id  = sr.item_id
        JOIN apontamento_v2.cadastro_almox_classerequisicao    cr ON cr.id = sr.classe_requisicao_id
        JOIN apontamento_v2.cadastro_almox_funcionario         f  ON f.id  = sr.funcionario_id
        JOIN apontamento_v2.cadastro_almox_cc                  cc ON cc.id = sr.cc_id
        WHERE (sr.rpa IS NULL OR sr.rpa != 'OK')
          AND sr.data_entrega IS NOT NULL
        ORDER BY sr.id
    """
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(sql)
        return cur.fetchall()


def marcar_ok(conn, sr_id: int, chave_innovaro: str):
    sql = """
        UPDATE apontamento_v2.solicitacao_almox_solicitacaorequisicao
        SET rpa = 'OK', chave_innovaro = %s
        WHERE id = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (chave_innovaro, sr_id))
    conn.commit()


def marcar_erro(conn, sr_id: int, mensagem: str):
    msg = mensagem[:490] if len(mensagem) > 490 else mensagem
    sql = """
        UPDATE apontamento_v2.solicitacao_almox_solicitacaorequisicao
        SET rpa = %s
        WHERE id = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (f"ERRO: {msg}", sr_id))
    conn.commit()


# ---------------------------------------------------------------------------
# Playwright — utilitários
# ---------------------------------------------------------------------------

def capturar_screenshot(page: Page, nome: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    caminho = OUTPUT_DIR / f"{ts}_{nome}.png"
    page.screenshot(path=str(caminho), full_page=True)
    return caminho


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

def inserir_requisicao(page: Page, rec: dict) -> str:
    """
    Insere um registro na grdRequisicoes, aprova, baixa e grava.
    Retorna a chave gerada pelo ERP.
    """
    classe  = rec["classe_req"]
    matr    = rec["matricula"]
    cc      = rec["cc"]
    recurso = rec["item"]
    qtd     = str(int(rec["quantidade"]) if rec["quantidade"] == int(rec["quantidade"]) else rec["quantidade"])

    log.info(f"  → Inserindo: recurso={recurso}, qtd={qtd}, classe={classe}, req={matr}")

    # ── 1. Fechar menu se aberto e focar iframe ────────────────────────────
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
    page.keyboard.press("Tab")
    aguardar_erp_pronto(page, wait_ms=800)

    # ── 5. Confirmar edição ────────────────────────────────────────────────
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
    page.mouse.click(172, 131)
    page.wait_for_selector('[role="dialog"]', timeout=15000)
    _fechar_modal(page)

    # ── 9. Baixar — aguarda o botão "Continuar" do form ficar visível ────────
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

    # Aguarda o dialog de confirmação fechar antes de buscar o modal de resultado
    page.wait_for_selector('[role="dialog"]', state="hidden", timeout=10000)

    # Aguarda modal de resultado (sucesso ou erro)
    page.wait_for_selector('[role="dialog"]', timeout=15000)
    _verificar_e_fechar_modal_pos_gravar(page)

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


def _verificar_e_fechar_modal_pos_gravar(page: Page):
    """
    Lê o texto do modal pós-gravação, fecha e levanta RuntimeError se for erro.
    """
    dialog = page.get_by_role("dialog")
    texto = ""
    try:
        texto = dialog.inner_text().strip()
    except Exception:
        pass
    log.info(f"  → Texto do modal pós-gravar: '{texto}'")

    try:
        btn = dialog.get_by_role("button", name="Fechar")
        if btn.count():
            btn.first.click()
        else:
            dialog.get_by_role("button").first.click()
        page.wait_for_selector('[role="dialog"]', state="hidden", timeout=5000)
    except Exception:
        pass

    palavras_erro = ["erro", "falha", "não foi", "nao foi", "impossível", "impossivel", "inválid", "invalido"]
    if any(p in texto.lower() for p in palavras_erro):
        raise RuntimeError(f"ERP retornou erro após gravar: {texto}")


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
}


def ciclo(page: Page, conn):
    """Busca pendentes e processa um a um."""
    if MOCK_TEST:
        pendentes = [MOCK_REC]
        log.info("MOCK_TEST ativo — usando registro de teste.")
    else:
        pendentes = buscar_pendentes(conn)
    if not pendentes:
        log.info("Nenhum registro pendente.")
        return

    log.info(f"{len(pendentes)} registro(s) pendente(s).")

    for rec in pendentes:
        sr_id = rec["id"]
        log.info(f"Processando id={sr_id} | recurso={rec['item']}")
        try:
            chave = inserir_requisicao(page, rec)
            if not MOCK_TEST:
                marcar_ok(conn, sr_id, chave)
            log.info(f"  ✓ id={sr_id} gravado com chave={chave}")
        except Exception as exc:
            log.error(f"  ✗ Erro no id={sr_id}: {exc}", exc_info=True)
            capturar_screenshot(page, f"erro_id{sr_id}")
            if not MOCK_TEST:
                marcar_erro(conn, sr_id, str(exc))
        finally:
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
    log.info("Iniciando automação de requisições...")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=False,
            slow_mo=SLOW_MO,
        )
        context = browser.new_context(viewport={"width": 1600, "height": 900})
        page = context.new_page()

        conn = conectar_bd()
        log.info("Conexão com banco estabelecida.")

        try:
            login(page)
            abrir_requisicoes(page)

            while True:
                try:
                    ciclo(page, conn)
                except Exception as exc:
                    log.error(f"Erro no ciclo: {exc}", exc_info=True)
                    capturar_screenshot(page, "erro_ciclo")
                    try:
                        reabrir_requisicoes(page)
                    except Exception:
                        login(page)
                        abrir_requisicoes(page)

                log.info(f"Aguardando {LOOP_WAIT_S}s para próximo ciclo...")
                time.sleep(LOOP_WAIT_S)

        except KeyboardInterrupt:
            log.info("Automação encerrada pelo usuário.")
        finally:
            conn.close()
            browser.close()


if __name__ == "__main__":
    main()
