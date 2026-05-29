# Boas Práticas — Automação do ERP Innovaro com Playwright

Documento consolidado a partir das automações existentes:
- Automação de Compras (DEE, Estoque, Explosão)
- Saldo ao Vivo
- Requisição

---

## 1. Configuração do Navegador

O Innovaro é um sistema web que depende de Chrome. Sempre use o Chrome instalado na máquina, não o navegador embutido do Playwright.

```python
# Python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(
        channel="chrome",   # Chrome instalado, não Chromium bundled
        headless=False,     # Manter visível — o ERP tem comportamentos diferentes sem janela
        slow_mo=250,        # Essencial para estabilidade. Aumentar para 500 se houver falhas
    )
    context = browser.new_context(viewport={"width": 1600, "height": 900})
    page = context.new_page()
```

```js
// JavaScript
const browser = await chromium.launch({
    channel: "chrome",
    headless: false,
    slowMo: 250,
});
const context = await browser.newContext({ viewport: { width: 1600, height: 900 } });
const page = await context.newPage();
```

---

## 2. URLs do Sistema

| Ambiente | URL |
|---|---|
| Produção (servidor local) | `http://192.168.3.140/sistema` ou `http://192.168.3.141/sistema` |
| Produção (cloud) | `https://cemag.innovaro.com.br/sistema` |
| Homologação (testes) | `https://hcemag.innovaro.com.br/sistema` |

Prefira variável de ambiente para a URL:

```python
ERP_URL = os.getenv("INNOVARO_URL", "http://192.168.3.140/sistema")
```

---

## 3. Login

O login tem seletores estáveis por `id`. Aguarde o botão "Menu" aparecer como sinal de que o login foi bem-sucedido.

```python
def login(page, username: str, password: str):
    page.goto(ERP_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    page.locator("#username").fill(username)
    page.locator("#password").fill(password)
    page.locator("#submit-login").click()

    # Aguardar tela principal — "Menu" é o indicador confiável
    for _ in range(15):
        if page.get_by_role("button", name="Menu", exact=True).count():
            return
        page.wait_for_timeout(1000)

    raise RuntimeError("Login não chegou na tela principal.")
```

**Alternativa com `getByRole` (mais semântico):**

```python
page.get_by_role("textbox", name="Usuário").fill(username)
page.get_by_role("textbox", name="Senha").fill(password)
page.get_by_role("button", name="Entrar", exact=True).click()
page.get_by_role("button", name="Menu", exact=True).wait_for(timeout=30000)
```

---

## 4. Menu Principal

O menu é um painel lateral que abre e fecha. Antes de clicar em "Menu", verifique se ele já está aberto (botão "Fechar menu" visível). Caso contrário o segundo clique fecha o menu.

```python
def abrir_menu_principal(page):
    fechar = page.get_by_role("button", name="Fechar menu", exact=True)
    if fechar.is_visible():
        return  # já está aberto

    page.get_by_role("button", name="Menu", exact=True).click()
    fechar.wait_for(timeout=15000)
    page.wait_for_timeout(500)
```

---

## 5. Navegação por Módulos

O Innovaro usa um menu em árvore. Clique nos itens em sequência, aguardando o nível filho expandir.

```python
# Exemplo: Produção > Plano mestre e simulação (MPS) > Plano mestre e simulação
def ir_para_plano_mestre(page):
    abrir_menu_principal(page)
    page.get_by_text("Produção", exact=True).click()
    page.wait_for_timeout(500)
    page.get_by_text("Plano mestre e simulação (MPS)", exact=True).click()
    page.wait_for_timeout(500)
    page.get_by_text("Plano mestre e simulação", exact=True).click()
    page.get_by_role("tab", name="Plano mestre e simulação").wait_for(timeout=30000)
    aguardar_erp_pronto(page, wait_ms=5000)

# Exemplo: Estoque > Consultas > Saldos de Recursos - CEMAG
def ir_para_saldos_recursos(page):
    abrir_menu_principal(page)
    page.get_by_text("Estoque", exact=True).click()
    page.wait_for_timeout(500)
    page.get_by_text("Consultas", exact=True).click()
    page.wait_for_timeout(500)
    page.get_by_text("Saldos de Recursos - CEMAG", exact=True).click()
    page.wait_for_timeout(2500)

# Exemplo: Compra > Consultas > Análise de Pedidos Pendentes ou Baixados - CEMAG
def ir_para_analise_pedidos(page):
    abrir_menu_principal(page)
    page.get_by_text("Compra", exact=True).click()
    page.wait_for_timeout(500)
    page.get_by_text("Consultas", exact=True).click()
    page.wait_for_timeout(500)
    page.get_by_text("Análise de Pedidos Pendentes ou Baixados - CEMAG", exact=True).click()
    page.wait_for_timeout(2500)
```

