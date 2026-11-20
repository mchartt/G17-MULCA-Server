"""
The server's own sentences in the group's language (20/11: an Italian group
got "Dependency saved: ..." right after Cadence spoke Italian, spec 4.3).

    italian(language, sample)  the group chose Italian, or chose nothing and
                               `sample` (a card's rationale, a member's
                               message) is Italian
    localize(text, it)         the English notice of a click, in Italian
                               when `it`; unknown sentences stay as they are
    sources_label(it)          the words of the line naming an answer's sources

The logic keeps producing English (its tests read it); only what reaches the
members is translated, here, in one place.
"""

from __future__ import annotations

import re
from collections.abc import Callable

_ITALIAN_WORDS = re.compile(
    r"\b(il|la|di|che|è|per|un|una|del|della|sono|chi|si|non|perché|anche|ma|sì|deve|dai|dal|questa|questo|"
    r"prima|fallo|aggiungi|crea|collega|intervengo|dipende|serve|servono|"
    # 20/11 live: "procede, stasera lo chiudo" + "Grazie Matías, ricevuto!" read as English ("From:").
    r"grazie|ricevuto|bene|stasera|domani|oggi|ancora|fatto|ho|sto|va|procede|ciao|capito|perfetto)\b",
    re.IGNORECASE,
)


def italian(language: str, sample: str) -> bool:
    if language in ("it", "en"):
        return language == "it"
    return len(_ITALIAN_WORDS.findall(sample or "")) >= 1


# A quoted title in any quote style: “x”, «x» or "x".
Q = '[“«"]([^”»"]*)[”»"]'
_STATUS = {"applied": "applicata", "rejected": "rifiutata", "expired": "scaduta", "snoozed": "messa in pausa", "pending": "in attesa"}


def _already(match: re.Match[str]) -> str:
    return f"Questa card è già {_STATUS.get(match.group(1), match.group(1))}."


_RULES: list[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]]] = [
    # A trailing "<member> owns “X”." after another sentence goes first, while the text is still English.
    (re.compile(r"(?<=\. )([^.“«\"]+?) owns " + Q + r"\.$"), "«\\2» è di \\1."),
    (re.compile(r"^Dependency saved: " + Q + " waits on " + Q + r"\.(.*)$"), "Dipendenza salvata: «\\1» aspetta «\\2».\\3"),
    (re.compile(r"^Dependency saved: " + Q + r" waits on the task on the card\.(.*)$"),
     "Dipendenza salvata: «\\1» aspetta l'attività della card.\\2"),
    (re.compile(r"^Marked as blocked: " + Q + r"\.(.*)$"), "Segnata come bloccata: «\\1».\\2"),
    (re.compile(r"^Saved: (.+?) owns " + Q + r"\.$"), "Salvato: «\\2» è di \\1."),
    (re.compile(r"^Decision saved: (.+)\.$"), "Decisione salvata: \\1."),
    (re.compile(r"^Plan saved: (\d+) task\(s\)\.$"), "Piano salvato: \\1 attività."),
    (re.compile(r"^Plan updated: (.+)\.$"), "Piano aggiornato: \\1."),
    (re.compile(r"^Kept: " + Q + r" stays the decision\.$"), "Confermata: «\\1» resta la decisione."),
    (re.compile(r"^Reopened: " + Q + r" is open again\. The old choice and its reason stay in the log\.$"),
     "Riaperta: «\\1» è di nuovo aperta. La scelta precedente e il suo motivo restano nel registro."),
    (re.compile(r"^Got it: no more suggestions like this about " + Q + r" for (\d+) minutes\.$"),
     "Ok: niente più suggerimenti come questo su «\\1» per \\2 minuti."),
    (re.compile(r"^This card was already (\w+)\.$"), _already),
    (re.compile(r"^This card is no longer valid: (.*)$"), "Questa card non è più valida: \\1"),
]
_FIXED = {
    "Confirmed and saved.": "Confermato e salvato.",
    "Rejected.": "Rifiutata.",
    "Saved: how you plan the work is in the decisions.": "Salvato: come pianificate il lavoro è tra le decisioni.",
    "Only members of this group can answer this card.": "Solo i membri del gruppo possono rispondere a questa card.",
    "This card no longer exists.": "Questa card non esiste più.",
    "This card has expired.": "Questa card è scaduta.",
    "Someone else answered this card first.": "Qualcun altro ha già risposto a questa card.",
    "Not now is only for cards the coordinator posted on its own.": "«Non ora» vale solo per le card proposte da Cadence di sua iniziativa.",
    "Nobody is suggested on this card: press “I'll take it”.": "La card non suggerisce nessuno: premi «Me ne occupo io».",
    "The suggested owner is not in this group any more.": "La persona suggerita non fa più parte del gruppo.",
    "Choose one of the options on the card.": "Scegli una delle opzioni della card.",
    "That member is not in this group any more.": "Quella persona non fa più parte del gruppo.",
    "The member named on the card is not in this group any more.": "La persona indicata sulla card non fa più parte del gruppo.",
    "Choose Keep or Reopen.": "Scegli Mantieni o Riapri.",
    "This task is no longer in the plan.": "Questa attività non è più nel piano.",
    "This link would make the plan go round in a circle, so it was not saved.":
        "Questo collegamento creerebbe un giro chiuso nel piano, quindi non è stato salvato.",
    "A task cannot wait on itself.": "Un'attività non può aspettare sé stessa.",
    "The coordinator is already working on a request for this group: its answer will appear here.":
        "Cadence sta già rispondendo a una richiesta di questo gruppo: la risposta comparirà qui.",
}


def localize(text: str, it: bool) -> str:
    """The notice in Italian when `it`, else unchanged."""
    if not it or not text:
        return text
    if text in _FIXED:
        return _FIXED[text]
    out = text
    for pattern, replacement in _RULES:
        out = pattern.sub(replacement, out)
    return out


def sources_label(it: bool) -> tuple[str, str, str, str]:
    """(line prefix, task, milestone, decision) for the line naming what an answer rests on."""
    return ("Fonti", "attività", "milestone", "decisione") if it else ("From", "task", "milestone", "decision")
