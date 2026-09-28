"""Elenco, matriz de estados e prompts de geração para a ectoscopia clínica.

Estrutura
---------
* ``ACTORS``  — 4 personas (A01..A04) com aparência, cenário e vieses clínicos.
* ``STATES``  — 5 estados físicos, cada um com direção de atuação e respiração.
* ``build_prompt(actor_id, state_id)`` — compõe o prompt final entregue ao Veo.

Por que os prompts estão em inglês
----------------------------------
O Veo 3.x responde de forma mensuravelmente mais aderente a prompts em inglês;
os metadados legíveis (``descricao``, ``rotulo``) permanecem em português para
alimentar planilha e manifesto. Editar a persona significa editar apenas os
campos ``aparencia``/``cenario`` — a gramática do prompt é montada aqui.

Regras clínicas inegociáveis (aplicadas a TODOS os clipes)
----------------------------------------------------------
1. Boca estritamente fechada em todos os frames — zero lip-sync, zero fala.
2. Câmera fixa em tripé — sem pan, tilt, zoom, dolly, handheld ou parallax.
3. Nenhum sinal patognomônico que entregue o diagnóstico (sem icterícia franca,
   sem cianose marcada, sem exantema, sem assimetria facial, sem equimoses,
   sem desvio de rima, sem estigmas de doença específica).
4. Nenhum equipamento que revele a hipótese (sem nebulizador, sem dreno,
   sem colar cervical, sem monitor com traçado legível).
"""

from __future__ import annotations

from typing import Dict, List

# --------------------------------------------------------------------------- #
# Blocos compartilhados
# --------------------------------------------------------------------------- #
CAMERA_BLOCK = (
    "Static locked-off tripod shot, single continuous take, no camera movement "
    "whatsoever: no pan, no tilt, no zoom, no dolly, no handheld shake, no rack focus. "
    "Medium close-up framing head and upper chest, subject centred, eye-level lens, "
    "35mm equivalent, shallow-to-moderate depth of field with the background softly "
    "out of focus. Neutral, even, diffuse clinical lighting with no dramatic shadows "
    "and no colour cast. Photorealistic documentary realism, natural skin texture, "
    "no stylisation, no film grain, no vignette, no on-screen text or overlays."
)

CLINICAL_CONSTRAINTS = (
    "The mouth stays completely closed and still in every single frame: lips gently "
    "sealed, no speaking, no lip movement, no lip-sync, no yawning, no visible teeth "
    "or tongue. Breathing happens through the nose and is read from the chest and "
    "shoulders only. The subject shows a general, non-specific physical state and "
    "never a diagnosis-revealing sign: no jaundice, no marked cyanosis, no rash, no "
    "bruising, no facial asymmetry, no drooping, no visible wounds, no sweat beading "
    "into rivulets, no medical devices, tubing, masks or monitors in frame. "
    "The performance is restrained and clinically plausible, never theatrical."
)

NEGATIVE_PROMPT = (
    "talking, speaking, open mouth, lip movement, lip sync, mouth opening, visible "
    "teeth, tongue, yawning, shouting, camera movement, pan, tilt, zoom, dolly, "
    "handheld shake, whip pan, cut, scene change, jump cut, multiple shots, text, "
    "captions, subtitles, watermark, logo, timestamp, UI overlay, medical equipment, "
    "oxygen mask, nasal cannula, nebuliser, ventilator, ECG monitor, IV pole, "
    "stethoscope, syringe, bandage, cast, neck collar, blood, wounds, rash, jaundice, "
    "cyanosis, bruises, facial droop, facial asymmetry, exaggerated grimace, crying, "
    "theatrical acting, cartoon, anime, illustration, 3d render, cgi, plastic skin, "
    "beauty filter, heavy makeup, distorted hands, extra fingers, deformed anatomy, "
    "low resolution, blurry, flicker, strobing"
)

# A "gramática de loop": a ponta e o início do clipe precisam casar, porque o
# pós-processamento costura [seg, 2*seg) com [0, seg) via crossfade.
LOOP_HINT = (
    "The subject's posture and framing at the end of the clip match the beginning, "
    "so the motion reads as a calm continuous cycle with no reset or freeze."
)