**Módulos conhecidos:**
- `Produção > Plano mestre e simulação (MPS) > Plano mestre e simulação`
- `Produção > Plano mestre e simulação (MPS) > Relatório de Logística de Compras da Simulação`
- `Estoque > Consultas > Saldos de Recursos - CEMAG`
- `Compra > Consultas > Análise de Pedidos Pendentes ou Baixados - CEMAG`
- `Estoque > Requisição > Requisições`

---

## 6. iFrames — Regra Mais Importante

**Todo o conteúdo útil do Innovaro está dentro de iframes.** Cada aba aberta tem o seu próprio iframe com classe `tab-frame`. Interações com formulários, grids e botões dentro de telas do ERP precisam ser feitas no contexto do iframe ativo.

```python
def obter_frame_ativo(page):
    """Retorna o frame do último iframe da página (iframe da aba ativa)."""
    iframe = page.locator("iframe").last
    iframe.wait_for(timeout=30000)
    frame = iframe.content_frame()
    if frame is None:
        raise RuntimeError("Não foi possível acessar o iframe ativo do Innovaro.")
    return frame

# Uso:
frame = obter_frame_ativo(page)
frame.locator("#grSimulacoes").wait_for(timeout=15000)
```

**Em JS:**
```js
async function waitForErpFrame(page) {
    const iframeLocator = page.locator("iframe").last();
    await iframeLocator.waitFor({ timeout: 30000 });
    const frameHandle = await iframeLocator.elementHandle();
    const frame = await frameHandle?.contentFrame();
    if (!frame) throw new Error("Não foi possível acessar o iframe ativo.");
    return frame;
}
```

### Seletor `frame_locator` (alternativa mais simples)

Para operações pontuais, `frame_locator` é mais conciso:

```python
frame = page.frame_locator("iframe")
frame.locator("input").first.fill("valor")
```

### Atenção: múltiplos iframes

O Innovaro pode ter vários iframes na página (um por aba aberta). Sempre use `.last` para pegar o iframe da aba atualmente ativa. Se precisar de uma aba específica, feche as demais antes.

---

## 7. Aguardar o ERP Processar

O ERP exibe overlays e mensagens enquanto processa ("Explodindo", "Gravando", "Carregando", etc.). Nunca prossiga sem aguardar esses estados terminarem.

```python
import re
import time

FASES_OCUPADO = re.compile(
    r"Explodindo|Gravando|Criando n[ií]vel|Carregando|Processando",
    re.IGNORECASE
)

def erp_esta_ocupado(page) -> bool:
    """Retorna True se o ERP ainda está processando."""
    try:
        frame = obter_frame_ativo(page)
    except Exception:
        return False

    return frame.evaluate("""() => {
        const body = document.body?.innerText || "";
        const pattern = /Explodindo|Gravando|Criando n[ií]vel|Carregando|Processando/i;
        const busyText = pattern.test(body);
        const busyOverlay = !!document.querySelector("#progressMessageBox, #statusMessageBox, [role='progressbar']");
        const busyDialog = !!document.querySelector(".wf-progress-dialog__content");
        return busyText || busyOverlay || busyDialog;
    }""")

def aguardar_erp_pronto(page, wait_ms: int = 2500, timeout_ms: int = 180000):
    """Aguarda o ERP sair de qualquer estado de processamento."""
    page.wait_for_load_state("domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass

    inicio = time.time()
    while (time.time() - inicio) * 1000 < timeout_ms:
        if not erp_esta_ocupado(page):
            page.wait_for_timeout(wait_ms)
            return
        page.wait_for_timeout(1500)

    raise TimeoutError("ERP não terminou de processar dentro do tempo esperado.")
```

---

## 8. Preencher Campos por Rótulo (Label)

O Innovaro usa tabelas HTML para layout de formulário. O padrão é: a célula com o rótulo fica na esquerda, o input fica na célula imediatamente à direita, na mesma linha (`<tr>`).

```python
def preencher_campo_por_rotulo(page, rotulo: str, valor: str, pressionar_tab: bool = False):
    """Encontra o input associado a um rótulo e preenche o valor."""
    token = f"codex-{int(time.time())}"
    frame = obter_frame_ativo(page)

    selector = frame.evaluate(f"""(args) => {{
        const normalize = (v) => String(v || "")
            .normalize("NFD").replace(/[\\u0300-\\u036f]/g, "")
            .replace(/\\s+/g, " ").trim().toUpperCase();

        const expected = normalize(args.label);
        for (const node of document.querySelectorAll("td, label, span, div")) {{
            if (normalize(node.textContent) !== expected) continue;
            const row = node.closest("tr");
            if (!row) continue;
            const cells = [...row.children];
            const idx = cells.findIndex(c => c === node || c.contains(node));
            for (let i = idx + 1; i < cells.length; i++) {{
                const input = cells[i].querySelector("input:not([type='hidden'])");
                if (!input) continue;
                input.setAttribute("data-target", args.token);
                return `[data-target="${{args.token}}"]`;
            }}
        }}
        throw new Error(`Campo para rótulo "${{args.label}}" não encontrado.`);
    }}""", {"label": rotulo, "token": token})

    input_el = frame.locator(selector)
    input_el.wait_for(timeout=30000)
    input_el.click()
    input_el.press("Control+A")
    input_el.type(valor)
    input_el.press("Tab" if pressionar_tab else "Enter")
    aguardar_erp_pronto(page, wait_ms=2000)
```

