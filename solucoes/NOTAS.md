# Notas sobre as soluções

## O que está embutido no sintético (`gerar_sintetico_real.py`, seed 7)

| Fenômeno | Onde a aluna deve redescobrir |
|---|---|
| Arquivo inexistente: `azure-worker-00` em 06/09 | 00 (inventário), 01, 03 |
| Queda parcial: `polito-worker-00`, 11/09 00h–12h UTC | 00 (célula clara), 03 (faixa cinza) |
| Ruído SNMP de `10.79.0.0/16` → `polimi-worker-00` (imita o arquivo real) | 00 seção 4 |
| ~75% dos IPs com 1 só sensor; ~6% em todos, que geram ~63% dos pacotes de varredura | 01, 02 |
| Afinidade regional (BR↔BR, IT↔IT), com Jaccard e lift maiores nos pares regionais | 02 |
| Agregar IP → /24 aumenta a cobertura quase só mecanicamente (nulo ≈ real) | 02 seção 5 |
| 9 episódios de DDoS (backscatter SA/R/RA em todos os sensores, 20–90 min) | 03 (picos, z robusto), 06 |
| Botnet tipo Mirai (23/2323) com ciclo diário na **hora local da origem** (pico ~19h local) | 03 TODO 2 |
| Campanha nova na porta 2375 (Docker) a partir de 09/09 | 03 seção 6 e TODO 4 |
| Ferramentas: ZMap (ip_id 54321), Masscan (janela 1024, TTL 255), Linux/Windows (opções TCP) | 04 |
| ZMap e Masscan têm alcance maior (mais sensores por IP) | 04 TODO 2 |
| Saltos menores quando origem e sensor estão na mesma região | 04 TODO 3 |
| Ordem fixa de varredura (ufes → ifes → polimi-00 → polimi-01 → polito → azure) em 60% dos globais | 05 |
| Varreduras completas rápidas (segundos) × lentas (horas): Δt bimodal | 05 |
| 60 "pools" /24 (cada IP, um sensor; mesma janela de 2 dias); a regra com sincronia acha ~58 | 06 |
| 2 organizações de "varredura acadêmica" (40 IPs cada, todos os dias, todos os sensores, milhares de portas) | 01, 06 TODO 4 |

## Pontos de discussão sugeridos

- **00:** a diferença entre dia sem arquivo (`NaN`) e dia sem tráfego (0). Por que origens privadas aparecem num sensor.
- **02:** "parece interessante" × "qual seria o valor ao acaso?". O modelo nulo é a ideia central da série.
- **03:** a correlação horária é inflada por poucos picos (Pearson × Spearman).
- **04:** as regras de ferramenta são heurísticas. A combinação "outro + tipo Mirai" do TODO 4 é um artefato da
  regra, não do comportamento.
- **05:** o nulo depende de escolhas (dentro do dia × período inteiro). Um erro de relógio de 2 min inverte
  conclusões sobre varreduras rápidas.
- **06:** censura (ingênuo × Kaplan–Meier). A extrapolação 2³²/N com N ≈ 1.500 tem erro enorme. A regra 1 do
  TODO 4 acha ~200 ASNs, o mesmo efeito de tamanho do notebook 02.

## Ao trocar para os dados reais

- Conferir o padrão de nome dos arquivos (`hio.PADROES`) e o fuso: o leitor assume epoch em ns UTC.
- No 04, as regras de ferramenta precisam ser revistas com a literatura (ex.: Masscan real e opções TCP).
- No 06, `N` (endereços monitorados) deve vir da configuração real dos sensores, não de `dst_ip.nunique()`.
- O arquivo real de teste (`polimi-worker-00-2026-08.parquet`) é mensal, cobre só de 07 a 10/08 e tem 100% de
  SNMP interno. Antes de rodar a série com dados reais, vale confirmar como os parquet são extraídos.