# --------------------------------------------------------------------------- #
# Estados físicos (5 por ator)
# --------------------------------------------------------------------------- #
STATES: Dict[str, Dict[str, str]] = {
    "basal": {
        "rotulo": "Basal",
        "descricao": "Repouso tranquilo, sem desconforto aparente.",
        # NB: as direções de estado são deliberadamente agnósticas quanto à
        # postura — quem estabelece "sentado" ou "deitado" é o SETTING de cada
        # persona. Falar em "cadeira" aqui contradiria a A03, que está em leito.
        "direcao": (
            "Calm baseline state. The subject is quietly at rest, facial muscles "
            "relaxed, brow smooth, gaze steady and forward with slow natural blinks. "
            "Shoulders are dropped and symmetrical, hands resting still."
        ),
        "respiracao": (
            "Quiet regular nasal breathing at roughly 14 breaths per minute, shallow "
            "and even chest rise, no accessory muscle use, no audible effort."
        ),
        "intensidade": "0 (ausente)",
    },
    "desconforto_leve": {
        "rotulo": "Desconforto leve",
        "descricao": "Incômodo discreto, ainda bem tolerado.",
        "direcao": (
            "Mild discomfort. A faint, intermittent tension appears between the "
            "eyebrows and around the eyes; the subject shifts position slightly once, "
            "resettles, and occasionally lets one hand drift toward the torso without "
            "guarding it. Gaze wanders briefly and returns. Overall still composed."
        ),
        "respiracao": (
            "Regular nasal breathing at roughly 16 breaths per minute with one slightly "
            "deeper breath, chest rise still easy and symmetrical."
        ),
        "intensidade": "2-3/10",
    },
    "dor_intensa": {
        "rotulo": "Dor intensa",
        "descricao": "Dor significativa com contenção antálgica, sem teatralidade.",
        "direcao": (
            "Significant pain. Sustained brow furrowing and orbital tightening, jaw set "
            "with lips firmly closed, eyes narrowed or briefly squeezed shut. The "
            "subject holds a guarded, slightly flexed posture and keeps very still "
            "because movement hurts, with one hand braced protectively near the trunk "
            "but not pointing to a specific organ. Skin looks slightly pale and damp."
        ),
        "respiracao": (
            "Guarded splinted breathing at roughly 22 breaths per minute, deliberately "
            "shallow chest excursion, an occasional held breath, visible tension in the "
            "neck without frank accessory muscle recruitment."
        ),
        "intensidade": "7-8/10",
    },
    "dispneia": {
        "rotulo": "Dispneia",
        "descricao": "Esforço respiratório evidente, fala não testada (boca fechada).",
        "direcao": (
            "Respiratory distress. The subject holds the head and trunk as upright as "
            "the position allows, shoulders elevated and braced, eyes wide and alert "
            "with a fixed anxious focus. Nostrils flare rhythmically with each "
            "inspiration. The effort is visible and sustained, but the subject stays "
            "oriented and does not change position."
        ),
        "respiracao": (
            "Laboured nasal breathing at roughly 28 breaths per minute, marked and "
            "regular chest and shoulder rise, visible suprasternal and intercostal "
            "effort, rhythmic nasal flaring, short inspiratory time — all conveyed "
            "with the mouth sealed shut."
        ),
        "intensidade": "esforço marcado",
    },
    "rebaixamento": {
        "rotulo": "Rebaixamento do nível de consciência",
        "descricao": "Sonolência/torpor, resposta lentificada, sem convulsão.",
        "direcao": (
            "Depressed level of consciousness. Facial muscles are slack and expression "
            "is absent, head heavy and drifting slowly downward and to one side before "
            "settling. Eyelids droop and close slowly, opening only partially with a "
            "delayed, unfocused gaze that does not track. Body is limp and low-tone, "
            "hands motionless. No seizure activity, no tremor, no agitation."
        ),
        "respiracao": (
            "Slow shallow nasal breathing at roughly 10 breaths per minute, barely "
            "perceptible and slightly irregular chest rise, no accessory muscle use."
        ),
        "intensidade": "sonolento / torporoso",
    },
}

STATE_ORDER: List[str] = [
    "basal",
    "desconforto_leve",
    "dor_intensa",
    "dispneia",
    "rebaixamento",
]