---

## 9. O Atalho "h" para Data Atual

O Innovaro interpreta a letra `"h"` digitada em campos de data como "hoje". É o modo padrão de preencher a data atual.

```python
# Preencher campo com data de hoje
preencher_campo_por_rotulo(page, "Data Base", "h", pressionar_tab=True)
preencher_campo_por_rotulo(page, "Emissão final", "h", pressionar_tab=True)
```

---

## 10. Interações com Grids

Grids do Innovaro têm IDs específicos (ex: `#grSimulacoes`, `#grEspecificacao`). O framework expõe o objeto `window.Environment` para acesso programático.

### Pesquisar em grid via framework interno:

```js
// JS — buscar por nome na grade de simulações
await frame.evaluate((nome) => {
    const proc = window.Environment?.getInstance?.().currentProcess;
    const grid = proc?.getGrid?.("grSimulacoes");
    if (!grid) throw new Error("Grid grSimulacoes não encontrada.");
    grid.emit("search", {
        gridName: "grSimulacoes",
        fieldName: "NOME",
        searchValue: nome,
        allFields: false,
        preventDefault: false
    });
}, simulationName);
```

### Clicar em elementos dentro de grids (via evaluate):

Elementos dentro do grid frequentemente precisam de eventos manuais para responder corretamente:

```js
function clickElement(element) {
    if (!element) return false;
    element.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
    element.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    element.dispatchEvent(new MouseEvent("mouseup",   { bubbles: true }));
    element.dispatchEvent(new MouseEvent("click",     { bubbles: true }));
    return true;
}
```

---

## 11. Exportar para CSV

O fluxo padrão de exportação tem os seguintes passos:

```python
def exportar_csv(page):
    # 1. Clicar no botão Exportar principal
    page.get_by_role("button", name="Exportar", exact=True).click()
    page.wait_for_timeout(1500)

    # 2. Escolher formato CSV
    page.get_by_text("Exportar para CSV", exact=True).click()
    page.wait_for_timeout(500)

    # 3. Clicar em Continuar
    page.get_by_role("button", name="Continuar", exact=True).click()
    page.wait_for_timeout(3000)

    # 4. Ajustar encoding (se aparecer o seletor)
    try:
        frame = page.frame_locator("iframe")
        select = frame.locator("select").first
        if select.count():
            select.select_option(label="UTF-8")
            page.wait_for_timeout(500)
    except Exception:
        pass

    # 5. Clicar no botão Exportar final
    page.get_by_role("button", name="Exportar", exact=True).click()

    # 6. Se o download não disparar, clicar em "clique aqui"
    try:
        page.get_by_text("clique aqui").click(timeout=5000)
    except Exception:
        pass
```

### Padrão de fallback (seletor → atalho → coordenada):

Botões do ERP às vezes não respondem a seletores normais. Use essa sequência de fallback:

```python
def clicar_exportar(page):
    candidatos = [
        page.get_by_role("button", name="Exportar", exact=True),
        page.get_by_text("Exportar", exact=True),
        page.locator("button").filter(has_text="Exportar"),
    ]
    for loc in candidatos:
        try:
            if loc.count():
                loc.first.click(timeout=10000)
                return
        except Exception:
            continue

    # Fallback 1: atalho de teclado
    try:
        page.keyboard.press("Alt+X")
        page.wait_for_timeout(1000)
        if page.get_by_text("Exportar para CSV", exact=True).count():
            return
    except Exception:
        pass

    # Fallback 2: coordenada (último recurso)
    page.mouse.click(190, 64)
```

---

## 12. Detectar e Aguardar Download

Após exportar, aguarde um arquivo novo aparecer na pasta Downloads:

```python
from pathlib import Path
import time

def aguardar_download_novo(download_dir: Path, referencia_mtime: float, timeout: int = 60) -> Path | None:
    limite = time.time() + timeout
    while time.time() < limite:
        arquivos = [p for p in download_dir.glob("*") if p.is_file()]
        if arquivos:
            mais_recente = max(arquivos, key=lambda p: p.stat().st_mtime)
            if mais_recente.stat().st_mtime > referencia_mtime:
                return mais_recente
        time.sleep(1)
    return None

# Uso:
downloads = Path.home() / "Downloads"
ref = max((p for p in downloads.glob("*") if p.is_file()), key=lambda p: p.stat().st_mtime, default=None)
ref_mtime = ref.stat().st_mtime if ref else 0

exportar_csv(page)

arquivo = aguardar_download_novo(downloads, ref_mtime)
if arquivo is None:
    raise RuntimeError("Nenhum download detectado após exportação.")
```

