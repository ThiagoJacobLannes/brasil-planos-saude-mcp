"""Answers about Brazilian health-plan operators, built on the ANS datasets in data.py."""

from __future__ import annotations

import re
from typing import Any

from .common import clamp, digits, median, norm, reg
from .data import Progress, Store

COVERAGES = {"medica": "Assistência médica", "odonto": "Exclusivamente odontológica"}
PORTES = {"pequeno": "Pequeno", "medio": "Médio", "grande": "Grande"}

HOW_TO_READ = {
    "igr": (
        "Índice Geral de Reclamações: reclamações por 100 mil beneficiários no mês (metodologia de 2023). "
        "Quanto MENOR, melhor. A ANS compara operadoras do mesmo porte (pequeno < 20 mil, médio 20-100 mil, "
        "grande > 100 mil beneficiários); um mês isolado oscila, prefira a média de 12 meses."
    ),
    "idss": (
        "Índice de Desempenho da Saúde Suplementar, de 0 a 1: quanto MAIOR, melhor. Sai uma vez por ano "
        "(ano do programa, avaliando o ano-base anterior). Dimensões: IDQS qualidade em atenção à saúde, "
        "IDGA garantia de acesso, IDSM sustentabilidade no mercado, IDGR gestão de processos e regulação."
    ),
    "garantia_de_atendimento": (
        "Monitoramento trimestral da garantia de atendimento (negativas e demoras de cobertura). Faixa 0 = "
        "nenhuma reclamação procedente; 1 = abaixo da mediana; 3 = mais de 50% acima da mediana. Faixa 3 "
        "recorrente leva à suspensão da venda de planos. Diga sempre o trimestre: a ANS pode demorar a publicar "
        "novos resultados no arquivo aberto."
    ),
    "penalidades": (
        "Multas aplicadas pela ANS (valor da decisão final, ou da última instância), inclusive as que ainda "
        "estão em recurso, suspensas ou arquivadas: não é o valor pago. Operadoras maiores tendem a ter mais "
        "processos: compare em proporção ao tamanho. O arquivo aberto vai só até 'dados_ate'."
    ),
    "reajuste_agrupamento": (
        "Reajuste anual que a operadora aplicou ao 'pool' de contratos coletivos com menos de 30 vidas "
        "(PME e por adesão pequenos). Não vale para planos individuais/familiares, cujo teto é definido "
        "pela ANS, nem para coletivos grandes, que negociam caso a caso."
    ),
}


def coverage_key(c: str | None) -> str:
    n = norm(c)
    if not n or n.startswith(("med", "saude", "hosp", "assist")):
        return "medica"
    if n.startswith(("odont", "dent")):
        return "odonto"
    raise ValueError(f"Cobertura desconhecida '{c}': use 'medica' ou 'odonto'.")


def porte_key(p: str | None) -> str | None:
    if not p:
        return None
    n = norm(p)
    for k, v in PORTES.items():
        if n.startswith(k[:3]):
            return v
    raise ValueError(f"Porte desconhecido '{p}': use 'pequeno', 'medio' ou 'grande'.")


