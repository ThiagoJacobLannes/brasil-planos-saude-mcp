"""FastMCP stdio server: compare Brazilian health-plan operators with ANS open data (read-only)."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Any, Callable

import anyio
import anyio.from_thread
import anyio.to_thread
from mcp.server.fastmcp import Context, FastMCP

from . import data
from .common import clamp
from .plans import Plans

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s brasil-planos-saude-mcp %(levelname)s %(message)s",
)
log = logging.getLogger("brasil_planos_saude_mcp")

mcp = FastMCP(
    "brasil-planos-saude",
    instructions=(
        "Compares Brazilian health-plan operators (planos de saúde) with official ANS open data: "
        "complaints index (IGR), quality score (IDSS), access-to-care monitoring, fines and the "
        "small-group readjustment. Answer in the user's language and always cite the month or year "
        "of each figure. It describes operators, not individual plans or prices. Not legal advice: "
        "for a denied procedure or a contract dispute, point to where_to_complain (Disque ANS "
        "0800 701 9656). The first call downloads the ANS files (about 60 MB) and can take a minute."
    ),
)

HELP = {
    "canais_ans": [
        {
            "canal": "Disque ANS",
            "contato": "0800 701 9656",
            "quando": "segunda a sexta (exceto feriados nacionais), das 8h às 20h",
        },
        {"canal": "Atendimento para pessoas com deficiência auditiva", "contato": "0800 021 2105"},
        {
            "canal": "Atendimento eletrônico (abrir reclamação / NIP)",
            "contato": "https://www.ans.gov.br/nip_solicitante/",
        },
        {
            "canal": "Atendimento presencial (Núcleos da ANS)",
            "contato": "https://www.gov.br/ans/pt-br/canais_atendimento/nossos-enderecos",
        },
    ],
    "como_funciona": [
        "Primeiro fale com a operadora e anote o número de protocolo: a ANS pede esse protocolo.",
        "Se não resolver, registre a reclamação na ANS. Ela abre uma Notificação de Intermediação "
        "Preliminar (NIP) e a operadora tem prazo curto para responder; o resultado entra no IGR.",
        "Em urgência ou emergência, procure atendimento primeiro; a discussão de cobertura vem depois.",
    ],
    "conferido_em": "https://www.gov.br/ans/pt-br/canais_atendimento/canais-de-atendimento-ao-consumidor",
    "aviso": "Informação, não aconselhamento jurídico. Para disputas, procure também o Procon ou a Defensoria Pública.",
}


async def _run(fn: Callable[[data.Progress], Any], ctx: Context | None = None) -> Any:
    """Run blocking work in a thread; forward progress to the client when it asked for it."""

    def progress(message: str, fraction: float | None) -> None:
        if ctx is None:
            return
        try:
            anyio.from_thread.run(ctx.report_progress, round((fraction or 0) * 100, 1), 100.0, message)
        except TypeError:  # older mcp without the `message` argument
            try:
                anyio.from_thread.run(ctx.report_progress, round((fraction or 0) * 100, 1), 100.0)
            except Exception:  # noqa: BLE001 — progress is best effort
                pass
        except Exception:  # noqa: BLE001
            pass

    def work() -> Any:
        res = fn(progress)
        warnings = data.store().take_warnings()
        if warnings and isinstance(res, dict):
            res["avisos"] = warnings
        return res

    return await anyio.to_thread.run_sync(work)


def _fail(tool: str, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ValueError):  # bad input: no traceback needed
        log.warning("%s: %s", tool, exc)
    else:
        log.exception("%s failed", tool)
    return {"error": str(exc)}


@mcp.tool()
async def find_operators(
    query: str | None = None,
    uf: str | None = None,
    city: str | None = None,
    modality: str | None = None,
    include_cancelled: bool = False,
    limit: int = 10,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Find Brazilian health-plan operators (operadoras de planos de saúde) registered with ANS.

    Use it to turn a brand the user mentions ("Unimed BH", "Amil", "Bradesco Saúde") into the
    ANS registry number, or to list operators by state, city or type. Largest first.

    Args:
        query:             Part of the name, the CNPJ, or the ANS registry number. Accents and
                           case don't matter. Several words must all match ("unimed campinas").
        uf:                State of the operator's head office, e.g. "SP", "MG".
        city:              City of the head office, e.g. "Belo Horizonte".
        modality:          e.g. "Cooperativa Médica", "Medicina de Grupo", "Seguradora",
                           "Autogestão", "Odontologia de Grupo", "Filantropia",
                           "Administradora de Benefícios" (substring match).
        include_cancelled: Also list operators whose registration was cancelled.
        limit:             How many to return (default 10, max 50).

    Each operator: registro_ans, name, legal name, modality, head office, status, and beneficiaries
    and size class (porte) per coverage. Head office is not where the operator sells plans.
    """
    try:
        return await _run(lambda p: Plans(data.store(), p).find(query, uf, city, modality, include_cancelled, limit), ctx)
    except Exception as exc:  # noqa: BLE001 — surface a clean message to the model
        return _fail("find_operators", exc)


