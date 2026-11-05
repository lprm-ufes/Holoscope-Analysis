"""
holoscope_io.py
---------------
Leitura dos dados de darknet do Holoscope no FORMATO REAL:

  * vários arquivos parquet, UM POR SENSOR E POR DIA, sem coluna 'sensor'
    (o sensor e o dia estão no NOME/CAMINHO do arquivo);
  * `timestamp` em epoch NANOSSEGUNDOS (int64, UTC);
  * IPs como inteiros uint32; flags TCP como bitmask uint8;
  * ASN e geolocalização da origem já mapeados, dentro de colunas STRUCT
    (`src_asn` = {asn, network, organization_name};
     `src_geo` = {continent_name, country_iso_code, country_name, location{lat, lon}}).

Funções principais
------------------
  inventario(pasta)            -> tabela sensor x dia x arquivo (sem ler os dados)
  carregar_holoscope(...)      -> UM DataFrame com os sensores/dias pedidos, já normalizado
  agregar_por_arquivo(...)     -> aplica uma função arquivo a arquivo (para dados grandes)
  validar(df)                  -> confere o DataFrame carregado

Esquema do DataFrame devolvido (colunas sempre presentes)
--------------------------------------------------------
  ts (datetime UTC), sensor, site, src_ip, dst_ip (uint32), src_port, dst_port,
  transport_proto, tcp_flags (uint8)  + colunas dos grupos extras pedidos:
    "asn"         -> asn (UInt32), asn_org, asn_rede
    "geo"         -> pais (ISO-2), pais_nome, continente, lat, lon
    "app"         -> app_proto
    "fingerprint" -> ttl, ip_id, ip_df, window, pkt_len, mss, wscale, has_sack, has_ts,
                     tcp_options_ordered
    "fluxo"       -> flow_hash
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# ----------------------------------------------------------------------------
# 1. Onde estão o sensor e o dia? (padrões de nome de arquivo CONFIGURÁVEIS)
# ----------------------------------------------------------------------------
# Cada padrão é uma expressão regular aplicada ao caminho RELATIVO do arquivo (com '/').
# Ela precisa ter dois grupos nomeados: (?P<sensor>...) e (?P<dia>...).
# O primeiro padrão que casar vence. Para outro formato, passe padroes=[...].
_DATA = r"\d{4}-?\d{2}-?\d{2}"
PADROES = [
    # estilo Hive/Spark:  sensor=ufes-worker-00/dia=2026-09-01/part-0.parquet
    rf"(?:^|/)sensor=(?P<sensor>[^/]+)/(?:dia|date|day|dt)=(?P<dia>{_DATA})/[^/]+\.parquet$",
    # subpasta por sensor:  ufes-worker-00/2026-09-01.parquet
    rf"(?:^|/)(?P<sensor>[^/=]+)/(?P<dia>{_DATA})\.parquet$",
    # tudo na mesma pasta:  ufes-worker-00-2026-09-01.parquet  (ou _20260901)
    rf"(?:^|/)(?P<sensor>[^/]+?)[-_](?P<dia>{_DATA})\.parquet$",
]

# Como tirar o SITE (instituição) do nome do sensor: 'polimi-worker-01' -> 'polimi'
PADRAO_SITE = r"^(?P<site>.+?)-worker-?\d+$"


def site_de(sensor: str, padrao_site: str = PADRAO_SITE) -> str:
    m = re.match(padrao_site, str(sensor))
    return m.group("site") if m else str(sensor)


def inventario(pasta, padroes: list[str] | None = None, padrao_site: str = PADRAO_SITE,
               ler_metadados: bool = True, verbose: bool = True) -> pd.DataFrame:
    """Lista os parquet de `pasta` (recursivamente) e descobre sensor e dia pelo caminho.

    NÃO lê os dados: só o rodapé (metadados) de cada arquivo, para saber o nº de linhas.
    Devolve uma tabela com: sensor, site, dia, caminho, linhas, mb.
    """
    padroes = padroes or PADROES
    pasta = Path(pasta)
    linhas, ignorados = [], []
    for p in sorted(pasta.rglob("*.parquet")):
        rel = p.relative_to(pasta).as_posix()
        m = next((re.search(rx, rel) for rx in padroes if re.search(rx, rel)), None)
        if m is None:
            ignorados.append(rel)
            continue
        dia = pd.to_datetime(m.group("dia").replace("-", ""), format="%Y%m%d").date()
        reg = {"sensor": m.group("sensor"), "site": site_de(m.group("sensor"), padrao_site),
               "dia": dia, "caminho": str(p), "mb": p.stat().st_size / 1e6}
        if ler_metadados:
            reg["linhas"] = pq.read_metadata(p).num_rows
        linhas.append(reg)
    if verbose and ignorados:
        print(f"[aviso] {len(ignorados)} parquet ignorados (nome fora dos padrões), ex.: {ignorados[:3]}")
    if not linhas:
        raise FileNotFoundError(f"Nenhum parquet reconhecido em {pasta}. Confira `padroes`.")
    inv = pd.DataFrame(linhas).sort_values(["sensor", "dia"]).reset_index(drop=True)
    dup = inv.duplicated(["sensor", "dia"], keep=False)
    if verbose and dup.any():
        print(f"[aviso] {dup.sum()} arquivos com (sensor, dia) repetido — serão lidos todos e somados.")
    return inv


def matriz_inventario(inv: pd.DataFrame, valor: str = "linhas") -> pd.DataFrame:
    """Tabela sensor x dia com `valor` (linhas ou mb). Dia sem arquivo = NaN."""
    dias = pd.date_range(inv["dia"].min(), inv["dia"].max(), freq="D").date
    return (inv.pivot_table(index="sensor", columns="dia", values=valor, aggfunc="sum")
               .reindex(columns=dias))


# ----------------------------------------------------------------------------
# 2. Colunas: o que existe no arquivo e como vira coluna do DataFrame
# ----------------------------------------------------------------------------
COLUNAS_BASE = ["timestamp", "src_ip", "dst_ip", "src_port", "dst_port", "transport_proto", "tcp_flags"]
GRUPOS = {
    "asn": ["src_asn"],
    "geo": ["src_geo"],
    "app": ["app_proto"],
    "fingerprint": ["ttl", "ip_id", "ip_df", "window", "pkt_len", "mss", "wscale",
                    "has_sack", "has_ts", "tcp_options_ordered"],
    "fluxo": ["flow_hash"],
}
# campo do struct -> nome da coluna no DataFrame
STRUCTS = {
    "src_asn": {"asn": "asn", "organization_name": "asn_org", "network": "asn_rede"},
    "src_geo": {"country_iso_code": "pais", "country_name": "pais_nome",
                "continent_name": "continente", "location.lat": "lat", "location.lon": "lon"},
}
CATEGORICAS = ["sensor", "site", "transport_proto", "app_proto", "tcp_options_ordered",
               "asn_org", "asn_rede", "pais", "pais_nome", "continente"]


def _campos_struct(chunk: pa.StructArray, prefixo: str = "") -> dict:
    """Achata um StructArray (respeitando nulos do 'pai') em {caminho: Array}."""
    out = {}
    for campo, filho in zip(chunk.type, chunk.flatten()):
        nome = f"{prefixo}{campo.name}"
        if pa.types.is_struct(filho.type):
            out.update(_campos_struct(filho, nome + "."))
        else:
            if pa.types.is_dictionary(filho.type):
                filho = filho.dictionary_decode()   # dicionário (às vezes enorme) -> texto
            out[nome] = filho
    return out


def _tabela_para_pandas(tb: pa.Table) -> pd.DataFrame:
    """Converte a tabela arrow em DataFrame, achatando os structs src_asn/src_geo."""
    simples = [c for c in tb.column_names if c not in STRUCTS]
    df = tb.select(simples).to_pandas()
    for st, mapa in STRUCTS.items():
        if st not in tb.column_names:
            continue
        partes = {}
        for chunk in tb.column(st).chunks:
            for caminho, arr in _campos_struct(chunk).items():
                if caminho in mapa:
                    partes.setdefault(caminho, []).append(arr)
        for caminho, arrs in partes.items():
            s = pa.chunked_array(arrs).to_pandas()
            nome = mapa[caminho]
            if nome == "asn":
                s = s.astype("UInt32")
            df[nome] = s.values
    return df


def ler_arquivo(caminho: str, colunas: list[str] | None = None) -> pd.DataFrame:
    """Lê UM parquet (só as colunas pedidas) e devolve DataFrame com structs achatados.
    Colunas pedidas que não existem no arquivo são ignoradas (com aviso)."""
    arq = pq.ParquetFile(caminho)
    existentes = arq.schema_arrow.names
    colunas = colunas or existentes
    faltando = [c for c in colunas if c not in existentes]
    if faltando:
        print(f"[aviso] {os.path.basename(caminho)} não tem {faltando}")
    colunas = [c for c in colunas if c in existentes]
    if not any(c in STRUCTS for c in colunas):
        return _tabela_para_pandas(arq.read(columns=colunas))
    # Com structs: lê um row group por vez. Nos arquivos reais o dicionário de nomes de
    # organização/rede se repete em CADA row group e ocupa centenas de MB; ler tudo de uma
    # vez chegou a ~1,8 GB de pico para um arquivo de 90 mil linhas (por partes: ~300 MB).
    partes = [_tabela_para_pandas(arq.read_row_group(i, columns=colunas))
              for i in range(arq.num_row_groups)]
    return pd.concat(partes, ignore_index=True) if partes else _tabela_para_pandas(arq.read(columns=colunas))


# ----------------------------------------------------------------------------
# 3. IPs reservados/privados (NÃO são tráfego da internet: gerência, rede local...)
# ----------------------------------------------------------------------------
RESERVADOS = ["0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
              "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16", "224.0.0.0/3"]


def _rede_int(cidr: str):
    ip, bits = cidr.split("/")
    a, b, c, d = map(int, ip.split("."))
    bits = int(bits)
    mask = (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF
    return np.uint32((a << 24) | (b << 16) | (c << 8) | d), np.uint32(mask)


def eh_reservado(ip) -> np.ndarray:
    """True para IPs (uint32) em faixas privadas/reservadas."""
    v = np.asarray(ip, dtype=np.uint32)
    res = np.zeros(len(v), dtype=bool)
    for cidr in RESERVADOS:
        rede, mask = _rede_int(cidr)
        res |= (v & mask) == rede
    return res


# ----------------------------------------------------------------------------
# 4. Normalização e carregamento
# ----------------------------------------------------------------------------
def _normalizar(df: pd.DataFrame, sensor: str, site: str, ts_unidade: str = "ns") -> pd.DataFrame:
    df.insert(0, "ts", pd.to_datetime(df.pop("timestamp"), unit=ts_unidade, utc=True))
    df.insert(1, "sensor", sensor)
    df.insert(2, "site", site)
    return df


def _selecionar(inv, sensores, inicio, fim):
    sel = inv
    if sensores is not None:
        sensores = [sensores] if isinstance(sensores, str) else list(sensores)
        sel = sel[sel["sensor"].isin(sensores) | sel["site"].isin(sensores)]
    if inicio is not None:
        sel = sel[sel["dia"] >= pd.Timestamp(inicio).date()]
    if fim is not None:
        sel = sel[sel["dia"] <= pd.Timestamp(fim).date()]
    if sel.empty:
        raise ValueError("Nenhum arquivo para esses sensores/datas.")
    return sel


def _colunas_pedidas(extras, colunas):
    if colunas is not None:
        return list(dict.fromkeys(["timestamp", *colunas]))
    cols = list(COLUNAS_BASE)
    for g in extras or ():
        if g not in GRUPOS:
            raise ValueError(f"Grupo extra desconhecido: {g}. Opções: {list(GRUPOS)}")
        cols += GRUPOS[g]
    return cols


def _finalizar(df: pd.DataFrame) -> pd.DataFrame:
    for c in CATEGORICAS:
        if c in df.columns:
            df[c] = df[c].astype("category")
    return df.sort_values("ts", kind="stable").reset_index(drop=True)


def carregar_holoscope(fonte, sensores=None, inicio=None, fim=None, extras=("asn",),
                       colunas: list[str] | None = None, unidade: str = "worker",
                       remover_reservados: bool = True, ts_unidade: str = "ns",
                       padroes: list[str] | None = None, verbose: bool = True) -> pd.DataFrame:
    """Carrega os parquet de vários sensores e dias em UM DataFrame.

    fonte      : pasta com os parquet OU um inventário (saída de `inventario`).
    sensores   : lista de sensores (ou sites) a ler; None = todos.
    inicio/fim : 'AAAA-MM-DD' (inclusivos), filtram pelos DIAS dos arquivos.
    extras     : grupos de colunas além das básicas: "asn", "geo", "app", "fingerprint", "fluxo".
    colunas    : lista explícita de colunas do ARQUIVO (substitui base+extras).
    unidade    : "worker" (cada worker é um sensor) ou "site" (workers do mesmo site somados).
    remover_reservados : descarta origens privadas/reservadas (tráfego que não veio da internet).
    """
    inv = fonte if isinstance(fonte, pd.DataFrame) else inventario(fonte, padroes, verbose=verbose)
    sel = _selecionar(inv, sensores, inicio, fim)
    cols = _colunas_pedidas(extras, colunas)

    partes, descartados = [], 0
    for r in sel.itertuples(index=False):
        d = ler_arquivo(r.caminho, cols)
        if remover_reservados and "src_ip" in d.columns:
            ruim = eh_reservado(d["src_ip"].values)
            descartados += int(ruim.sum())
            d = d[~ruim]
        nome = r.sensor if unidade == "worker" else r.site
        partes.append(_normalizar(d, nome, r.site, ts_unidade))
    df = _finalizar(pd.concat(partes, ignore_index=True))

    if verbose:
        n_arq = len(sel)
        print(f"{n_arq} arquivos | {df['sensor'].nunique()} sensores ({unidade}) | "
              f"{sel['dia'].min()} a {sel['dia'].max()} | {len(df):,} pacotes | "
              f"{df.memory_usage(deep=True).sum() / 1e6:,.0f} MB na memória")
        if remover_reservados:
            print(f"origens reservadas/privadas descartadas: {descartados:,}")
    return df


def agregar_por_arquivo(fonte, func, sensores=None, inicio=None, fim=None, extras=("asn",),
                        colunas=None, unidade="worker", remover_reservados=True,
                        ts_unidade="ns", padroes=None, verbose=True) -> pd.DataFrame:
    """Para dados GRANDES: lê UM arquivo (sensor, dia) por vez, aplica `func(df)` e junta
    só os resultados (que são pequenos). `func` recebe o DataFrame do arquivo já normalizado
    e deve devolver um DataFrame, Series ou número.

    Exemplo:  agregar_por_arquivo(PASTA, lambda d: d['src_ip'].nunique())
    """
    inv = fonte if isinstance(fonte, pd.DataFrame) else inventario(fonte, padroes, verbose=verbose)
    sel = _selecionar(inv, sensores, inicio, fim)
    cols = _colunas_pedidas(extras, colunas)
    res = []
    for i, r in enumerate(sel.itertuples(index=False), 1):
        d = ler_arquivo(r.caminho, cols)
        if remover_reservados and "src_ip" in d.columns:
            d = d[~eh_reservado(d["src_ip"].values)]
        nome = r.sensor if unidade == "worker" else r.site
        d = _finalizar(_normalizar(d, nome, r.site, ts_unidade))
        out = func(d)
        if isinstance(out, pd.Series):
            out = out.rename(out.name or "valor").reset_index()
        elif not isinstance(out, pd.DataFrame):
            out = pd.DataFrame({"valor": [out]})
        out.insert(0, "dia", r.dia)
        out.insert(0, "sensor", nome)
        res.append(out)
        if verbose and (i % 20 == 0 or i == len(sel)):
            print(f"  {i}/{len(sel)} arquivos processados")
    return pd.concat(res, ignore_index=True)


# ----------------------------------------------------------------------------
# 5. Conversões úteis
# ----------------------------------------------------------------------------
def ip_texto(ip) -> pd.Series:
    """uint32 -> 'a.b.c.d' (use só para EXIBIR; para contar e agrupar, mantenha inteiro)."""
    idx = ip.index if isinstance(ip, pd.Series) else None
    v = np.asarray(ip, dtype=np.int64)
    s = [pd.Series((v >> k) & 255, index=idx).astype(str) for k in (24, 16, 8, 0)]
    return s[0] + "." + s[1] + "." + s[2] + "." + s[3]


def ip_inteiro(texto) -> np.ndarray:
    """'a.b.c.d' -> uint32."""
    p = pd.Series(texto).str.split(".", expand=True).astype(np.int64).values
    return ((p[:, 0] << 24) | (p[:, 1] << 16) | (p[:, 2] << 8) | p[:, 3]).astype(np.uint32)


def prefixo(ip, bits: int = 24) -> np.ndarray:
    """Endereço de rede (uint32) do prefixo /bits que contém cada IP."""
    mask = np.uint32((0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF)
    return np.asarray(ip, dtype=np.uint32) & mask


def rede_texto(rede, bits: int = 24) -> pd.Series:
    return ip_texto(rede) + f"/{bits}"


_FLAGS = [(1, "F"), (2, "S"), (4, "R"), (8, "P"), (16, "A"), (32, "U"), (64, "E"), (128, "C")]


def flags_texto(flags) -> pd.Series:
    """Bitmask TCP -> texto: 2 -> 'S', 18 -> 'SA', 4 -> 'R', 20 -> 'RA', 194 -> 'SEC'.
    Para pacotes não-TCP o valor é 0 -> ''."""
    f = pd.Series(np.asarray(flags, dtype=np.int64),
                  index=flags.index if isinstance(flags, pd.Series) else None)
    lut = {v: "".join(ch for b, ch in _FLAGS if v & b) for v in f.unique()}
    return f.map(lut)


def ttl_inicial(ttl) -> np.ndarray:
    """TTL com que o pacote provavelmente SAIU da origem: o menor entre 32, 64, 128, 255
    que seja >= TTL observado (sistemas usam valores iniciais padronizados)."""
    t = np.asarray(ttl, dtype=np.int64)
    return np.select([t <= 32, t <= 64, t <= 128], [32, 64, 128], 255)


def saltos(ttl) -> np.ndarray:
    """Número estimado de roteadores atravessados = TTL inicial - TTL observado."""
    return ttl_inicial(ttl) - np.asarray(ttl, dtype=np.int64)


# ----------------------------------------------------------------------------
# 6. Validação
# ----------------------------------------------------------------------------
def validar(df: pd.DataFrame, verbose: bool = True) -> bool:
    """Confere o DataFrame carregado. Imprime OK / AVISO / ERRO e devolve True se não houver ERRO."""
    msgs, ok = [], True
    for c in ["ts", "sensor", "src_ip", "dst_ip", "dst_port", "transport_proto"]:
        if c not in df.columns:
            msgs.append(f"[ERRO]  falta '{c}'"); ok = False
    if "ts" in df.columns:
        if str(df["ts"].dt.tz) != "UTC":
            msgs.append("[ERRO]  'ts' não está em UTC"); ok = False
        anos = df["ts"].dt.year.unique()
        if not all(2015 <= a <= 2035 for a in anos):
            msgs.append(f"[ERRO]  anos estranhos em 'ts' {sorted(anos)[:5]} — unidade errada?"); ok = False
    if "src_ip" in df.columns:
        frac = eh_reservado(df["src_ip"].values).mean()
        if frac > 0:
            msgs.append(f"[AVISO] {frac:.1%} das origens são privadas/reservadas")
    if "dst_ip" in df.columns and "sensor" in df.columns:
        n24 = df.assign(d24=prefixo(df["dst_ip"].values, 24)).groupby("sensor", observed=True)["d24"].nunique()
        msgs.append("[OK]    prefixos /24 de destino por sensor: " + ", ".join(f"{k}={v}" for k, v in n24.items()))
    if "asn" in df.columns:
        sem = df["asn"].isna().mean()
        msgs.append(f"[{'AVISO' if sem > 0.05 else 'OK'}]{' ' if sem > 0.05 else '    '}"
                    f"{sem:.1%} dos pacotes sem ASN")
    if "sensor" in df.columns:
        msgs.append(f"[OK]    {df['sensor'].nunique()} sensores: {sorted(map(str, df['sensor'].unique()))}")
    if verbose:
        print(f"{len(df):,} pacotes | colunas: {list(df.columns)}")
        print("\n".join(msgs))
        print("=> ", "VÁLIDO" if ok else "COM PROBLEMAS")
    return ok