---

## 13. Tratar CSVs do Innovaro

Os CSVs exportados pelo Innovaro têm particularidades:

- Separador: ponto e vírgula (`;`)
- Strings protegidas: colunas de código vêm no formato `="valor"` (proteção Excel)
- Encoding: pode variar (UTF-8, latin-1, UTF-8-BOM)
- Números: separador decimal é vírgula (`,`), milhar é ponto (`.`)

```python
import pandas as pd

def ler_csv_innovaro(caminho: str) -> pd.DataFrame:
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            df = pd.read_csv(caminho, sep=";", encoding=enc)
            break
        except UnicodeDecodeError:
            continue

    # Remover ="..." e aspas de todas as colunas e valores
    df.rename(columns=lambda c: c.replace('="', '').replace('"', '').strip(), inplace=True)
    df = df.map(lambda x: str(x).replace('="', '').replace('"', '') if isinstance(x, str) else x)

    return df

def converter_numero_br(valor: str) -> float:
    """Converte número no formato brasileiro (1.234,56) para float."""
    return float(str(valor).replace(".", "").replace(",", "."))
```

---

## 14. Fechar Abas Extras (Duplicação)

Ao navegar para algumas telas, o Innovaro pode abrir uma aba de "Duplicação de requisições" ou similar. Sempre feche essas abas extras antes de interagir.

```python
def fechar_aba_duplicacao(page):
    try:
        botao = page.get_by_role("button", name="Fechar", exact=True)
        if botao.is_visible(timeout=3000):
            botao.click()
            page.wait_for_timeout(1500)
    except Exception:
        pass
```

---

## 15. Polling com Condição no Frame

Para aguardar um elemento aparecer dentro do iframe sem usar `wait_for` diretamente (útil para condições complexas):

```python
def aguardar_condicao_no_frame(page, js_condition: str, timeout_ms: int = 30000, poll_ms: int = 500):
    """js_condition é uma expressão JS que retorna true/false dentro do iframe."""
    inicio = time.time()
    while (time.time() - inicio) * 1000 < timeout_ms:
        frame = obter_frame_ativo(page)
        ok = frame.evaluate(f"() => {{ try {{ return !!({js_condition}); }} catch(e) {{ return false; }} }}")
        if ok:
            return
        page.wait_for_timeout(poll_ms)
    raise TimeoutError(f"Condição não satisfeita: {js_condition}")

# Exemplo: aguardar grid aparecer
aguardar_condicao_no_frame(page, "document.querySelector('#grEspecificacao')")
```

---

## 16. Screenshots para Debug

Sempre salve screenshot ao capturar erros e em pontos críticos do fluxo:

```python
import os
from pathlib import Path
from datetime import datetime

OUTPUT_DIR = Path("output/screenshots")

def capturar_screenshot(page, nome: str):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    caminho = OUTPUT_DIR / f"{ts}_{nome}.png"
    page.screenshot(path=str(caminho), full_page=True)
    return caminho

# Em blocos try/except:
try:
    # ... automação
except Exception as e:
    capturar_screenshot(page, "erro")
    raise
```

---

## 17. Variáveis de Ambiente

Nunca deixe credenciais no código. Use `.env`:

```
# .env
INNOVARO_URL=http://192.168.3.140/sistema
INNOVARO_USERNAME=Ti.cemag
INNOVARO_PASSWORD=SUA_SENHA
INNOVARO_DEFAULT_WAIT_MS=2500
INNOVARO_SLOW_MO_MS=250
INNOVARO_PHASE_TIMEOUT_MS=180000
```

```python
from dotenv import load_dotenv
load_dotenv()
url = os.getenv("INNOVARO_URL")
```

---

## 18. Estrutura Recomendada de Projeto

```
minha_automacao/
├── .env                  # Credenciais e configurações (não commitar)
├── .env.example          # Exemplo sem valores reais
├── .gitignore
├── requirements.txt
├── main.py               # Ponto de entrada
├── erp/
│   ├── __init__.py
│   ├── browser.py        # launch_browser(), login()
│   ├── menu.py           # abrir_menu_principal(), navegar para módulos
│   ├── frame.py          # obter_frame_ativo(), aguardar_erp_pronto()
│   └── export.py         # exportar_csv(), aguardar_download_novo()
├── data/
│   └── processamento.py  # ler_csv_innovaro(), converter_numero_br()
└── output/
    └── screenshots/      # Screenshots gerados pela automação
```

---

## 19. Formulário de Variáveis de Relatório (wf-grid virtual)

