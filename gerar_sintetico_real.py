#!/usr/bin/env python3
"""
gerar_sintetico_real.py
-----------------------
Gera dados FICTÍCIOS de darknet no MESMO FORMATO dos parquet reais do Holoscope:
um arquivo por sensor e por dia (`<sensor>-AAAA-MM-DD.parquet`), sem coluna de sensor,
`timestamp` em ns, IPs uint32, flags uint8 e ASN/geo em colunas struct.

    python gerar_sintetico_real.py                  # -> dados_sinteticos/
    python gerar_sintetico_real.py --dias 7 --saida outra_pasta

Tudo é inventado: blocos de destino em 198.18.0.0/15 (reservado para testes), ASNs na faixa
privada (64512+), nomes de organização fictícios. Os nomes dos sensores imitam o padrão real
(site-worker-NN) só para treinar o leitor.

Fenômenos embutidos (para a aluna REDESCOBRIR — não conte antes!):
  * visitantes únicos (1 sensor, 1 dia) são a maioria dos IPs;
  * afinidade regional: parte dos IPs visita só sensores do mesmo país;
  * varredores globais: visitam todos os sensores, alguns sempre na mesma ordem, rápido (segundos)
    ou devagar (horas);
  * "pools": várias máquinas de uma mesma /24 dividem o trabalho (cada IP vê 1 sensor);
  * duas organizações de "varredura acadêmica" ativas todos os dias, em todos os sensores;
  * botnet tipo Mirai (Telnet 23/2323) com ciclo diário na hora LOCAL das vítimas infectadas;
  * campanha nova numa porta (surge num dia e se espalha por vários sensores);
  * backscatter de ataques DDoS (SYN-ACK/RST) visto por todos os sensores ao mesmo tempo;
  * impressões digitais de ferramentas (ZMap, Masscan, Mirai, pilhas Linux/Windows);
  * PROBLEMAS DE COLETA: ruído SNMP de rede privada num sensor, um dia sem arquivo e
    meio dia de queda em outro sensor.
"""
from __future__ import annotations

import argparse
import os

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

SENSORES = {  # sensor -> (bloco /24 de destino, região)
    "ufes-worker-00":   ("198.18.10", "BR"),
    "ifes-worker-00":   ("198.18.20", "BR"),
    "polimi-worker-00": ("198.18.30", "IT"),
    "polimi-worker-01": ("198.18.31", "IT"),
    "polito-worker-00": ("198.18.40", "IT"),
    "azure-worker-00":  ("198.18.50", "NUVEM"),
}
ORDEM_GLOBAL = ["ufes-worker-00", "ifes-worker-00", "polimi-worker-00", "polimi-worker-01",
                "polito-worker-00", "azure-worker-00"]

PAISES = {  # iso: (nome, continente, lat, lon, peso)
    "CN": ("China", "Asia", 35.0, 105.0, 16), "US": ("United States", "North America", 39.0, -98.0, 15),
    "RU": ("Russia", "Europe", 61.0, 99.0, 6), "BR": ("Brazil", "South America", -10.0, -52.0, 7),
    "NL": ("Netherlands", "Europe", 52.3, 5.3, 6), "DE": ("Germany", "Europe", 51.0, 10.0, 5),
    "IN": ("India", "Asia", 21.0, 78.0, 6), "VN": ("Vietnam", "Asia", 16.0, 107.0, 4),
    "KR": ("South Korea", "Asia", 36.0, 128.0, 3), "GB": ("United Kingdom", "Europe", 54.0, -2.0, 3),
    "FR": ("France", "Europe", 46.0, 2.0, 2), "HK": ("Hong Kong", "Asia", 22.3, 114.2, 3),
    "SG": ("Singapore", "Asia", 1.35, 103.8, 2), "IR": ("Iran", "Asia", 32.0, 53.0, 2),
    "TW": ("Taiwan", "Asia", 23.7, 121.0, 2), "IT": ("Italy", "Europe", 42.8, 12.8, 3),
    "UA": ("Ukraine", "Europe", 49.0, 32.0, 2), "BG": ("Bulgaria", "Europe", 42.7, 25.5, 2),
    "ID": ("Indonesia", "Asia", -2.0, 118.0, 2), "TR": ("Turkey", "Asia", 39.0, 35.0, 1),
    "AR": ("Argentina", "South America", -34.0, -64.0, 1), "MX": ("Mexico", "North America", 23.0, -102.0, 1),
}
REGIAO_DO_CONT = {"South America": "BR", "Europe": "IT"}