@mcp.tool()
async def operator_profile(operator: str, coverage: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """Everything ANS publishes about one health-plan operator, to judge whether it is a good choice.

    Use for "is Hapvida good?", "how does Unimed Campinas rank on complaints?", "has SulAmérica been
    fined?". Returns:
      - registration: legal name, CNPJ, modality, head office, phone, status (active or cancelled,
        with date and reason), beneficiaries and size class;
      - complaints (IGR): latest month, 12-month average, the median of operators of the same size,
        the operator's position among them, and the monthly series;
      - quality (IDSS, 0-1, higher is better) for the last 5 years with its four dimensions;
      - access-to-care monitoring band (0 best, 3 worst) for the last published quarters;
      - fines applied by ANS: totals, last 5 years, status and the most recent cases;
      - the readjustment applied to its small-group pool (contracts with < 30 lives).

    Args:
        operator: Name (or part of it), CNPJ, or ANS registry number. If the name matches several
                  operators, the largest active one is used and the others are listed in
                  outras_operadoras_com_nome_parecido: check it is the one the user meant
                  (Unimed, for example, is hundreds of separate cooperatives).
        coverage: "medica" or "odonto". Default: every coverage the operator has.

    Always quote the month/year of each figure and explain that IGR is lower-is-better and IDSS
    higher-is-better (see como_ler in the result).
    """
    try:
        return await _run(lambda p: Plans(data.store(), p).profile(operator, coverage), ctx)
    except Exception as exc:  # noqa: BLE001
        return _fail("operator_profile", exc)


@mcp.tool()
async def compare_operators(operators: list[str], coverage: str = "medica", ctx: Context | None = None) -> dict[str, Any]:
    """Side-by-side comparison of 2 to 8 health-plan operators on the ANS indicators.

    Use for "Amil or Bradesco Saúde?", "compare the plans my company offers". For each operator:
    12-month complaints index (IGR) with the median of its size class and its position, latest
    IDSS, latest access-to-care band, number of fines in the last 5 years, and the latest
    small-group readjustment.

    Args:
        operators: Names, CNPJs or ANS registry numbers, e.g. ["Amil", "Bradesco Saúde", "SulAmérica"].
        coverage:  "medica" (default) or "odonto".
    """
    try:
        if not operators:
            raise ValueError("Informe ao menos uma operadora.")
        return await _run(lambda p: Plans(data.store(), p).compare(list(operators), coverage), ctx)
    except Exception as exc:  # noqa: BLE001
        return _fail("compare_operators", exc)


@mcp.tool()
async def rank_operators(
    by: str = "reclamacoes",
    coverage: str = "medica",
    porte: str | None = None,
    uf: str | None = None,
    modality: str | None = None,
    min_beneficiaries: int = 5000,
    worst_first: bool = False,
    limit: int = 10,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Rank active health-plan operators by complaints or by quality.

    Use for "which large health plans get the fewest complaints?", "best-rated dental plans",
    "worst operators in Minas Gerais".

    Args:
        by:                "reclamacoes" (12-month IGR, lower is better) or "qualidade" (latest IDSS,
                           higher is better).
        coverage:          "medica" (default) or "odonto".
        porte:             "pequeno" (< 20k beneficiaries), "medio" (20k-100k) or "grande" (> 100k).
                           ANS itself only compares operators of the same size.
        uf:                Head-office state, e.g. "MG". Not the same as where it sells plans.
        modality:          e.g. "Cooperativa Médica", "Medicina de Grupo", "Seguradora".
        min_beneficiaries: Skip operators smaller than this (default 5000; tiny ones swing wildly).
        worst_first:       True to list the worst first.
        limit:             How many to return (default 10, max 50).
    """
    try:
        return await _run(
            lambda p: Plans(data.store(), p).rank(by, coverage, porte, uf, modality, max(0, int(min_beneficiaries)), worst_first, clamp(limit, 1, 50)),
            ctx,
        )
    except Exception as exc:  # noqa: BLE001
        return _fail("rank_operators", exc)


@mcp.tool()
def where_to_complain() -> dict[str, Any]:
    """How to complain about a health plan in Brazil: the official ANS channels and the steps.

    Use when a user's plan denied or delayed a procedure, or they have a contract problem.
    Each channel was checked on the ANS consumer-service page (conferido_em).
    """
    return HELP


def main() -> None:
    ap = argparse.ArgumentParser(prog="brasil-planos-saude-mcp", description=__doc__)
    ap.add_argument("--refresh", action="store_true", help="download the ANS files into the local cache now, then exit")
    args = ap.parse_args()
    if args.refresh:
        t0 = time.time()
        s = data.store()
        for name in data.DATASETS:
            s.get(name, lambda msg, _f: log.info(msg))
        log.info("cache ready in %s (%.0fs)", data.cache_dir(), time.time() - t0)
        return
    mcp.run()


if __name__ == "__main__":
    main()
