# 🤖 Infinity Bot — Respostas Automáticas Mercado Livre

Bot de respostas automáticas para a loja INFINITY AUTO PARTS.

## Variáveis de Ambiente (configurar no Railway)

| Variável         | Descrição                              |
|-----------------|----------------------------------------|
| `MAC_API_KEY`   | Chave da MAC API (marketplace connect) |
| `CLAUDE_API_KEY`| Chave da API do Claude (Anthropic)     |
| `INTERVALO_SEG` | Intervalo entre verificações (padrão: 30) |

## Como fazer deploy no Railway

1. Faça upload desta pasta para o GitHub
2. Acesse railway.app e crie um novo projeto
3. Conecte o repositório GitHub
4. Adicione as variáveis de ambiente acima
5. Deploy automático!

## Como funciona

- A cada `INTERVALO_SEG` segundos, o bot verifica novas perguntas no ML
- O Claude analisa cada pergunta comparando com o anúncio
- Se tiver certeza → responde automaticamente
- Se não tiver certeza → deixa para resposta manual