APP = {21: "FTP", 22: "SSH", 23: "TELNET", 2323: "TELNET", 25: "SMTP", 53: "DNS", 80: "HTTP",
       8080: "HTTP", 8000: "HTTP", 81: "HTTP", 443: "TLS", 8443: "TLS", 445: "SMB", 3389: "RDP",
       123: "NTP", 161: "SNMP", 1900: "SSDP", 11211: "MEMCACHED", 5060: "SIP", 1433: "MSSQL",
       3306: "MYSQL", 6379: "REDIS", 2375: "DOCKER", 5900: "VNC", 9200: "ELASTICSEARCH",
       37215: "UNKNOWN", 52869: "UNKNOWN"}

PORTAS_TCP = np.array([22, 23, 80, 443, 445, 3389, 8080, 21, 25, 1433, 3306, 5900, 6379, 9200, 8443, 81, 8000, 2323])
PESO_TCP = np.array([18, 10, 12, 8, 9, 8, 7, 3, 3, 3, 3, 3, 3, 2, 2, 2, 2, 2], float)
PORTAS_UDP = np.array([53, 123, 161, 1900, 11211, 5060])
PESO_UDP = np.array([30, 20, 20, 15, 8, 7], float)

# ferramenta: ttl inicial, ip_id fixo (ou -1), janela (ou -1 = aleatória), opções, mss, wscale, sack, ts, df, tam
FERRAMENTAS = {
    "zmap":     (255, 54321, 65535, "",                         0,    0, False, False, False, 40),
    "masscan":  (255, -1,    1024,  "MSS",                      1460, 0, False, False, False, 44),
    "mirai":    (64,  -1,    -1,    "",                         0,    0, False, False, True,  40),
    "linux":    (64,  -1,    64240, "MSS,SACK,TS,NOP,WS",       1460, 7, True,  True,  True,  60),
    "windows":  (128, -1,    64240, "MSS,NOP,WS,NOP,NOP,SACK",  1460, 8, True,  False, True,  52),
}


