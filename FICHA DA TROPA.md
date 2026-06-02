# 📋 FICHA DA TROPA — Manual da Operação

> Bem-vindo(a)! Este é o manual da nossa operação automatizada.
> Você **não precisa ser programador(a)** pra operar isto. Leia com calma, na ordem.
> Qualquer dúvida, é só perguntar pro Claude (nosso parceiro de IA) colando o print do que apareceu.

---

## 0. O que é tudo isso, em uma frase

Vendemos autopeças em **4 lojas no Mercado Livre + 1 na Shopee**. Como é muita coisa repetitiva (responder cliente, arrumar anúncio, etc.), montamos uma **"tropa" de robôs** que fazem esse trabalho sozinhos, 24 horas por dia. Este manual explica quem é cada robô e como cuidar deles.

---

## 1. Como tudo se conecta (a visão geral)

Pensa numa linha de montagem com 4 estações:

1. **GitHub** = a *estante de manuais*. É onde mora o código de cada robô (os arquivos `.py`). 
   - Endereço: `github.com/carloszagallo/infinity-automacao`
2. **Railway** = a *fábrica*. É onde os robôs ficam **ligados e rodando** o dia todo. Cada robô é um "serviço" lá dentro.
   - Projeto: `innovative-amazement`
3. **Tiops (Marketplace Connect)** = o *crachá de acesso*. É a ponte que deixa os robôs entrarem nas lojas. 
   - Endereço: `marketplaces.tiops.com.br`
4. **Mercado Livre / Shopee** = as *lojas* de verdade, onde os clientes compram.

➡️ Resumo do fluxo: **mexemos no código no GitHub → o Railway pega e roda → o robô usa o crachá da Tiops → e age nas lojas.**

Detalhe-chave: quando a gente **salva uma mudança no GitHub**, o Railway percebe sozinho e **religa o robô com a versão nova**. Não precisa "mandar" pro Railway — ele puxa automático.

---

## 2. A Tropa (quem é cada robô)

| Cargo | Arquivo (no GitHub) | Serviço (no Railway) | O que faz | Quando trabalha |
|-------|---------------------|----------------------|-----------|-----------------|
| **Atendente** | `bot.py` | ATENDENTE_PROMOS | Responde perguntas de clientes, responde avaliações e ativa promoções (até 7%) | A cada 30 segundos |
| **Corretor/Faxineiro** | `funcionario_digital.py` | CORRETOR_FAXINEIRO | Conserta a "ficha" dos anúncios: garantia 90 dias, INMETRO, origem, códigos OEM/peça, e avisa se tem "original" no título | Em varreduras |
| **Fichário** | `ficha_tecnica.py` | FICHÁRIO | Preenche a ficha técnica dos anúncios mais fracos (saúde < 0,8) | Uma vez por semana |

**Explicando cada um como se fosse gente:**

- **O Atendente** é o que fala com o cliente. Se chega uma pergunta ("tem frete grátis?", "é nova?"), ele responde na hora, educadamente, usando os dados do anúncio. Se a pergunta for sobre algo que ele NÃO tem certeza (ex.: "serve no meu carro 2008?"), ele **não inventa** — ele fica quieto e deixa pra gente responder.
- **O Corretor/Faxineiro** é o organizado. Ele passa nos anúncios arrumando os campos que estão errados ou vazios (garantia, país de origem, códigos da peça). Não mexe em preço nem em foto — só ajeita a papelada.
- **O Fichário** preenche a ficha técnica (marca, modelo, ano que serve...) dos anúncios que estão com a ficha incompleta, pra eles aparecerem melhor na busca.

---

## 3. As lojas que cuidamos

| Loja | Onde | Número (ID) |
|------|------|-------------|
| INFINITY AUTOPARTS | Mercado Livre | 60771984 |
| FREEDOM (FreePartsSC) | Mercado Livre | 233798434 |
| AUTOPARTSLIBERTY | Mercado Livre | 554248644 |
| DESTINYAUTOPARTS | Mercado Livre | 1994875400 |
| Liberty | Shopee | 1242997946 |

Todos os produtos são **novos** e **paralelos** (não originais), salvo quando marcado o contrário.