> **Descoberto na automação `conferencia_pedido` (maio/2026)**
>
> Esta seção é CRÍTICA. Economiza horas de debugging.

### O problema

O formulário de variáveis/filtros de **relatórios** do Innovaro usa um componente `wf-grid` com **renderização virtual**. Ele parece um formulário HTML normal, mas:

- `document.querySelectorAll('input, select')` retorna **0 elementos**
- `page.frame_locator("iframe").get_by_role("combobox")` retorna **0 resultados**
- `elementFromPoint(x, y)` sempre retorna o elemento `wf-grid` pai
- `page.get_by_text("Aprovados")` não encontra nada
- Clicks por coordenada (`page.mouse.click`) não abrem dropdowns
- `dispatchEvent` no elemento não funciona

**Por quê:** o `wf-grid` renderiza os campos internamente via JavaScript do `window.Environment` da janela pai, sem criar elementos HTML no DOM do iframe. O iframe tem apenas 17 elementos (scaffolding) — o conteúdo visual é pintado pelo framework.

### Como identificar se é wf-grid virtual

```python
frame_handle = page.locator("iframe").last.element_handle()
frame = frame_handle.content_frame()
count = frame.evaluate("() => document.querySelectorAll('*').length")
# Se count <= 20 e o form está visível: é wf-grid virtual.
```

### A solução: grid.updateCellValue()

O grid É acessível via `window.Environment` da página principal. Use `updateCellValue` para setar qualquer campo:

```python
def setar_campo_vars(page, nome_campo: str, valor: str):
    """Define um campo do formulário de variáveis via API JavaScript do Innovaro."""
    page.evaluate(
        """([nome, val]) => {
            const env = window.Environment?.getInstance?.();
            const grid = env?.currentProcess?.getGrid?.('vars');
            if (!grid) throw new Error('grid vars nao encontrado');
            grid.updateCellValue({
                gridName: 'vars',
                fieldName: nome,
                value: val,
                displayValue: val
            });
        }""",
        [nome_campo, valor],
    )
    page.wait_for_timeout(300)
```

### Como descobrir os nomes dos campos

Os campos seguem o padrão `dsvfilter_PED_<NOME>`. Para descobri-los, faça um patch no `grid.emit` e navegue com Tab:

```python
# 1. Patch do emit para logar eventos
page.evaluate("""() => {
    const env = window.Environment?.getInstance?.();
    const grid = env?.currentProcess?.getGrid?.('vars');
    const orig = grid.emit.bind(grid);
    grid.emit = function(ev, data) {
        console.log('EV:' + ev + ':' + JSON.stringify(data || {}));
        return orig(ev, data);
    };
}""")

# 2. Capturar logs do console
logs = []
page.on("console", lambda m: logs.append(m.text) if m.text.startswith("EV:") else None)

# 3. Dar foco ao grid e navegar com Tab
page.evaluate("() => { window.Environment?.getInstance?.()?.currentProcess?.getGrid?.('vars')?.focus?.(); }")
for _ in range(20):
    page.keyboard.press("Tab")
    page.wait_for_timeout(300)

# Logs terão: EV:fieldFocus:{"fieldName":"dsvfilter_PED_EMISSAOMOV_START",...}
```

Campos que têm `hasFocusEvents: False` (como Aprovação, Classificação) não aparecem no Tab mas EXISTEM. Descobri-los via linked list:

```python
fields = page.evaluate("""() => {
    const env = window.Environment?.getInstance?.();
    const grid = env?.currentProcess?.getGrid?.('vars');
    // Varrer a linked list: field.nextField / field.priorField
    let cur = grid.field('dsvfilter_PED_XAPROVACAO'); // campo conhecido como ponto de partida
    while (cur.priorField) cur = cur.priorField;       // ir ao início
    const result = [];
    while (cur) {
        result.push({ name: cur.name, label: cur.label, type: cur.type });
        cur = cur.nextField;
    }
    return result;
}""")
```

### Campos mapeados — Relatório de Pendência (Venda > Consultas)

| Label no formulário | Nome interno | Tipo |
|---|---|---|
| Aprovação | `dsvfilter_PED_XAPROVACAO` | combo |
| Classificação | `dsvfilter_PED_CLASSE` | memo |
| Classe Recurso | `dsvfilter_PED_RECURSO.CLASSE` | memo |
| Recurso | `dsvfilter_PED_RECURSO` | memo |
| Início | `dsvfilter_PED_EMISSAOMOV_START` | date |
| Fim | `dsvfilter_PED_EMISSAOMOV_END` | date |
| Previsão Emissão Doc. (Início) | `dsvfilter_PED_PREVISAOEMISSAODOC_START` | date |
| Previsão Emissão Doc. (Fim) | `dsvfilter_PED_PREVISAOEMISSAODOC_END` | date |
| Prog Entrega (Início) | `dsvfilter_PED_PROGRAMACA_START` | date |
| Prog Entrega (Fim) | `dsvfilter_PED_PROGRAMACA_END` | date |
| Baixa | `dsvfilter_PED_XBAIXA` | combo |
| Cancelamento de saldo | `dsvfilter_PED_XCANCELAMENTOSALDO` | combo |

