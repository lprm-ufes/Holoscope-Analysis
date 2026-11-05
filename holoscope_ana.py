"""
holoscope_ana.py
----------------
Funções de ANÁLISE e de GRÁFICO reaproveitadas pelos notebooks 01–06.
Os notebooks mostram a ideia de cada função na primeira vez que ela aparece;
depois disso, só a chamam daqui.
"""
from __future__ import annotations

import itertools

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap

from holoscope_io import prefixo, rede_texto, ip_texto

# ----------------------------------------------------------------------------
# Estilo e cores
# ----------------------------------------------------------------------------
# Paleta categórica em ORDEM FIXA (validada para daltonismo nos pares vizinhos).
PALETA = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
CINZA = "#8a8984"
TEXTO = "#0b0b0b"
TEXTO2 = "#52514e"
GRADE = "#e4e3df"
# Sequencial (magnitude): um só matiz, do claro ao escuro.
CMAP_SEQ = LinearSegmentedColormap.from_list(
    "azul", ["#f4f8fd", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"])
# Divergente (acima/abaixo de um valor de referência): dois matizes + cinza no meio.
CMAP_DIV = LinearSegmentedColormap.from_list("div", ["#b6431c", "#eb6834", "#f3c3ac", "#ecebe8",
                                                     "#b7d3f6", "#3987e5", "#184f95"])


def estilo():
    """Estilo único para todos os gráficos (linhas finas, grade discreta, sem moldura)."""
    mpl.rcParams.update({
        "figure.dpi": 110, "figure.figsize": (9, 4), "figure.facecolor": "white",
        "axes.facecolor": "white", "axes.edgecolor": GRADE, "axes.labelcolor": TEXTO2,
        "axes.titlesize": 12, "axes.titleweight": "bold", "axes.titlelocation": "left",
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "grid.color": GRADE, "grid.linewidth": 0.8,
        "axes.prop_cycle": mpl.cycler(color=PALETA),
        "lines.linewidth": 2, "lines.markersize": 6,
        "xtick.color": TEXTO2, "ytick.color": TEXTO2, "legend.frameon": False,
        "font.size": 10,
    })
    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 30)


def cores(nomes) -> dict:
    """Cor FIXA por nome (sensor, ferramenta...). A cor segue a entidade, não a posição:
    filtrar sensores não muda a cor dos que sobram."""
    nomes = sorted(map(str, nomes))
    if len(nomes) > len(PALETA):
        raise ValueError("Mais de 8 categorias: agrupe o resto em 'Outros' ou use painéis separados.")
    return {n: PALETA[i] for i, n in enumerate(nomes)}


def heatmap(M: pd.DataFrame, ax=None, cmap=None, fmt="{:.2f}", vmin=None, vmax=None,
            anotar=True, titulo=None, barra=True, centro=None):
    """Mapa de calor de uma tabela, com os valores escritos nas células."""
    ax = ax or plt.gca()
    vals = M.values.astype(float)
    if centro is not None:
        lim = np.nanmax(np.abs(vals - centro))
        vmin, vmax, cmap = centro - lim, centro + lim, cmap or CMAP_DIV
    im = ax.imshow(vals, cmap=cmap or CMAP_SEQ, vmin=vmin, vmax=vmax, aspect="auto")
    ax.set_xticks(range(M.shape[1]), [str(c) for c in M.columns], rotation=45, ha="right")
    ax.set_yticks(range(M.shape[0]), [str(i) for i in M.index])
    ax.grid(False)
    if anotar and M.size <= 400:
        lo, hi = im.get_clim()
        for (i, j), v in np.ndenumerate(vals):
            if np.isnan(v):
                continue
            claro = (v - lo) / (hi - lo + 1e-12) > 0.6 if centro is None else abs(v - centro) / (hi - centro + 1e-12) > 0.6
            ax.text(j, i, fmt.format(v), ha="center", va="center", fontsize=8,
                    color="white" if claro else TEXTO)
    if barra:
        plt.colorbar(im, ax=ax, fraction=0.04, pad=0.02)
    if titulo:
        ax.set_title(titulo)
    return ax


def barras_h(s: pd.Series, ax=None, cor=PALETA[0], fmt="{:,.0f}", titulo=None, xlabel=None):
    """Barras horizontais ordenadas (maior em cima), com o valor escrito ao lado."""
    ax = ax or plt.gca()
    s = s.iloc[::-1]
    ax.barh([str(i) for i in s.index], s.values, color=cor, height=0.7)
    for y, v in enumerate(s.values):
        ax.text(v, y, " " + fmt.format(v), va="center", fontsize=8, color=TEXTO2)
    ax.grid(axis="y", visible=False)
    ax.margins(x=0.15)
    if titulo:
        ax.set_title(titulo)
    if xlabel:
        ax.set_xlabel(xlabel)
    return ax


# ----------------------------------------------------------------------------
# Entidades
# ----------------------------------------------------------------------------
def adicionar_entidades(df: pd.DataFrame) -> pd.DataFrame:
    """Cria as colunas de entidade de ORIGEM (mantidas como números, que é mais leve):
      ent_ip  = IP de origem (uint32)
      ent_24  = rede /24 de origem (uint32 do endereço de rede)
      ent_asn = número do sistema autônomo (se a coluna 'asn' existir)
      ent_pais= país (se 'pais' existir)
    """
    out = df.copy()
    out["ent_ip"] = out["src_ip"].values
    out["ent_24"] = prefixo(out["src_ip"].values, 24)
    if "asn" in out.columns:
        out["ent_asn"] = out["asn"]
    if "pais" in out.columns:
        out["ent_pais"] = out["pais"]
    return out


NIVEIS = {"IP": "ent_ip", "/24": "ent_24", "ASN": "ent_asn", "País": "ent_pais"}


def rotulo(nivel: str, valores) -> pd.Series:
    """Texto legível para valores de uma entidade (IP e /24 viram 'a.b.c.d')."""
    v = pd.Series(valores)
    if nivel in ("IP", "ent_ip"):
        return ip_texto(v.astype("uint32"))
    if nivel in ("/24", "ent_24"):
        return rede_texto(v.astype("uint32"), 24)
    if nivel in ("ASN", "ent_asn"):
        return "AS" + v.astype("Int64").astype(str)
    return v.astype(str)


# ----------------------------------------------------------------------------
# Presença e interseção
# ----------------------------------------------------------------------------
def matriz_presenca(df: pd.DataFrame, nivel: str = "ent_ip") -> pd.DataFrame:
    """Tabela booleana entidade x sensor: True se a entidade foi vista no sensor."""
    cont = df.groupby([nivel, "sensor"], observed=True).size().unstack(fill_value=0)
    return cont > 0


def jaccard(P: pd.DataFrame) -> pd.DataFrame:
    """Jaccard(A,B) = |A ∩ B| / |A ∪ B| para cada par de sensores (colunas de P)."""
    X = P.astype(np.int64).values
    inter = X.T @ X
    tam = np.diag(inter)
    uni = tam[:, None] + tam[None, :] - inter
    return pd.DataFrame(inter / uni, index=P.columns, columns=P.columns)


def condicional(P: pd.DataFrame) -> pd.DataFrame:
    """P(vê coluna | vê linha). Assimétrica: revela se um sensor 'contém' o outro."""
    X = P.astype(np.int64).values
    inter = X.T @ X
    return pd.DataFrame(inter / np.diag(inter)[:, None], index=P.columns, columns=P.columns)


def lift(P: pd.DataFrame) -> pd.DataFrame:
    """lift(A,B) = P(A e B) / (P(A) P(B)).  1 = independência; >1 = aparecem juntos mais que o acaso."""
    X = P.astype(np.int64).values
    n = len(X)
    inter = X.T @ X / n
    p = np.diag(inter)
    return pd.DataFrame(inter / np.outer(p, p), index=P.columns, columns=P.columns)


def combinacoes(P: pd.DataFrame, top: int = 15) -> pd.Series:
    """Conta as entidades por COMBINAÇÃO EXATA de sensores (base do gráfico 'UpSet')."""
    chave = P.apply(lambda r: tuple(c for c, v in r.items() if v), axis=1)
    return chave.value_counts().head(top)


def grafico_upset(cont: pd.Series, sensores, titulo="Combinações de sensores"):
    """Barras = nº de entidades; pontos embaixo = quais sensores formam a combinação."""
    sensores = list(sensores)
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(10, 5.5), sharex=True,
                                 gridspec_kw={"height_ratios": [2.2, 1.4], "hspace": 0.05})
    x = np.arange(len(cont))
    a1.bar(x, cont.values, color=PALETA[0], width=0.7)
    for xi, v in zip(x, cont.values):
        a1.text(xi, v, f"{v:,}", ha="center", va="bottom", fontsize=7, color=TEXTO2)
    a1.set_ylabel("entidades")
    a1.set_title(titulo)
    a1.grid(axis="x", visible=False)
    for xi, comb in zip(x, cont.index):
        ys = [sensores.index(s) for s in comb]
        a2.scatter([xi] * len(sensores), range(len(sensores)), color=GRADE, s=40, zorder=1)
        a2.scatter([xi] * len(ys), ys, color=TEXTO, s=40, zorder=2)
        if len(ys) > 1:
            a2.plot([xi, xi], [min(ys), max(ys)], color=TEXTO, lw=2, zorder=2)
    a2.set_yticks(range(len(sensores)), sensores)
    a2.set_xticks([])
    a2.grid(False)
    a2.invert_yaxis()
    return fig