# --------------------------------------------------------------------------- #
# Elenco (4 personas)
# --------------------------------------------------------------------------- #
ACTORS: Dict[str, Dict[str, object]] = {
    "A01": {
        "id": "A01",
        "rotulo": "A01 — Homem 58a, pardo, robusto",
        "descricao": (
            "Homem de 58 anos, pardo, compleição robusta, cabelo grisalho curto. "
            "Cenário de Pronto-Socorro. Vieses clínicos: dor torácica, DPOC, "
            "abdome agudo."
        ),
        "aparencia": (
            "A 58-year-old Brazilian man of mixed (pardo) heritage with warm medium-brown "
            "skin, a sturdy heavy-set build and broad shoulders, short grey-flecked hair, "
            "close-trimmed greying stubble, and deep-set brown eyes with pronounced "
            "nasolabial lines. He wears a plain light-grey hospital gown."
        ),
        "cenario": (
            "Seated upright in an emergency department examination chair against a plain, "
            "softly defocused pale-green clinical wall."
        ),
        "vieses_clinicos": ["dor torácica", "DPOC", "abdome agudo"],
        "cenario_curto": "PS",
    },
    "A02": {
        "id": "A02",
        "rotulo": "A02 — Mulher 34a, cabelo escuro preso",
        "descricao": (
            "Mulher de 34 anos, cabelo escuro preso em coque baixo. Cenário de "
            "Pronto-Socorro. Vieses clínicos: cefaleia, apendicite, queixas "
            "ginecológicas."
        ),
        "aparencia": (
            "A 34-year-old Brazilian woman with light-olive skin, dark brown hair pulled "
            "back into a neat low bun with a few loose strands at the temples, arched dark "
            "eyebrows, brown eyes, an oval face and a slender-to-average build. She wears "
            "a plain light-blue hospital gown and no makeup or jewellery."
        ),
        "cenario": (
            "Seated upright in an emergency department examination chair against a plain, "
            "softly defocused pale-blue clinical wall."
        ),
        "vieses_clinicos": ["cefaleia", "apendicite", "queixas ginecológicas"],
        "cenario_curto": "PS",
    },
    "A03": {
        "id": "A03",
        "rotulo": "A03 — Mulher idosa negra 78a, leito a 30°",
        "descricao": (
            "Mulher negra de 78 anos, em leito hospitalar com cabeceira a 30°. "
            "Cenário de Enfermaria. Vieses clínicos: sepse, insuficiência cardíaca, "
            "delirium."
        ),
        "aparencia": (
            "A 78-year-old Black Brazilian woman with deep brown skin, short tightly "
            "coiled white hair, a thin frail frame, prominent cheekbones and softly lined "
            "features, wearing a pale hospital gown and lying on white bed linen."
        ),
        "cenario": (
            "Lying in a hospital ward bed with the head of the bed raised to about 30 "
            "degrees, head resting on a white pillow, plain softly defocused hospital "
            "room behind her with no visible equipment."
        ),
        "vieses_clinicos": ["sepse", "insuficiência cardíaca", "delirium"],
        "cenario_curto": "Enfermaria",
        # A persona está deitada: a direção postural genérica não se aplica.
        "postura_override": (
            "Because she is lying in bed at 30 degrees, all posture cues are expressed "
            "through the head on the pillow, the neck, the shoulders and the hands on the "
            "blanket rather than through sitting or leaning."
        ),
    },
    "A04": {
        "id": "A04",
        "rotulo": "A04 — Homem jovem 24a, pardo, atlético",
        "descricao": (
            "Homem de 24 anos, pardo, compleição atlética. Cenário de Pronto-Socorro. "
            "Vieses clínicos: asma, trauma leve, intoxicação exógena."
        ),
        "aparencia": (
            "A 24-year-old Brazilian man of mixed (pardo) heritage with light-brown skin, "
            "short dark curly hair, a lean athletic build with defined shoulders, clean "
            "shaven, dark brown eyes and smooth unlined skin. He wears a plain light-grey "
            "hospital gown."
        ),
        "cenario": (
            "Seated upright in an emergency department examination chair against a plain, "
            "softly defocused pale-green clinical wall."
        ),
        "vieses_clinicos": ["asma", "trauma leve", "intoxicação exógena"],
        "cenario_curto": "PS",
    },
}

ACTOR_ORDER: List[str] = ["A01", "A02", "A03", "A04"]


# --------------------------------------------------------------------------- #
# Composição de prompts
# --------------------------------------------------------------------------- #
def build_prompt(actor_id: str, state_id: str) -> str:
    """Monta o prompt completo (uma string) para um par ator × estado."""
    actor = get_actor(actor_id)
    state = get_state(state_id)

    parts: List[str] = [
        f"SUBJECT: {actor['aparencia']}",
        f"SETTING: {actor['cenario']}",
        f"PERFORMANCE — {state['rotulo'].upper()}: {state['direcao']}",
        f"BREATHING: {state['respiracao']}",
    ]
    override = actor.get("postura_override")
    if override:
        parts.append(f"POSTURE NOTE: {override}")
    parts.extend(
        [
            f"CAMERA: {CAMERA_BLOCK}",
            f"CLINICAL CONSTRAINTS: {CLINICAL_CONSTRAINTS}",
            f"LOOP: {LOOP_HINT}",
            "AUDIO: none — this is a silent clip with no dialogue, no ambience and no music.",
        ]
    )
    return "\n".join(parts)


def get_actor(actor_id: str) -> Dict[str, object]:
    try:
        return ACTORS[actor_id]
    except KeyError:
        raise KeyError(
            f"Ator desconhecido: {actor_id!r}. Disponíveis: {', '.join(ACTOR_ORDER)}"
        ) from None


def get_state(state_id: str) -> Dict[str, str]:
    try:
        return STATES[state_id]
    except KeyError:
        raise KeyError(
            f"Estado desconhecido: {state_id!r}. Disponíveis: {', '.join(STATE_ORDER)}"
        ) from None


def iter_matrix(actors: List[str] | None = None, states: List[str] | None = None):
    """Itera a matriz ator × estado na ordem canônica (20 clipes por padrão)."""
    for actor_id in actors or ACTOR_ORDER:
        for state_id in states or STATE_ORDER:
            yield actor_id, state_id


def matrix_size(actors: List[str] | None = None, states: List[str] | None = None) -> int:
    return len(actors or ACTOR_ORDER) * len(states or STATE_ORDER)
