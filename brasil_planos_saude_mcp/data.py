"""Downloads, parses and caches the ANS open-data files this server uses.

Every dataset is a CSV on https://dadosabertos.ans.gov.br/FTP/PDA/. Each one is parsed
once into a compact gzip JSON in the user's cache dir. A cached copy is trusted for 24 h;
after that a HEAD request checks Last-Modified, and the file is downloaded again only if
ANS published a new one. If ANS is unreachable, the cached copy is used with a warning.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import os
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import httpx
from platformdirs import user_cache_dir

from .common import USER_AGENT, decode, digits, num, reg

log = logging.getLogger("brasil_planos_saude_mcp")

BASE = "https://dadosabertos.ans.gov.br/FTP/PDA"
CACHE_VERSION = 1
TRUST_SECONDS = 24 * 3600
IGR_MONTHS_KEPT = 24

Progress = Callable[[str, "float | None"], None]


def cache_dir() -> Path:
    d = Path(os.environ.get("BRASIL_PLANOS_SAUDE_CACHE_DIR") or user_cache_dir("brasil-planos-saude-mcp", appauthor=False))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text), delimiter=";"))


# ---------------------------------------------------------------------------------
# Parsers: raw CSV text -> compact JSON-able structures keyed by 6-digit registry
# ---------------------------------------------------------------------------------


def _operator(r: dict[str, str], status: str) -> dict[str, Any]:
    # Contact e-mail and the legal representative's name are left out on purpose:
    # they can identify a person, and nobody needs them to compare health plans.
    phone = " ".join(x for x in (digits(r.get("DDD")), digits(r.get("TELEFONE"))) if x)
    o = {
        "registro_ans": reg(r["REGISTRO_OPERADORA"]),
        "cnpj": digits(r.get("CNPJ")),
        "razao_social": (r.get("RAZAO_SOCIAL") or "").strip(),
        "nome_fantasia": (r.get("NOME_FANTASIA") or "").strip() or None,
        "modalidade": (r.get("MODALIDADE") or "").strip(),
        "sede": {"cidade": (r.get("CIDADE") or "").strip(), "uf": (r.get("UF") or "").strip()},
        "telefone": phone or None,
        "registrada_em": r.get("DATA_REGISTRO_ANS") or None,
        "situacao": status,
    }
    if status == "cancelada":
        o["cancelada_em"] = r.get("DATA_DESCREDENCIAMENTO") or None
        o["motivo_cancelamento"] = (r.get("MOTIVO_DO_DESCREDENCIAMENTO") or "").strip() or None
    return o


def parse_operators_active(text: str) -> dict[str, Any]:
    return {o["registro_ans"]: o for o in (_operator(r, "ativa") for r in _rows(text))}


def parse_operators_cancelled(text: str) -> dict[str, Any]:
    return {o["registro_ans"]: o for o in (_operator(r, "cancelada") for r in _rows(text))}


def parse_igr(text: str) -> dict[str, Any]:
    """IGR (version 2023): monthly complaints per 100,000 beneficiaries, per operator and coverage."""
    by: dict[str, dict[str, list[list[Any]]]] = defaultdict(lambda: defaultdict(list))
    names: dict[str, str] = {}
    updated = ""
    for r in _rows(text):
        k = reg(r["REGISTRO_OPERADORA"])
        names[k] = r["RAZAO_SOCIAL"].strip()
        cov = "odonto" if "odonto" in r["COBERTURA"].lower() else "medica"
        by[k][cov].append(
            [r["COMPETENCIA"], num(r["IGR"]), int(num(r["QTD_RECLAMACOES"]) or 0), int(num(r["QTD_BENEFICIARIOS"]) or 0), r["PORTE_OPERADORA"]]
        )
        updated = max(updated, r.get("DT_ATUALIZACAO") or "")
    out: dict[str, Any] = {}
    for k, covs in by.items():
        out[k] = {cov: sorted(v)[-IGR_MONTHS_KEPT:] for cov, v in covs.items()}
    return {"by_operator": out, "names": names, "updated": updated}


def parse_idss(text: str) -> dict[str, Any]:
    """IDSS history: columns like IDSS_2025_2024 = program year 2025, base year 2024."""
    out: dict[str, Any] = {}
    for r in _rows(text):
        years: dict[str, dict[str, float | None]] = {}
        for col, val in r.items():
            if not col or not col.startswith(("IDSS_", "IDQS_", "IDGA_", "IDSM_", "IDGR_")):
                continue
            idx, year, base = col.split("_")
            v = num(val)
            if v is not None:
                years.setdefault(year, {"ano_base": int(base)})[idx.lower()] = v
        if years:
            out[reg(r["REGISTRO_OPERADORA"])] = dict(sorted(years.items()))
    return out


def parse_penalties(text: str) -> dict[str, Any]:
    """Fines applied by ANS, aggregated per operator (individual cases kept only for the latest few)."""
    agg: dict[str, Any] = {}
    snapshot = ""
    for r in _rows(text):
        k = reg(r["REGISTRO_OPERADORA"])
        snapshot = max(snapshot, r.get("NR_COMPETENCIA_CARGA") or "")
        d = r.get("DT_DECISAO_1A") or ""
        iso = f"{d[6:10]}-{d[3:5]}-{d[0:2]}" if len(d) == 10 else ""
        value = num(r.get("VL_MULTA_FINAL_APLICADA")) or num(r.get("VL_TOTAL_APLICADO_2A")) or num(r.get("VL_TOTAL_APLICADO_1A")) or 0.0
        a = agg.setdefault(k, {"total": 0, "valor_total": 0.0, "por_ano": {}, "por_status": {}, "por_tipo": {}, "recentes": []})
        a["total"] += 1
        a["valor_total"] += value
        if iso:
            y = a["por_ano"].setdefault(iso[:4], [0, 0.0])
            y[0] += 1
            y[1] += value
        st = (r.get("STATUS_DEMANDA") or "?").strip()
        a["por_status"][st] = a["por_status"].get(st, 0) + 1
        tp = (r.get("TIPO_PROCESSO") or "?").strip()
        a["por_tipo"][tp] = a["por_tipo"].get(tp, 0) + 1
        a["recentes"].append([iso, tp, (r.get("OBJETO") or "").strip() or None, st, round(value, 2), (r.get("NR_PROCESSO") or "").strip()])
        if len(a["recentes"]) > 40:
            a["recentes"] = sorted(a["recentes"], key=lambda x: (x[0], x[5]), reverse=True)[:5]
    for a in agg.values():
        a["recentes"] = sorted(a["recentes"], key=lambda x: (x[0], x[5]), reverse=True)[:5]
        a["valor_total"] = round(a["valor_total"], 2)
        for y in a["por_ano"].values():
            y[1] = round(y[1], 2)
    return {"by_operator": agg, "snapshot": snapshot}


def parse_readjustments(text: str) -> dict[str, Any]:
    """Readjustment applied to the small-group pool (collective contracts with < 30 lives)."""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in _rows(text):
        out[reg(r["REGISTRO_OPERADORA"])].append(
            {
                "ciclo": int(r["CICLO"]),
                "tipo": r["TP_AGRUPAMENTO"],
                "percentual": num(r["PCT_UNICO"]),
                "ambulatorial": num(r["PCT_AMBULATORIAL"]),
                "internacao_sem_obstetricia": num(r["PCT_INT_SEM_OBSTET"]),
                "internacao_com_obstetricia": num(r["PCT_INT_COM_OBSTET"]),
                "contratos": int(num(r["QT_CONTRATOS"]) or 0),
                "beneficiarios": int(num(r["QT_BENEF"]) or 0),
            }
        )
    return {k: sorted(v, key=lambda x: x["ciclo"])[-6:] for k, v in out.items()}


def parse_access(text: str) -> dict[str, Any]:
    """Quarterly 'garantia de atendimento' bands (0 best ... 3 worst)."""
    out: dict[str, list[list[Any]]] = defaultdict(list)
    for r in _rows(text):
        band = digits(r.get("POSICAO"))
        out[reg(r["REGISTRO_OPERADORA"])].append([r["DT_FIM_COMPETENCIA"], r["COMPETENCIA"], int(band) if band else None])
    return {k: sorted(v)[-4:] for k, v in out.items()}


DATASETS: dict[str, tuple[str, Callable[[str], Any], str]] = {
    # name: (path under BASE, parser, what it is)
    "operadoras_ativas": ("operadoras_de_plano_de_saude_ativas/Relatorio_cadop.csv", parse_operators_active, "Operadoras ativas (CADOP)"),
    "operadoras_canceladas": ("operadoras_de_plano_de_saude_canceladas/Relatorio_cadop_canceladas.csv", parse_operators_cancelled, "Operadoras canceladas (CADOP)"),
    "igr": ("IGR/IGR_versao_2023/pda-023-igr.csv", parse_igr, "Índice Geral de Reclamações (IGR)"),
    "idss": ("historico_idss-020/pda-020-historico-idss-2016_2025.csv", parse_idss, "Índice de Desempenho da Saúde Suplementar (IDSS)"),
    "penalidades": ("penalidades_aplicadas_a_operadoras/penalidades_aplicadas_a_operadoras.csv", parse_penalties, "Penalidades aplicadas a operadoras"),
    "reajustes": ("percentuais_de_reajuste_de_agrupamento-055/pda-055-Percentuais_de_Reajuste_de_Agrupamento.csv", parse_readjustments, "Reajuste do agrupamento de contratos (pool < 30 vidas)"),
    "garantia": ("monitoramento_garantia_atendimento/monit_garantia_atendimento.csv", parse_access, "Monitoramento da Garantia de Atendimento"),
}


class Store:
    """Loads each dataset lazily, from the cache or from ANS."""

    def __init__(self) -> None:
        self._mem: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.warnings: list[str] = []

    def _path(self, name: str) -> Path:
        return cache_dir() / f"{name}.json.gz"

    def _read(self, name: str) -> dict[str, Any] | None:
        p = self._path(name)
        if not p.exists():
            return None
        try:
            with gzip.open(p, "rt", encoding="utf-8") as f:
                d = json.load(f)
            return d if d.get("version") == CACHE_VERSION else None
        except Exception as exc:  # noqa: BLE001 — a corrupt cache is just a cache miss
            log.warning("ignoring unreadable cache %s: %s", p.name, exc)
            return None

    def _write(self, name: str, d: dict[str, Any]) -> None:
        p = self._path(name)
        tmp = p.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, p)

    def get(self, name: str, progress: Progress | None = None) -> Any:
        with self._lock:
            return self._get(name, progress or (lambda _m, _f: None))

    def _get(self, name: str, progress: Progress) -> Any:
        path, parser, title = DATASETS[name]
        url = f"{BASE}/{path}"
        d = self._mem.get(name) or self._read(name)
        if d and time.time() - d["checked_epoch"] < TRUST_SECONDS:
            self._mem[name] = d
            return d["data"]
        headers = {"User-Agent": USER_AGENT}
        try:
            with httpx.Client(timeout=httpx.Timeout(30.0, read=180.0), follow_redirects=True, headers=headers) as c:
                if d:
                    lm = c.head(url).headers.get("last-modified")
                    if lm and lm == d.get("last_modified"):
                        d["checked_epoch"] = time.time()
                        self._write(name, d)
                        self._mem[name] = d
                        return d["data"]
                progress(f"Baixando {title} da ANS…", None)
                log.info("downloading %s", url)
                r = c.get(url)
                r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            if d:
                self.warnings.append(f"Não foi possível atualizar '{title}' ({exc}); usando a cópia em cache de {d.get('last_modified') or 'data desconhecida'}.")
                self._mem[name] = d
                return d["data"]
            raise RuntimeError(f"Não foi possível baixar '{title}' da ANS: {exc}") from exc
        progress(f"Processando {title}…", None)
        d = {
            "version": CACHE_VERSION,
            "checked_epoch": time.time(),
            "last_modified": r.headers.get("last-modified"),
            "url": url,
            "data": parser(decode(r.content)),
        }
        self._write(name, d)
        self._mem[name] = d
        return d["data"]

    def source(self, name: str) -> dict[str, Any]:
        path, _parser, title = DATASETS[name]
        d = self._mem.get(name) or {}
        return {"dataset": title, "url": f"{BASE}/{path}", "publicado_pela_ans_em": d.get("last_modified")}

    def take_warnings(self) -> list[str]:
        w, self.warnings = self.warnings, []
        return w


_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store