### Outros métodos úteis do grid

```python
# Verificar se um campo existe
page.evaluate("() => window.Environment?.getInstance?.()?.currentProcess?.getGrid?.('vars')?.hasField?.('dsvfilter_PED_XAPROVACAO')")

# Focar um campo (ativa modo de edição visual)
page.evaluate("() => window.Environment?.getInstance?.()?.currentProcess?.getGrid?.('vars')?.focusField?.('dsvfilter_PED_XAPROVACAO')")

# Dar foco ao grid para aceitar Tab navigation
page.evaluate("() => window.Environment?.getInstance?.()?.currentProcess?.getGrid?.('vars')?.focus?.()")
```

### O que NÃO funciona para wf-grid virtual

- `page.frame_locator("iframe").locator("input")` — 0 resultados
- `page.frame_locator("iframe").get_by_text("Aprovados")` — 0 resultados
- `page.mouse.click(x, y)` por coordenada — não abre dropdowns
- `frame.evaluate("() => document.querySelectorAll('input')")` — array vazio
- `dispatchEvent(new MouseEvent(...))` no wf-grid — sem efeito
- `grid.openLookup(fieldName)` — requer elemento DOM que não existe
- `grid.emit('fieldChange', ...)` — só efeito visual temporário, não persiste

---

## 21. Tela de Requisições — Grids, Botões e Alternância de Modo

> **Descoberto na automação de Requisições (maio/2026)**

### Navegação até a tela

```
Estoque > Requisição > Requisições
```

Cuidado: no submenu há **dois itens "Requisições"** — um com ícone `dashboard` (módulo principal) e outro com `description_plus` (relatório). Usar sempre o de ícone `dashboard`.

### Nomes das grids

| Grid | Nome interno |
|---|---|
| Requisições | `grdRequisicoes` |
| Movimentações de Depósito | `grdMovDeposito` |

Descoberto via `iframe.contentDocument.querySelectorAll('[data-grid-name]')`.

### Botões de ação visíveis na toolbar da grdRequisicoes

| Botão | Ação |
|---|---|
| Aprova | Aprovar requisição |
| Desaprova | Desaprovar requisição |
| Baixar | Baixar requisição |
| Cancelar | Cancelar requisição |
| Descancelar | Descancelar requisição |

### Capacidades da grid (via `window.Environment`)

```python
grid = proc.getGrid('grdRequisicoes')
# grid.canInsert = True
# grid.canDelete = True
# grid.canConfirm = True
# grid.canDuplicate = True
# grid.canToggleKey = True
# grid.hasFormView = True
# grid.hasTableView = True
# grid.canExport = False  # exportação não disponível nesta tela
```

### Alternar modo de visão (tabela ↔ formulário)

O botão "Alternar modo de visão da grade" **não existe no DOM** — é renderizado virtualmente. Usar o método `toggleView()` da grid:

```python
page.evaluate("""() => {
    const env = window.Environment.getInstance();
    const grid = env.currentProcess.getGrid('grdRequisicoes');
    grid.toggleView();
}""")
```

Para verificar/forçar modo específico:

```python
# Verificar modo atual
mode = page.evaluate("""() => {
    const grid = window.Environment.getInstance().currentProcess.getGrid('grdRequisicoes');
    return { viewMode: grid.viewMode, inFormView: grid.inFormView };
}""")
# viewMode: "TABLE_VIEW" ou "FORM_VIEW"
# inFormView: True/False

# Garantir modo formulário
page.evaluate("""() => {
    const grid = window.Environment.getInstance().currentProcess.getGrid('grdRequisicoes');
    if (!grid.inFormView) grid.toggleView();
}""")

# Garantir modo tabela
page.evaluate("""() => {
    const grid = window.Environment.getInstance().currentProcess.getGrid('grdRequisicoes');
    if (grid.inFormView) grid.toggleView();
}""")
```

### Campos visíveis no modo formulário

O botão "Alternar modo de visão da grade" tem `aria-label="Alternar modo de visão da grade"` e está dentro da `wf-overlay` no iframe — mas usar `grid.toggleView()` via API é mais confiável.

### Mapeamento completo de campos da grdRequisicoes

Descoberto via linked list `field.nextField` a partir de campo conhecido.