# ----------------------------------------------------------------------------
# Distribuições
# ----------------------------------------------------------------------------
def ecdf(x):
    """CDF empírica: devolve (x ordenado, P(X <= x))."""
    x = np.sort(np.asarray(x, dtype=float))
    return x, np.arange(1, len(x) + 1) / len(x)


def ccdf(x):
    """CDF complementar: P(X >= x). Em escala log-log revela caudas pesadas."""
    x, y = ecdf(x)
    return x, 1 - y + 1 / len(x)


# ----------------------------------------------------------------------------
# Tempo e simultaneidade
# ----------------------------------------------------------------------------
def primeira_vez(df: pd.DataFrame, nivel: str = "ent_ip", por_dia: bool = False) -> pd.DataFrame:
    """Instante da PRIMEIRA aparição de cada entidade em cada sensor (entidade x sensor).
    Com por_dia=True, a primeira aparição em cada DIA (índice = entidade, dia)."""
    chaves = [nivel, "sensor"]
    if por_dia:
        df = df.assign(dia=df["ts"].dt.floor("D"))
        chaves = [nivel, "dia", "sensor"]
    return df.groupby(chaves, observed=True)["ts"].min().unstack("sensor")


def delta_pares(first: pd.DataFrame) -> pd.DataFrame:
    """Para cada par (A, B) e cada entidade vista nos dois: Δt = t_B − t_A em segundos.
    Formato longo: par, a, b, dt_s. Aceita instantes datetime ou números (segundos)."""
    partes = []
    for a, b in itertools.combinations(first.columns, 2):
        sub = first[[a, b]].dropna()
        if sub.empty:
            continue
        dif = sub[b] - sub[a]
        dt = dif.dt.total_seconds().values if hasattr(dif, "dt") else dif.values.astype(float)
        partes.append(pd.DataFrame({"par": f"{a} → {b}", "a": a, "b": b, "dt_s": dt}))
    return pd.concat(partes, ignore_index=True) if partes else pd.DataFrame(columns=["par", "a", "b", "dt_s"])