def _mix64(x):
    """Hash inteiro (splitmix64) — usado para o flow_hash."""
    x = (x + np.uint64(0x9E3779B97F4A7C15)) & np.uint64(0xFFFFFFFFFFFFFFFF)
    x = ((x ^ (x >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)) & np.uint64(0xFFFFFFFFFFFFFFFF)
    x = ((x ^ (x >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)) & np.uint64(0xFFFFFFFFFFFFFFFF)
    return x ^ (x >> np.uint64(31))


def _ip_int(txt: str) -> int:
    a, b, c, d = map(int, txt.split("."))
    return (a << 24) | (b << 16) | (c << 8) | d


def gerar(inicio="2026-09-01", n_dias=14, seed=7, escala=1.0):
    rng = np.random.default_rng(seed)
    with np.errstate(over="ignore"):
        return _gerar(rng, pd.Timestamp(inicio, tz="UTC"), n_dias, escala)


def _gerar(rng, t0, n_dias, escala):
    sensores = list(SENSORES)
    reg_sensor = {s: SENSORES[s][1] for s in sensores}
    T = n_dias * 86400.0

    # ---------------- topologia: ASN -> país, org, rede; /24 dentro do ASN
    isos = list(PAISES)
    pp = np.array([PAISES[i][4] for i in isos], float); pp /= pp.sum()
    tipos_org = ["Hosting", "Telecom", "Cloud", "Broadband", "Mobile", "Datacenter"]
    p_tipo = [0.25, 0.2, 0.12, 0.28, 0.08, 0.07]
    n_asn = 450
    asn = pd.DataFrame({"asn": 64512 + np.arange(n_asn),
                        "pais": rng.choice(isos, size=n_asn, p=pp),
                        "tipo": rng.choice(tipos_org, size=n_asn, p=p_tipo)})
    asn["org"] = [f"{t} Exemplo {i:03d} ({p})" for i, (t, p) in enumerate(zip(asn.tipo, asn.pais))]
    proibidos = {0, 10, 100, 127, 169, 172, 192, 198, 203}
    oct1 = rng.choice([x for x in range(11, 224) if x not in proibidos], size=n_asn)
    oct2 = rng.integers(0, 256, size=n_asn)
    asn["rede16"] = (oct1 << 24) | (oct2 << 16)
    # 2 organizações de "varredura acadêmica" (fictícias)
    pesq = pd.DataFrame({"asn": [65400, 65401], "pais": ["US", "DE"], "tipo": ["Pesquisa", "Pesquisa"],
                         "org": ["Varredura Academica Exemplo A", "Varredura Academica Exemplo B"],
                         "rede16": [(45 << 24) | (83 << 16), (71 << 24) | (6 << 16)]})
    asn = pd.concat([asn, pesq], ignore_index=True).set_index("asn")

    w_asn = 1.0 / np.arange(1, n_asn + 1) ** 0.9; w_asn /= w_asn.sum()

    def novos_ips(n, asns=None, tipo_pref=None):
        if asns is None:
            if tipo_pref:
                cand = asn.index[(asn.tipo.isin(tipo_pref)) & (asn.index < 65400)].values
                asns = rng.choice(cand, size=n)
            else:
                asns = rng.choice(asn.index[:n_asn].values, size=n, p=w_asn)
        oct3 = rng.integers(0, 64, size=n)      # cada ASN usa só ~64 /24 do seu /16
        host = rng.integers(1, 255, size=n)
        ip = asn.loc[asns, "rede16"].values + (oct3 << 8) + host
        return pd.DataFrame({"ip": ip.astype(np.uint32), "asn": asns})

    # ---------------- atores
    atores = []

    def add(df, tipo, ferr, proto="TCP", portas=None, **kw):
        df = df.copy(); df["tipo"] = tipo; df["ferr"] = ferr; df["proto"] = proto
        df["portas"] = portas if portas is not None else [None] * len(df)
        for k, v in kw.items():
            df[k] = v
        atores.append(df)

    n = int(30000 * escala)
    base = novos_ips(n)
    tipo = rng.choice(["oneshot", "local", "global"], size=n, p=[0.68, 0.25, 0.07])
    # a ferramenta depende do comportamento: varredores globais usam mais ZMap/Masscan
    p_ferr = {"oneshot": [0.15, 0.15, 0.45, 0.25], "local": [0.25, 0.25, 0.35, 0.15],
              "global": [0.5, 0.35, 0.15, 0.0]}
    ferr = np.empty(n, dtype=object)
    for t, pf in p_ferr.items():
        m = tipo == t
        ferr[m] = rng.choice(["zmap", "masscan", "linux", "windows"], size=m.sum(), p=pf)
    proto = rng.choice(["TCP", "UDP", "ICMP"], size=n, p=[0.84, 0.13, 0.03])
    for t in ["oneshot", "local", "global"]:
        m = tipo == t
        add(base[m], t, ferr[m], proto[m])

    # pools /24: 60 redes, ~12 IPs cada, cada IP 1 sensor, mesma janela de 2 dias
    pools = []
    for k in range(int(60 * escala)):
        a = rng.choice(asn.index[(asn.tipo.isin(["Hosting", "Cloud", "Datacenter"])) & (asn.index < 65400)])
        rede = asn.loc[a, "rede16"] + (int(rng.integers(64, 128)) << 8)
        hosts = rng.choice(np.arange(1, 255), size=int(rng.integers(8, 20)), replace=False)
        d = pd.DataFrame({"ip": (rede + hosts).astype(np.uint32), "asn": a})
        d["dia_pool"] = int(rng.integers(0, max(n_dias - 2, 1)))
        d["porta_pool"] = int(rng.choice(PORTAS_TCP[:8]))
        pools.append(d)
    add(pd.concat(pools, ignore_index=True), "pool", "masscan")

    # varredura acadêmica: 2 orgs x 40 IPs, todos os dias, todos os sensores, muitas portas
    for a in (65400, 65401):
        d = novos_ips(40, asns=np.full(40, a))
        add(d, "pesquisa", "zmap")

    # botnet Mirai: IPs residenciais
    nm = int(5000 * escala)
    d = novos_ips(nm, tipo_pref=["Broadband", "Mobile"])
    add(d, "mirai", "mirai", "TCP", [None] * nm)

    # campanha nova (porta 2375, Docker) a partir do dia 9
    nc = int(800 * escala)
    d = novos_ips(nc, tipo_pref=["Hosting", "Cloud", "Datacenter"])
    add(d, "campanha", rng.choice(["masscan", "zmap", "linux"], size=nc, p=[0.5, 0.3, 0.2]))

    A = pd.concat(atores, ignore_index=True).drop_duplicates("ip").reset_index(drop=True)
    A["pais"] = asn.loc[A.asn, "pais"].values
    cont = A.pais.map(lambda p: PAISES[p][1])
    A["reg_origem"] = cont.map(REGIAO_DO_CONT).fillna("OUTRA")
    A["saltos0"] = rng.integers(9, 22, size=len(A))
    A["lon"] = A.pais.map(lambda p: PAISES[p][3])
    A["sport_fixa"] = rng.integers(32768, 61000, size=len(A))

    # ---------------- visitas (ator, sensor, instante inicial, nº de pacotes)
    V = {"ator": [], "sensor": [], "t": [], "n": []}

    def visita(i, s, t, npk):
        V["ator"].append(i); V["sensor"].append(s); V["t"].append(t); V["n"].append(npk)

    pesos_hora = 1 + 0.25 * np.sin((np.arange(24) - 9) / 24 * 2 * np.pi); pesos_hora /= pesos_hora.sum()
    pesos_hora_res = 1 + 0.9 * np.sin((np.arange(24) - 14) / 24 * 2 * np.pi); pesos_hora_res /= pesos_hora_res.sum()

    def t_dia(d, n=1):
        return d * 86400 + rng.choice(24, size=n, p=pesos_hora) * 3600 + rng.uniform(0, 3600, size=n)

    grupos_reg = {"BR": ["ufes-worker-00", "ifes-worker-00"],
                  "IT": ["polimi-worker-00", "polimi-worker-01", "polito-worker-00"]}

    for i, r in enumerate(A.itertuples(index=False)):
        tp = r.tipo
        if tp == "oneshot":
            visita(i, sensores[rng.integers(len(sensores))], t_dia(rng.integers(n_dias))[0], int(rng.integers(1, 4)))
        elif tp == "local":
            if r.reg_origem in grupos_reg and rng.random() < 0.7:     # afinidade com a região de origem
                g = grupos_reg[r.reg_origem]
            elif rng.random() < 0.5:
                g = grupos_reg[["BR", "IT"][rng.integers(2)]]
            else:
                g = sensores
            ss = rng.choice(g, size=min(int(rng.integers(1, 3)), len(g)), replace=False)
            for d in rng.choice(n_dias, size=int(rng.integers(2, 8)), replace=False):
                for s in ss:
                    visita(i, s, t_dia(d)[0], int(rng.lognormal(1.6, 0.9)) + 1)
        elif tp == "global":
            rapido = rng.random() < 0.5
            fixa = rng.random() < 0.6
            for d in rng.choice(n_dias, size=int(rng.integers(3, 10)), replace=False):
                ordem = ORDEM_GLOBAL if fixa else list(rng.permutation(sensores))
                b = d * 86400 + rng.uniform(0, 86400)
                offs = np.cumsum(rng.exponential(30.0 if rapido else 5400.0, size=len(ordem)))
                for s, o in zip(ordem, offs):
                    visita(i, s, b + o, int(rng.lognormal(2.3, 0.8)) + 1)
        elif tp == "pool":
            s = sensores[rng.integers(len(sensores))]
            for d in (r.dia_pool, r.dia_pool + 1):
                visita(i, s, t_dia(d)[0], int(rng.lognormal(2.0, 0.6)) + 1)
        elif tp == "pesquisa":
            for d in range(n_dias):
                if rng.random() < 0.85:
                    b = d * 86400 + rng.uniform(0, 86400)
                    offs = np.cumsum(rng.exponential(8.0, size=len(ORDEM_GLOBAL)))
                    for s, o in zip(ORDEM_GLOBAL, offs):
                        visita(i, s, b + o, int(rng.lognormal(3.0, 0.4)) + 1)
        elif tp == "mirai":
            # ativo em alguns dias; hora segue a hora LOCAL do dispositivo infectado
            fuso = int(round(r.lon / 15))
            ss = rng.choice(sensores, size=int(rng.integers(1, 3)), replace=False)
            for d in rng.choice(n_dias, size=int(rng.integers(1, 6)), replace=False):
                h_loc = rng.choice(24, p=pesos_hora_res)
                t = d * 86400 + ((h_loc - fuso) % 24) * 3600 + rng.uniform(0, 3600)
                for s in ss:
                    visita(i, s, t + rng.uniform(0, 600), int(rng.lognormal(1.3, 0.7)) + 1)
        elif tp == "campanha":
            d0 = 8 + min(int(rng.exponential(1.2)), n_dias - 9)
            if d0 >= n_dias:
                continue
            b = d0 * 86400 + rng.uniform(0, 86400)
            for s in rng.choice(sensores, size=int(rng.integers(3, 7)), replace=False):
                visita(i, s, b + rng.exponential(1800), int(rng.integers(1, 4)))

    V = pd.DataFrame(V)
    V = V[(V.t >= 0) & (V.t < T)].reset_index(drop=True)

    # ---------------- expande visitas em pacotes
    rep = V.n.values
    P = V.loc[V.index.repeat(rep)].reset_index(drop=True)
    P["t"] = P.t + rng.exponential(20.0, size=len(P))
    P = P.join(A[["ip", "asn", "tipo", "ferr", "proto", "saltos0", "reg_origem", "sport_fixa",
                  "porta_pool"]], on="ator")
    npk = len(P)

    # portas e protocolo
    dport = rng.choice(PORTAS_TCP, size=npk, p=PESO_TCP / PESO_TCP.sum())
    udp = P.proto.values == "UDP"
    dport[udp] = rng.choice(PORTAS_UDP, size=udp.sum(), p=PESO_UDP / PESO_UDP.sum())
    # cada ator usa só 1-3 portas: sorteia de um conjunto pequeno ligado ao ator
    sub = rng.random(npk) < 0.75
    ator_porta = np.where(udp, PORTAS_UDP[P.ator.values % len(PORTAS_UDP)],
                          PORTAS_TCP[(P.ator.values * 7) % len(PORTAS_TCP)])
    dport = np.where(sub, ator_porta, dport)
    m = (P.tipo == "pool").values; dport[m] = P.porta_pool.values[m]
    m = (P.tipo == "mirai").values
    dport[m] = rng.choice([23, 2323, 37215, 52869, 80], size=m.sum(), p=[0.6, 0.2, 0.08, 0.05, 0.07])
    m = (P.tipo == "campanha").values; dport[m] = 2375
    m = (P.tipo == "pesquisa").values
    dport[m] = np.where(rng.random(m.sum()) < 0.5, rng.choice(PORTAS_TCP, size=m.sum()),
                        rng.integers(1, 65536, size=m.sum()))
    icmp = P.proto.values == "ICMP"
    dport[icmp] = 0
    P["dport"] = dport.astype(np.uint16)

    # destino: host aleatório no /24 do sensor
    pref = P.sensor.map({s: _ip_int(SENSORES[s][0] + ".0") for s in sensores}).values
    P["dst"] = (pref + rng.integers(1, 255, size=npk)).astype(np.uint32)

    # ---------------- backscatter de DDoS (vítima responde a origens forjadas)
    bs = []
    vit = novos_ips(9, tipo_pref=["Hosting", "Cloud"])
    for k, v in enumerate(vit.itertuples(index=False)):
        ini = rng.uniform(0, T - 7200); dur = rng.uniform(1200, 5400)
        porta = int(rng.choice([80, 443, 53, 22]))
        nb = int(rng.integers(800, 3000) * escala) * len(sensores)
        tt = ini + rng.uniform(0, dur, size=nb)
        ss = rng.choice(sensores, size=nb)
        bs.append(pd.DataFrame({"t": tt, "sensor": ss, "ip": np.uint32(v.ip), "asn": v.asn,
                                "sport": porta, "flags": rng.choice([18, 4, 20], size=nb, p=[0.6, 0.15, 0.25])}))
    # backscatter de fundo: vítimas pequenas, poucas respostas
    nb = int(15000 * escala)
    fundo = novos_ips(1500, tipo_pref=["Hosting", "Cloud", "Datacenter", "Telecom"])
    idx = rng.integers(0, len(fundo), size=nb)
    bs.append(pd.DataFrame({"t": rng.uniform(0, T, size=nb), "sensor": rng.choice(sensores, size=nb),
                            "ip": fundo.ip.values[idx], "asn": fundo.asn.values[idx],
                            "sport": rng.choice([80, 443, 53, 25, 22], size=nb),
                            "flags": rng.choice([18, 4, 20], size=nb, p=[0.6, 0.15, 0.25])}))
    B = pd.concat(bs, ignore_index=True)
    B["dport"] = rng.integers(1024, 65535, size=len(B)).astype(np.uint16)
    B["dst"] = (B.sensor.map({s: _ip_int(SENSORES[s][0] + ".0") for s in sensores}).values
                + rng.integers(1, 255, size=len(B))).astype(np.uint32)

    # ---------------- monta colunas finais (varredura)
    F = pd.DataFrame({"t": P.t.values, "sensor": P.sensor.values, "src_ip": P.ip.values.astype(np.uint32),
                      "dst_ip": P.dst.values, "dst_port": P.dport.values, "proto": P.proto.values,
                      "ferr": P.ferr.values, "asn": P.asn.values})
    # porta de origem
    sp = rng.integers(1024, 65535, size=npk)
    fixa = np.isin(P.ferr.values, ["zmap", "masscan"])
    sp[fixa] = P.sport_fixa.values[fixa]
    sp[icmp] = 0
    F["src_port"] = sp.astype(np.uint16)
    # impressões digitais
    cols = {k: np.empty(npk, dtype=object) for k in ["opts"]}
    ttl0 = np.zeros(npk, int); ipid = rng.integers(0, 65536, size=npk); win = np.zeros(npk, int)
    mss = np.zeros(npk, int); wsc = np.zeros(npk, int); sack = np.zeros(npk, bool); tsx = np.zeros(npk, bool)
    df_ = np.zeros(npk, bool); tam = np.zeros(npk, int); opts = np.full(npk, "", dtype=object)
    for nome, (t0_, iid, w, o, ms, ws, sk, ts_, dfb, tm) in FERRAMENTAS.items():
        m = P.ferr.values == nome
        ttl0[m] = t0_
        if iid >= 0:
            ipid[m] = iid
        win[m] = w if w >= 0 else rng.integers(1024, 65535, size=m.sum())
        opts[m] = o; mss[m] = ms; wsc[m] = ws; sack[m] = sk; tsx[m] = ts_; df_[m] = dfb; tam[m] = tm
    # UDP/ICMP não têm campos TCP; tamanho depende do protocolo de aplicação
    nt = ~(P.proto.values == "TCP")
    win[nt] = 0; opts[nt] = ""; mss[nt] = 0; wsc[nt] = 0; sack[nt] = False; tsx[nt] = False
    tam_udp = {53: 62, 123: 76, 161: 107, 1900: 133, 11211: 43, 5060: 420}
    tam[udp] = [tam_udp[p] for p in dport[udp]]
    tam[icmp] = 28
    # TTL observado = inicial - saltos (saltos dependem de quão longe a origem está do sensor)
    longe = np.array([0 if ro == reg_sensor[s] else (2 if reg_sensor[s] == "NUVEM" else 5)
                      for ro, s in zip(P.reg_origem.values, P.sensor.values)])
    ttl = ttl0 - (P.saltos0.values + longe + (rng.random(npk) < 0.05))
    flags = np.where(P.proto.values == "TCP", 2, 0)
    # alguns stacks Linux mandam SYN com ECN (ECE+CWR = 194)
    ecn = (P.ferr.values == "linux") & (rng.random(npk) < 0.3)
    flags[ecn] = 194
    F = F.assign(ttl=ttl, ip_id=ipid, window=win, tcp_options_ordered=opts, mss=mss, wscale=wsc,
                 has_sack=sack, has_ts=tsx, ip_df=df_, pkt_len=tam, tcp_flags=flags)
    # retransmissões das pilhas de SO (mesmo fluxo, +1 s e +3 s)
    re = F[np.isin(F.ferr, ["linux", "windows"]) & (F.proto == "TCP") & (rng.random(npk) < 0.5)]
    F = pd.concat([F, re.assign(t=re.t + 1.0), re.iloc[::2].assign(t=re.t.iloc[::2] + 3.0)], ignore_index=True)

    # backscatter com as mesmas colunas
    Bf = pd.DataFrame({"t": B.t, "sensor": B.sensor, "src_ip": B.ip.astype(np.uint32), "dst_ip": B.dst,
                       "dst_port": B.dport, "proto": "TCP", "ferr": "backscatter", "asn": B.asn,
                       "src_port": B.sport.astype(np.uint16), "ttl": 64 - rng.integers(10, 20, size=len(B)),
                       "ip_id": 0, "window": np.where(B["flags"] == 18, 29200, 0),
                       "tcp_options_ordered": np.where(B["flags"] == 18, "MSS", ""),
                       "mss": np.where(B["flags"] == 18, 1460, 0), "wscale": 0, "has_sack": False,
                       "has_ts": False, "ip_df": True, "pkt_len": np.where(B["flags"] == 18, 44, 40),
                       "tcp_flags": B["flags"]})
    F = pd.concat([F, Bf], ignore_index=True)
    F = F[(F.t >= 0) & (F.t < T)]

    # ruído: SNMP de rede privada chegando ao polimi-worker-00 (gerência, NÃO é darknet)
    ns = int(2500 * n_dias)
    priv = np.array([_ip_int(x) for x in ["10.79.0.116", "10.79.1.17", "10.79.0.117"]], dtype=np.uint32)
    R = pd.DataFrame({"t": rng.uniform(0, T, size=ns), "sensor": "polimi-worker-00",
                      "src_ip": rng.choice(priv, size=ns), "dst_ip": np.uint32(_ip_int("198.18.30.6")),
                      "dst_port": np.uint16(161), "proto": "UDP", "ferr": "snmp", "asn": np.nan,
                      "src_port": rng.integers(40000, 60000, size=ns).astype(np.uint16), "ttl": 121,
                      "ip_id": rng.integers(0, 65536, size=ns), "window": 0, "tcp_options_ordered": "",
                      "mss": 0, "wscale": 0, "has_sack": False, "has_ts": False, "ip_df": False,
                      "pkt_len": 107, "tcp_flags": 0})
    F = pd.concat([F, R], ignore_index=True)

    # falhas de coleta: polito sem dados na manhã do dia 10 (UTC)
    d10 = 10 * 86400
    F = F[~((F.sensor == "polito-worker-00") & (F.t >= d10) & (F.t < d10 + 12 * 3600))]

    F["app_proto"] = np.where(F.proto == "ICMP", "ICMP", F.dst_port.map(APP).fillna("UNKNOWN"))
    m = F.ferr == "backscatter"
    F.loc[m, "app_proto"] = F.loc[m, "src_port"].map(APP).fillna("UNKNOWN")
    F["timestamp"] = (t0.value + (F.t.values * 1e9).astype(np.int64)).astype(np.int64)
    key = (F.src_ip.values.astype(np.uint64) << np.uint64(32)) ^ F.dst_ip.values.astype(np.uint64) \
        ^ (F.src_port.values.astype(np.uint64) << np.uint64(16)) ^ F.dst_port.values.astype(np.uint64)
    F["flow_hash"] = _mix64(key)
    F["dia"] = (F.t // 86400).astype(int)
    return F, asn


def _tabela(D: pd.DataFrame, asn: pd.DataFrame) -> pa.Table:
    ok = D.asn.notna()
    a = asn.reindex(D.asn.fillna(0).astype(int).values)
    rede = [f"{(r >> 24) & 255}.{(r >> 16) & 255}.0.0/16" if o else None for r, o in
            zip(a.rede16.fillna(0).astype(np.int64).values, ok.values)]
    org = np.where(ok, a.org.values, None)
    pais = np.where(ok, a.pais.values, None)
    info = [PAISES.get(p) if p else None for p in pais]
    dic = lambda xs: pa.array(list(xs), pa.string()).dictionary_encode()
    src_asn = pa.StructArray.from_arrays(
        [pa.array(np.where(ok, a.index.values, 0).astype(np.uint32), mask=~ok.values),
         dic(rede), dic(org)], names=["asn", "network", "organization_name"], mask=pa.array(~ok.values))
    loc = pa.StructArray.from_arrays(
        [pa.array([i[2] if i else None for i in info], pa.float64()),
         pa.array([i[3] if i else None for i in info], pa.float64())], names=["lat", "lon"])
    src_geo = pa.StructArray.from_arrays(
        [dic([i[1] if i else None for i in info]), dic(pais), dic([i[0] if i else None for i in info]), loc],
        names=["continent_name", "country_iso_code", "country_name", "location"], mask=pa.array(~ok.values))
    u = lambda c, t: pa.array(D[c].values.astype(t))
    return pa.table({
        "timestamp": u("timestamp", np.int64), "src_ip": u("src_ip", np.uint32), "dst_ip": u("dst_ip", np.uint32),
        "src_port": u("src_port", np.uint16), "dst_port": u("dst_port", np.uint16),
        "transport_proto": dic(D.proto), "app_proto": dic(D.app_proto), "ttl": u("ttl", np.uint8),
        "ip_id": u("ip_id", np.uint16), "ip_df": u("ip_df", bool), "window": u("window", np.uint16),
        "pkt_len": u("pkt_len", np.uint16), "tcp_flags": u("tcp_flags", np.uint8), "mss": u("mss", np.uint32),
        "wscale": u("wscale", np.uint32), "has_sack": u("has_sack", bool), "has_ts": u("has_ts", bool),
        "tcp_options_ordered": dic(D.tcp_options_ordered), "flow_hash": u("flow_hash", np.uint64),
        "src_asn": src_asn, "src_geo": src_geo})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--saida", default="dados_sinteticos")
    ap.add_argument("--inicio", default="2026-09-01")
    ap.add_argument("--dias", type=int, default=14)
    ap.add_argument("--escala", type=float, default=1.0, help="multiplica o nº de atores (volume)")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    F, asn = gerar(a.inicio, a.dias, a.seed, a.escala)
    os.makedirs(a.saida, exist_ok=True)
    t0 = pd.Timestamp(a.inicio)
    total = 0
    for (s, d), D in F.groupby(["sensor", "dia"]):
        dia = (t0 + pd.Timedelta(days=int(d))).strftime("%Y-%m-%d")
        if s == "azure-worker-00" and int(d) == 5:
            continue                                   # falha de coleta: arquivo do dia 6 não existe
        D = D.sample(frac=1, random_state=int(d)).sort_values("t", kind="stable")
        pq.write_table(_tabela(D, asn), os.path.join(a.saida, f"{s}-{dia}.parquet"),
                       row_group_size=4000, compression="zstd")
        total += len(D)
    print(f"{F.groupby(['sensor', 'dia']).ngroups - 1} arquivos, {total:,} pacotes em {a.saida}/")


if __name__ == "__main__":
    main()