| Nome interno | Label no ERP | Tipo | readOnly |
|---|---|---|---|
| `CHAVE` | Chave | integer | ✅ sim |
| `CLASSE` | Classe | integer | não |
| `REQUISITAN` | Requisitante | integer | não |
| `CCUSTRES` | C Custos Resultados | integer | não |
| `RECURSO` | Recurso | integer | não |
| `PACK` | Pack | integer | não |
| `UNIDMEDIDA` | UM | string | ✅ sim |
| `QUANTIDADE` | Quantidade | number | não |
| `OBSERVACAO` | Observação | memo | não |
| `EMISSAO` | Emissão | date | não |
| `EMISSAOH` | Hora | string | não |
| `PROGRAMACA` | Programação | date | não |
| `LOTE` | Lote | integer | não |
| `ATIVOLOTE` | Lote Ativo | integer | não |
| `ETAPA` | Etapa | integer | não |
| `MOTIVO` | Motivo | memo | não |
| `APROVACAO` | Aprovação | date | ✅ sim |
| `APROVADOR` | Aprovador | integer | ✅ sim |
| `CANCELAMEN` | Cancelamento | date | ✅ sim |
| `CANCELADOR` | Cancelador | integer | ✅ sim |
| `USUARIO` | Usuário | integer | ✅ sim |
| `SUGCLAMOVD` | Sug Classe da Baixa | integer | não |
| `UNIMOVORIGEM` | Unimovorigem | integer | não |
| `UNIMOV` | Unimov | integer | não |
| `TAREFA` | Tarefa | integer | ✅ sim |

### Mapeamento banco → ERP (automação de requisições)

| Campo no ERP | Nome interno | Coluna no banco |
|---|---|---|
| Classe | `CLASSE` | `cr.nome` (classe_req) |
| Requisitante | `REQUISITAN` | `f.matricula` |
| C Custos Resultados | `CCUSTRES` | `cc.nome` |
| Recurso | `RECURSO` | `i.codigo` |
| Quantidade | `QUANTIDADE` | `sr.quantidade` |

Campos `readOnly: true` **não devem** ser preenchidos via `updateCellValue` — o ERP ignora ou lança erro.

---

## 22. Preenchimento de Campos via Teclado — Método Correto para grdRequisicoes

> **Descoberto na automação de Requisições (maio/2026) — CRÍTICO**

### O problema com updateCellValue

`updateCellValue` com texto em campos tipo `integer` (lookup) exibe `[valor vazio]` porque o campo espera o ID interno do registro, não o texto. **Não usar para campos lookup.**

### A solução: Tab + digitação direta

O wf-grid captura eventos reais de teclado enviados via Playwright (`page.keyboard.press()`). O fluxo correto é:

1. **Acionar Insert** via `grid.emit('insert', { gridName: 'grdRequisicoes' })` — o ERP entra em modo inserção e foca o primeiro campo editável (Chave, readOnly).
2. **Tab** para mover o foco ao próximo campo.
3. **Digitar** os caracteres um a um via `page.keyboard.press('X')`.
4. O ERP faz autocomplete/lookup automaticamente pelo texto digitado.
5. **Tab** confirma o valor e avança ao próximo campo.

### Regra de ouro

> O foco do navegador deve estar no **iframe** para que `page.keyboard.press()` funcione. Garantir isso clicando no iframe antes de iniciar a sequência.

### Sequência de Tab para preencher os campos obrigatórios

```
Insert → [Chave: readOnly, auto]
Tab → Classe        → digitar texto + Tab
Tab → Requisitante  → digitar matrícula + Tab
Tab → C Custos Resultados → digitar texto + Tab
Tab → Recurso       → digitar código + Tab
Tab → Pack          → Tab (pular)
Tab → UM            → readOnly, auto-preenchido pelo Recurso
Tab → Quantidade    → digitar número + Tab
```

### Comportamento do autocomplete

- **Campos lookup (integer)**: ao digitar texto, o ERP resolve para o registro correspondente. Ex: digitar `"4610"` → resolve para `"4610 - Paulo Vinicius de Sousa Silva"`.
- **UM (Unidade de Medida)**: preenchido automaticamente quando o Recurso é confirmado. Não precisa digitar.
- **Chave**: gerada automaticamente pelo ERP no Insert. Não tocar.

### Exemplo Python (preencher um registro completo)