def embaralhar_colunas(first: pd.DataFrame, seed: int = 0) -> pd.DataFrame:
    """MODELO NULO: em cada sensor, embaralha os instantes entre as entidades.
    Mantém a distribuição de horários de cada sensor, mas destrói a ligação
    'esta entidade chegou aqui e lá ao mesmo tempo'."""
    rng = np.random.default_rng(seed)
    out = first.copy()
    for c in out.columns:
        v = out[c].values.copy()
        ok = ~pd.isna(v)
        v[ok] = rng.permutation(v[ok])
        out[c] = v
    return out


def kaplan_meier(duracao, evento):
    """Estimador de Kaplan–Meier da 'sobrevivência' S(t) = P(duração > t).
    duracao: tempo observado; evento: True se o FIM foi observado (False = censurado)."""
    d = pd.DataFrame({"t": np.asarray(duracao, float), "e": np.asarray(evento, bool)})
    tab = d.groupby("t").agg(n_evento=("e", "sum"), n_saida=("e", "size")).sort_index()
    em_risco = len(d) - tab["n_saida"].cumsum().shift(fill_value=0)
    s = (1 - tab["n_evento"] / em_risco).cumprod()
    return pd.concat([pd.Series([1.0], index=[0.0]), s]).groupby(level=0).last()


def sem_diagonal(M: pd.DataFrame) -> pd.DataFrame:
    """Troca a diagonal por NaN (sensor contra ele mesmo não informa nada e 'rouba' a escala de cores)."""
    return M.where(~np.eye(len(M), dtype=bool))