---

## 4. Tarefas do dia a dia (passo a passo)

### ✅ Ver se um robô está trabalhando
1. Entra no Railway (projeto `innovative-amazement`).
2. Clica no serviço (ex.: ATENDENTE_PROMOS).
3. Aba **"Deploy Logs"** → você vê o robô "falando" (ex.: "Respondida", "Aguardando 30s...").
   - **Verde / "Active"** = trabalhando. **Vermelho / "Crashed"** = caiu (ver seção 5).

### ✅ Atualizar um robô (mudar o código)
1. Abre o arquivo no GitHub (ex.: `bot.py`).
2. Clica no lápis ✏️ pra editar.
3. **REGRA DE OURO:** quando o Claude te manda um código, copia ele pelo **botãozinho de copiar** no canto do bloco — **NUNCA** selecionando com o mouse (o mouse corta o texto no meio e quebra tudo).
4. No GitHub: seleciona tudo (Ctrl/Cmd + A) → apaga → cola.
5. **Confere se veio inteiro** (a última linha geralmente é `main()`). 
6. Salva ("Commit changes"). O Railway religa o robô sozinho em ~1 minuto.

### ✅ Ligar / desligar um robô
- No Railway, no serviço, dá pra pausar/reativar. Mas no dia a dia não precisa mexer.

---

## 5. Quando algo dá errado (socorro!)

> **Regra número 1: não entra em pânico, e não sai mexendo no código.** Tira print e mostra pro Claude.

| Sintoma | O que provavelmente é | O que fazer |
|---------|----------------------|-------------|
| Robô **vermelho / "Crashed"** | Tem um erro no código | Abre o Deploy Log, rola até o fim, acha o texto vermelho (ex.: `SyntaxError`), tira print e manda pro Claude |
| Erro **"SyntaxError" / arquivo cortado** | O copia-e-cola cortou o arquivo no meio | Refaz a colagem usando o **botão de copiar** e confere que veio até o fim |
| **"Network is unreachable"** ao mandar e-mail | O Railway bloqueia e-mail comum | Já é esperado; usamos o serviço "Resend" pra e-mail. Não é falha grave |
| Contas **desconectadas** na Tiops | A conexão das contas extras caiu | Reconecta no painel da Tiops. ⚠️ Isso NÃO deveria acontecer todo dia — se acontecer, avisa, é problema deles |
| **"Sem crédito / plano free"** | A API da Tiops às vezes não enxerga nosso plano | Confere no painel Tiops → "Plano & créditos". **O painel é a verdade.** Se lá diz "Ativo", o plano está pago |

---

## 6. Dicionário (palavras difíceis, em português claro)

- **Script / código:** as instruções que o robô segue. Arquivos que terminam em `.py`.
- **Deploy:** quando o Railway "publica" e liga a versão nova do robô.
- **Crash:** o robô caiu/travou por causa de um erro.
- **Log:** o "diário" do robô — onde ele escreve o que está fazendo.
- **Commit:** salvar uma mudança no GitHub.
- **Variável de ambiente:** configurações e senhas que ficam guardadas no Railway (NÃO no código), tipo `MAC_API_KEY`. São secretas.
- **API:** o jeito de um sistema conversar com o outro automaticamente.
- **Token / chave:** uma "senha" que dá acesso. Nunca compartilhar com ninguém.

---

## 7. Combinados de ouro 🏅

1. **Testar em 1 antes de aplicar em tudo.** Sempre.
2. **Botão de copiar**, nunca o mouse, pra copiar código.
3. **Nada de original sem confirmar** — todo produto é paralelo até o Carlos dizer o contrário.
4. **Promoção:** no máximo 7% de desconto nosso.
5. **Senhas e chaves nunca saem daqui.** Não cola chave de API em lugar nenhum.
6. **Na dúvida, pergunta pro Claude** colando o print. É de graça e evita estrago.

---

## 8. A quem recorrer

- **Dúvida técnica / erro:** Claude (no chat), colando o print do problema.
- **Conta de loja (Mercado Livre/Shopee):** suporte do próprio marketplace.
- **Tiops:** (atenção: não tem canal de suporte direto — resolvemos pelo painel).