```python
from playwright.sync_api import Page

def preencher_requisicao(page: Page, classe: str, matricula: str, cc: str, recurso_codigo: str, quantidade: str):
    """Preenche um novo registro na grdRequisicoes via Tab + digitação."""
    
    # 1. Garantir foco no iframe
    page.locator("iframe").click()
    page.wait_for_timeout(300)

    # 2. Acionar Insert
    page.evaluate("""() => {
        const grid = window.Environment.getInstance().currentProcess.getGrid('grdRequisicoes');
        grid.emit('insert', { gridName: 'grdRequisicoes' });
    }""")
    page.wait_for_timeout(500)

    # 3. Ir para Classe (Tab sai do Chave readOnly)
    page.keyboard.press("Tab")
    page.wait_for_timeout(200)

    # 4. Classe
    page.keyboard.type(classe)   # ex: "Req p Consumo"
    page.keyboard.press("Tab")
    page.wait_for_timeout(300)

    # 5. Requisitante (matrícula)
    page.keyboard.type(matricula)   # ex: "4610"
    page.keyboard.press("Tab")
    page.wait_for_timeout(300)

    # 6. C Custos Resultados
    page.keyboard.type(cc)   # ex: "CC Cx"
    page.keyboard.press("Tab")
    page.wait_for_timeout(300)

    # 7. Recurso (código)
    page.keyboard.type(recurso_codigo)   # ex: "497878"
    page.keyboard.press("Tab")
    page.wait_for_timeout(300)

    # 8. Pular Pack → UM (auto)
    page.keyboard.press("Tab")  # Pack
    page.keyboard.press("Tab")  # UM (readOnly)
    page.wait_for_timeout(200)

    # 9. Quantidade
    page.keyboard.type(quantidade)   # ex: "50"
    page.keyboard.press("Tab")
    page.wait_for_timeout(300)
```

### Selecionar linha pelo checkbox (marcar/desmarcar)

O checkbox de seleção de linha é um elemento **virtual** do wf-grid — não existe no DOM. Eventos sintéticos (`dispatchEvent`) são ignorados porque não têm `isTrusted: true`.

**Único método que funciona:** `page.mouse.click(x, y)` via Playwright nativo.

```python
# Coordenadas do checkbox da primeira linha (viewport)
# Ajustar Y conforme posição da linha na grid
page.mouse.click(27, 205)   # x=27 (coluna checkbox), y=205 (primeira linha)
page.wait_for_timeout(400)

# Verificar se foi selecionado
allSelected = page.evaluate("""() => {
    const grid = window.Environment.getInstance().currentProcess.getGrid('grdRequisicoes');
    return grid.allSelected;
}""")
```

**Atenção:** `page.mouse.click()` NÃO está disponível via `page.evaluate()`. Usar diretamente na automação Python após obter o `page` do Playwright.

**Referência de coordenadas — grid em TABLE_VIEW:**
- Coluna do checkbox: x ≈ 27
- Primeira linha de dados: y ≈ 205 (viewport com toolbar do ERP em y=84)
- Cada linha subsequente: +22px aproximadamente

### Capturar a Chave gerada pelo ERP

A chave é gerada automaticamente no momento do `insert`. Ela DEVE ser lida logo após o `emit('insert')`, antes de qualquer `Gravar` — após gravar a grid fica vazia e `grid.field('CHAVE').value` retorna vazio.

```python
# Ler IMEDIATAMENTE após o insert
chave = page.evaluate("""() => {
    const grid = window.Environment?.getInstance?.()?.currentProcess?.getGrid?.('grdRequisicoes');
    return String(grid?.field?.('CHAVE')?.value ?? '');
}""")
# Guardar para atualizar o banco depois
```

### Confirmar e gravar o registro

Após preencher todos os campos, confirmar via botão na floating toolbar ou via `page.keyboard.press("Enter")`:

```python
# Opção 1: botão Confirmar na floating toolbar (dentro do iframe wf-overlay)
page.frame_locator("iframe").get_by_role("button", name="Confirmar edição").click()

# Opção 2: botão Gravar na page toolbar
page.get_by_role("button", name="Gravar").click()
```

### Obter a Chave gerada após gravação

```python
chave = page.evaluate("""() => {
    const grid = window.Environment.getInstance().currentProcess.getGrid('grdRequisicoes');
    return grid.field('CHAVE').value;
}""")
# Retorna o número da chave, ex: 104224430
```

---

## 20. Resumo de Padrões Críticos

| Situação | O que fazer |
|---|---|
| Interagir com formulários/grids comuns | Obter o iframe ativo antes de qualquer locator |
| Formulário de variáveis de relatório | Usar `grid.updateCellValue()` via `page.evaluate()` |
| ERP travado em "Carregando..." | `aguardar_erp_pronto()` com polling |
| Campo de data = hoje | Digitar `"h"` e pressionar Tab |
| Botão não responde | Tentar seletor → atalho de teclado (nunca coordenada) |
| Grid não clica corretamente | Usar `dispatchEvent` com mouseover+mousedown+mouseup+click |
| CSV com `="valor"` | Remover `="` e `"` no pós-processamento |
| Números como `"1.234,56"` | `replace(".", "").replace(",", ".")` antes de `float()` |
| Download não detectado | Aguardar arquivo novo na pasta Downloads por mtime |
| Aba extra aberta | Fechar com botão "Fechar" antes de prosseguir |
| Menu já está aberto | Verificar "Fechar menu" antes de clicar em "Menu" |
| 0 inputs no iframe mas form visível | wf-grid virtual — usar `grid.updateCellValue()` |
| Descobrir nomes de campos do wf-grid | Patch em `grid.emit` + Tab navigation + linked list |