class Plans:
    def __init__(self, store: Store, progress: Progress | None = None) -> None:
        self.s = store
        self.p = progress
        self._ops: dict[str, dict[str, Any]] | None = None
        self._latest: str | None = None

    # --- data -------------------------------------------------------------------------

    def ds(self, name: str) -> Any:
        return self.s.get(name, self.p)

    def operators(self) -> dict[str, dict[str, Any]]:
        if self._ops is None:
            ops = dict(self.ds("operadoras_canceladas"))
            ops.update(self.ds("operadoras_ativas"))  # active wins when a registry appears in both
            self._ops = ops
        return self._ops

    def igr_rows(self, k: str, cov: str) -> list[list[Any]]:
        """IGR rows for one operator and coverage; [] if it stopped reporting more than a year ago."""
        rows = self.ds("igr")["by_operator"].get(k, {}).get(cov, [])
        latest = self.latest_competencia()
        year_ago = f"{int(latest[:4]) - 1}{latest[4:]}"
        return rows if rows and rows[-1][0] > year_ago else []

    def latest_competencia(self) -> str:
        if self._latest is None:
            self._latest = max(r[-1][0] for covs in self.ds("igr")["by_operator"].values() for r in covs.values() if r)
        return self._latest

    def size(self, k: str) -> dict[str, Any]:
        """Beneficiaries and size class per coverage, from the latest IGR month."""
        out = {}
        for cov in COVERAGES:
            rows = self.igr_rows(k, cov)
            if rows:
                out[cov] = {"beneficiarios": rows[-1][3], "porte": rows[-1][4], "competencia": rows[-1][0]}
        return out

    # --- resolving a name, CNPJ or registry --------------------------------------------

    def search(self, query: str | None, uf: str | None = None, city: str | None = None,
               modality: str | None = None, include_cancelled: bool = False) -> list[dict[str, Any]]:
        ops = self.operators()
        q = (query or "").strip()
        d = digits(q)
        if d and re.fullmatch(r"[\d.\-/ ]+", q):  # a CNPJ or an ANS registry number
            if len(d) == 14:
                hits = [o for o in ops.values() if o["cnpj"] == d]
            else:
                hits = [ops[reg(d)]] if reg(d) in ops else []
            return hits
        toks = norm(q).split()
        nuf, ncity, nmod = norm(uf), norm(city), norm(modality)
        hits = []
        for o in ops.values():
            if not include_cancelled and o["situacao"] != "ativa":
                continue
            if nuf and norm(o["sede"]["uf"]) != nuf:
                continue
            if ncity and norm(o["sede"]["cidade"]) != ncity:
                continue
            if nmod and nmod not in norm(o["modalidade"]):
                continue
            hay = f" {norm(o['razao_social'])} {norm(o['nome_fantasia'])} "
            # "sulamerica" should find "SUL AMERICA": also try with the spaces taken out.
            if toks and not all(f" {t}" in hay for t in toks) and "".join(toks) not in hay.replace(" ", ""):
                continue
            hits.append(o)
        qn = norm(q)

        def rank(o: dict[str, Any]) -> tuple:
            exact = qn and qn in (norm(o["razao_social"]), norm(o["nome_fantasia"]))
            ben = max((v["beneficiarios"] for v in self.size(o["registro_ans"]).values()), default=0)
            return (o["situacao"] != "ativa", not exact, -ben)

        return sorted(hits, key=rank)

    def resolve(self, query: str) -> dict[str, Any]:
        hits = self.search(query, include_cancelled=True)
        if not hits:
            raise ValueError(
                f"Nenhuma operadora encontrada para '{query}'. Tente parte do nome ('Unimed Campinas'), "
                "o CNPJ ou o número de registro na ANS; find_operators lista candidatas."
            )
        qn = norm(query)
        exact = [o for o in hits if qn in (norm(o["razao_social"]), norm(o["nome_fantasia"])) and o["situacao"] == "ativa"]
        active = [o for o in hits if o["situacao"] == "ativa"]
        top = sorted(active, key=lambda o: -max((v["beneficiarios"] for v in self.size(o["registro_ans"]).values()), default=0))[:8]
        # An exact name wins, unless the name is a shared brand and that exact match is a small one.
        if len(exact) == 1 and (len(active) <= 5 or exact[0] in top[:3]):
            return exact[0]
        if len(active) > 5:  # "Unimed" is hundreds of separate cooperatives: don't pick one at random
            names = "; ".join(f"{o['nome_fantasia'] or o['razao_social']} ({o['registro_ans']})" for o in top)
            raise ValueError(
                f"'{query}' corresponde a {len(active)} operadoras ativas. Seja mais específico ou use o registro ANS. "
                f"Maiores: {names}."
            )
        return hits[0]

    def alternatives(self, query: str, chosen: dict[str, Any]) -> list[dict[str, Any]]:
        return [self.brief(o) for o in self.search(query, include_cancelled=True)[1:6] if o is not chosen]

    # --- building blocks ---------------------------------------------------------------

    def brief(self, o: dict[str, Any]) -> dict[str, Any]:
        return {
            "registro_ans": o["registro_ans"],
            "nome": o["nome_fantasia"] or o["razao_social"],
            "razao_social": o["razao_social"],
            "modalidade": o["modalidade"],
            "sede": f"{o['sede']['cidade']}/{o['sede']['uf']}",
            "situacao": o["situacao"],
            "tamanho": self.size(o["registro_ans"]),
        }

    def porte_stats(self, cov: str, comp: str) -> dict[str, dict[str, Any]]:
        """Median IGR and the sorted list of 12-month IGRs per size class, for one month."""
        groups: dict[str, list[tuple[float, str]]] = {}
        for k, covs in self.ds("igr")["by_operator"].items():
            rows = covs.get(cov) or []
            if rows and rows[-1][0] == comp and rows[-1][1] is not None:
                avg = self.igr_12m(rows)
                if avg is not None:
                    groups.setdefault(rows[-1][4], []).append((avg, k))
        return {p: {"mediana_igr_12m": median(v for v, _ in xs), "ordem": [k for _, k in sorted(xs)]} for p, xs in groups.items()}

    @staticmethod
    def igr_12m(rows: list[list[Any]]) -> float | None:
        last = rows[-12:]
        comp = sum(r[2] for r in last)
        ben = sum(r[3] for r in last)
        return round(comp / ben * 100_000, 2) if ben else None

    def complaints(self, k: str, cov: str, months: int = 12) -> dict[str, Any] | None:
        rows = self.igr_rows(k, cov)
        if not rows:
            return None
        comp = rows[-1][0]
        stats = self.porte_stats(cov, comp).get(rows[-1][4], {})
        order = stats.get("ordem", [])
        return {
            "competencia": comp,
            "igr_mes": rows[-1][1],
            "reclamacoes_mes": rows[-1][2],
            "igr_media_12m": self.igr_12m(rows),
            "reclamacoes_12m": sum(r[2] for r in rows[-12:]),
            "porte": rows[-1][4],
            "mediana_do_porte_igr_12m": stats.get("mediana_igr_12m"),
            "posicao_no_porte": (
                {"posicao": order.index(k) + 1, "de": len(order), "nota": "1 = menos reclamações no porte, pela média de 12 meses"}
                if k in order else None
            ),
            "serie_mensal": [{"competencia": r[0], "igr": r[1], "reclamacoes": r[2]} for r in rows[-months:]],
        }

    def quality(self, k: str, years: int = 5) -> dict[str, Any] | None:
        h = self.ds("idss").get(k)
        if not h:
            return None
        items = [(y, v) for y, v in h.items() if v.get("idss") is not None][-years:]
        if not items:
            return None
        return {"ultimo": {"ano": items[-1][0], **items[-1][1]}, "historico": [{"ano": y, **v} for y, v in items]}

    def latest_idss_year(self) -> str:
        return max(y for h in self.ds("idss").values() for y, v in h.items() if v.get("idss") is not None)

    def penalties(self, k: str) -> dict[str, Any] | None:
        p = self.ds("penalidades")["by_operator"].get(k)
        if not p:
            return None
        years = sorted(p["por_ano"].items())[-5:]
        snapshot = self.ds("penalidades")["snapshot"]
        return {
            "total_de_processos": p["total"],
            "valor_total_aplicado_brl": p["valor_total"],
            "ultimos_5_anos": [
                {"ano": y, "processos": n, "valor_aplicado_brl": v, **({"ano_parcial": True} if y == snapshot[:4] else {})}
                for y, (n, v) in years
            ],
            "por_status": p["por_status"],
            "por_tipo": p["por_tipo"],
            "mais_recentes": [
                {"decisao_1a_instancia": d, "tipo": t, "objeto": ob, "status": st, "valor_aplicado_brl": v, "processo": pr}
                for d, t, ob, st, v, pr in p["recentes"]
            ],
            "dados_ate": f"{snapshot[:4]}-{snapshot[4:]}" if len(snapshot) == 6 else snapshot,
        }

    def readjustments(self, k: str) -> list[dict[str, Any]] | None:
        return self.ds("reajustes").get(k) or None

    def access(self, k: str) -> list[dict[str, Any]] | None:
        rows = self.ds("garantia").get(k)
        return [{"trimestre": t, "faixa": f} for _end, t, f in rows] if rows else None

    def sources(self, *names: str) -> list[dict[str, Any]]:
        return [self.s.source(n) for n in names]

    # --- tools -------------------------------------------------------------------------

    def find(self, query, uf, city, modality, include_cancelled, limit) -> dict[str, Any]:
        hits = self.search(query, uf, city, modality, include_cancelled)
        return {
            "encontradas": len(hits),
            "operadoras": [self.brief(o) for o in hits[: clamp(limit, 1, 50)]],
            "nota": "A UF e a cidade são as da sede da operadora, não a área onde ela vende planos.",
            "fontes": self.sources("operadoras_ativas", "operadoras_canceladas", "igr"),
        }

    def profile(self, query: str, coverage: str | None) -> dict[str, Any]:
        o = self.resolve(query)
        k = o["registro_ans"]
        covs = [coverage_key(coverage)] if coverage else [c for c in COVERAGES if self.igr_rows(k, c)] or ["medica"]
        out: dict[str, Any] = {
            "operadora": {**o, "tamanho": self.size(k)},
            "reclamacoes_igr": {COVERAGES[c]: self.complaints(k, c) for c in covs},
            "qualidade_idss": self.quality(k),
            "garantia_de_atendimento": self.access(k),
            "penalidades": self.penalties(k),
            "reajuste_agrupamento_menos_de_30_vidas": self.readjustments(k),
            "como_ler": HOW_TO_READ,
            "fontes": self.sources(*self.s_names()),
        }
        alts = self.alternatives(query, o) if not digits(query) else []
        if alts:
            out["outras_operadoras_com_nome_parecido"] = alts
        return out

    def compare(self, queries: list[str], coverage: str | None) -> dict[str, Any]:
        cov = coverage_key(coverage)
        rows, missing = [], []
        for q in queries[:8]:
            try:
                o = self.resolve(q)
            except ValueError as exc:
                missing.append({"consulta": q, "motivo": str(exc)})
                continue
            k = o["registro_ans"]
            c = self.complaints(k, cov, months=0) or {}
            qi = self.quality(k, years=1) or {}
            pen = self.penalties(k) or {}
            rj = self.readjustments(k) or []
            acc = self.access(k) or []
            rows.append(
                {
                    "consulta": q,
                    **self.brief(o),
                    "igr_media_12m": c.get("igr_media_12m"),
                    "mediana_do_porte_igr_12m": c.get("mediana_do_porte_igr_12m"),
                    "posicao_no_porte": c.get("posicao_no_porte"),
                    "idss": (qi.get("ultimo") or {}).get("idss"),
                    "idss_ano": (qi.get("ultimo") or {}).get("ano"),
                    "garantia_ultima_faixa": acc[-1] if acc else None,
                    "multas_ultimos_5_anos": sum(y["processos"] for y in pen.get("ultimos_5_anos", [])) if pen else 0,
                    "reajuste_agrupamento_ultimo": {"ciclo": rj[-1]["ciclo"], "percentual": rj[-1]["percentual"]} if rj else None,
                }
            )
        out: dict[str, Any] = {"cobertura": COVERAGES[cov], "operadoras": rows, "como_ler": HOW_TO_READ, "fontes": self.sources(*self.s_names())}
        if missing:
            out["nao_resolvidas"] = missing
        return out

    def rank(self, by, coverage, porte, uf, modality, min_beneficiaries, worst, limit) -> dict[str, Any]:
        cov = coverage_key(coverage)
        por = porte_key(porte)
        by_n = norm(by)
        if by_n.startswith(("rec", "igr", "compl")):
            metric = "reclamacoes"
        elif by_n.startswith(("qual", "idss")):
            metric = "qualidade"
        else:
            raise ValueError("Use by='reclamacoes' (IGR, menor é melhor) ou by='qualidade' (IDSS, maior é melhor).")
        comp = self.latest_competencia()
        idss_year = self.latest_idss_year()
        ops = self.operators()
        items = []
        for k, covs in self.ds("igr")["by_operator"].items():
            rows = covs.get(cov) or []
            o = ops.get(k)
            if not rows or not o or o["situacao"] != "ativa" or rows[-1][0] != comp:
                continue
            ben, p = rows[-1][3], rows[-1][4]
            if ben < min_beneficiaries or (por and p != por):
                continue
            if uf and norm(o["sede"]["uf"]) != norm(uf):
                continue
            if modality and norm(modality) not in norm(o["modalidade"]):
                continue
            q = (self.quality(k, years=1) or {}).get("ultimo")
            last_idss = (q["ano"], q) if q else None
            if metric == "reclamacoes":
                value = self.igr_12m(rows)
            else:  # only operators scored in the latest IDSS year, so the ranking is like for like
                value = q["idss"] if q and q["ano"] == idss_year else None
            if value is None:
                continue
            items.append(
                {
                    "registro_ans": k,
                    "nome": o["nome_fantasia"] or o["razao_social"],
                    "modalidade": o["modalidade"],
                    "sede": f"{o['sede']['cidade']}/{o['sede']['uf']}",
                    "beneficiarios": ben,
                    "porte": p,
                    "igr_media_12m": self.igr_12m(rows),
                    "idss": last_idss[1].get("idss") if last_idss else None,
                    "idss_ano": last_idss[0] if last_idss else None,
                    "_v": value,
                }
            )
        best_first = metric == "reclamacoes"
        items.sort(key=lambda x: (x["_v"], -x["beneficiarios"]), reverse=(best_first == bool(worst)))
        for x in items:
            x.pop("_v")
        return {
            "criterio": "IGR médio de 12 meses (menor é melhor)" if metric == "reclamacoes" else f"IDSS {idss_year} (maior é melhor)",
            "ordem": "piores primeiro" if worst else "melhores primeiro",
            "cobertura": COVERAGES[cov],
            "competencia_igr": comp,
            "total_no_filtro": len(items),
            "operadoras": items[: clamp(limit, 1, 50)],
            "notas": [
                "Só operadoras ativas com dado no mês mais recente.",
                "Operadoras pequenas oscilam muito: compare dentro do mesmo porte e use min_beneficiaries.",
                "A UF é a da sede, não a área onde a operadora vende planos.",
            ],
            "como_ler": {k: HOW_TO_READ[k] for k in ("igr", "idss")},
            "fontes": self.sources("igr", "idss", "operadoras_ativas"),
        }

    @staticmethod
    def s_names() -> tuple[str, ...]:
        return ("operadoras_ativas", "operadoras_canceladas", "igr", "idss", "garantia", "penalidades", "reajustes")
