# Talks to a locally-hosted model served by LM Studio's OpenAI-compatible
# local server. Two jobs: (1) chat_with_tools() drives the real, general
# tool-calling conversation the Notion agent runs (see notion_agent.py) --
# the LLM decides for itself which tools to call; a message's `content` can
# be a plain string or a multimodal list (text + image_url parts, same
# shape _chat_multimodal() builds) so an attached image is just part of the
# same tool-calling turn, not a separate call to a different model -- the
# whole point is one model doing vision + tool-calling + text together, not
# a vision model relaying prose to a separate text model. (2)
# estimate_study_minutes() / estimate_project_minutes() / extract_topics()
# are narrow structured calls used by the Study Planner to size up study/
# work time and pull thematic topics out of uploaded material.
#
# SETUP:
#   1. In LM Studio, load the model you want the agent to use
#   2. Go to the "Developer" tab and click "Start Server"
#   3. That's it -- no API key needed, this just talks to the OpenAI-compatible
#      REST API LM Studio exposes locally.
#
# Model ID gotcha: LM Studio requires the "model" field in each request to
# match the *exact* string it reports for the loaded model, not a guessed
# name -- a mismatch 500s. This client fetches that string automatically
# from GET /v1/models rather than hardcoding it, so whatever you have
# loaded in LM Studio is what the agent uses.

import ast
import json
import re
from datetime import date

import requests
from PyQt6.QtCore import QSettings

LM_STUDIO_BASE_URL = "http://127.0.0.1:3812/v1"

SETTINGS_ORG = "StudyWarden"
SETTINGS_APP = "StudyWarden"
SETTINGS_KEY_LM_STUDIO_URL = "connections/lm_studio_url"  # set via the Settings dialog
ENGAGEMENT_JUDGMENT_TIMEOUT_SEC = 30  # deliberately short -- this runs on a repeating cadence during an
                                       # active session (see lockdown_window.py's periodic engagement
                                       # check-in), so a slow response should just be skipped for this
                                       # window (fail open to full credit) rather than block the next one
VOICE_QA_TIMEOUT_SEC = 60             # the student is waiting live, mid-conversation, for a spoken answer --
                                       # text-only/no-images so this should be fast, but still well short of
                                       # REQUEST_TIMEOUT_SEC's allowance for slow vision/thinking calls
REQUEST_TIMEOUT_SEC = 900  # local CPU inference can take a while, especially with images attached -- a
                            # "thinking"/reasoning-heavy model working through several slides one at a time
                            # before answering can easily need several minutes of SLOW token-by-token
                            # decoding (observed in practice: ~6 tokens/sec on modest hardware, 1400+
                            # reasoning tokens and still not done) on top of the image prompt-processing
                            # time -- 240s was cutting genuinely-still-working requests off mid-thought,
                            # not just catching truly stuck ones

STUDY_ESTIMATE_SYSTEM_PROMPT = """You estimate how many total minutes of focused study a student needs \
to prepare for an exam, based on study material they've provided and how many chapters the exam covers.

Output ONLY a single integer -- the total number of minutes, nothing else. No explanation, no units, no \
punctuation, no commas in the number.

Guidelines:
- Base your estimate on the density/complexity of the material and the number of chapters, not just how \
much text there is.
- A chapter of ordinary difficulty usually needs somewhere around 60-120 minutes of focused review; more \
for dense/technical/math-heavy material, less for light conceptual review.
- If no material was provided, or it's too sparse to judge complexity from, default to about 90 minutes \
per chapter.
- Some material is provided as images (slide/page renders or screenshots) instead of or in addition to \
text -- actually look at what's in them (diagrams, charts, equations, density of content per slide) and \
factor that into your estimate the same way you would for text."""


PROJECT_ESTIMATE_SYSTEM_PROMPT = """You estimate how many total minutes of focused work a student needs \
to complete a project or assignment, based on a description they've given and/or material (instructions, \
rubric, requirements doc) they've provided. Unlike exam studying, this is about doing/building/writing \
something, not reviewing chapters.

Output ONLY a single integer -- the total number of minutes, nothing else. No explanation, no units, no \
punctuation, no commas in the number.

Guidelines:
- Base your estimate on the scope and complexity implied by the description/material, not just how much \
text there is.
- A small, single-part deliverable (a short report, a simple script, a routine homework assignment) \
usually needs somewhere around 1-4 hours (60-240 minutes) total; a substantial multi-part project \
(research + implementation + writing + revision) can need 10-20+ hours (600-1200+ minutes).
- If both the description and material are sparse, default to about 180 minutes (3 hours) as a reasonable \
middle estimate for a typical assignment/project.
- Some material is provided as images (rubric/instructions page renders or screenshots) instead of or in \
addition to text -- actually look at what's in them and factor that into your estimate."""


REVISION_ESTIMATE_SYSTEM_PROMPT = """You estimate how many total minutes a student needs to FULLY REVISE \
material they've ALREADY been taught in class (not learning it for the first time), based on the slides \
provided and how many of them were actually covered.

Output ONLY a single integer -- the total number of minutes, nothing else. No explanation, no units, no \
punctuation, no commas in the number.

Guidelines:
- This is REVISION, not first-time learning -- the student has already seen this material in class, so \
estimate for THOROUGH REVIEW/CONSOLIDATION (re-reading, working through examples, making sure everything \
is solid), not the slower pace of learning something brand new. Revision is typically noticeably faster \
per unit of material than first-time study.
- Base your estimate on the density/complexity of the covered slides, not just how many there are -- a \
slide with a few bullet points needs less time than one packed with worked examples, diagrams, or dense \
technical content.
- A typical lecture slide usually needs somewhere around 3-8 minutes of focused revision; more for dense/ \
technical/math-heavy slides, less for light/overview slides.
- Some material is provided as images (slide renders) instead of or in addition to text -- actually look \
at what's in them (diagrams, charts, equations, density of content per slide) and factor that into your \
estimate the same way you would for text."""


ENGAGEMENT_JUDGMENT_SYSTEM_PROMPT = """You judge a student's engagement during a recent short window of a \
monitored study session, based ONLY on distraction-monitor data for that window -- how many times they got \
flagged as distracted (phone use, looking away), how long those distractions lasted, and which slides were \
in view. You are NOT judging the material itself, and you have not seen it.

Output ONLY a JSON object with exactly these two keys, nothing else -- no explanation, no markdown fences:
  "credit": one of "full", "reduced", "none" -- how much of this window's time should count toward study progress.
  "revise_slide": either null, or the exact slide number (must be one of the numbers in "slides_viewed") \
that seems worth a second look.

Guidelines:
- "full": no distractions, or one brief one -- normal, expected studying.
- "reduced": a couple of real distractions, or a meaningful chunk of the window spent distracted -- still \
studying, but not fully focused.
- "none": distracted for most/all of the window, or several repeated distractions -- this window shouldn't \
count as real progress.
- revise_slide: only suggest one if the SAME slide(s) kept coming up around distraction events, suggesting \
the material there didn't actually get absorbed -- null otherwise. Never invent a slide number that isn't \
in "slides_viewed"."""


SESSION_REVIEW_SYSTEM_PROMPT = """You help a student review right after finishing a study session, based \
on the material they just studied.

Output ONLY a JSON object with exactly these two keys, nothing else -- no explanation, no markdown fences:
  "recap": a short (2-4 sentence) summary of the main things this material covered.
  "quiz": a JSON array of exactly 3 objects, each with "question" and "answer" -- short-answer questions \
that test whether the key ideas actually stuck, with a brief correct answer for each.

Guidelines:
- Base both the recap and the quiz on what's ACTUALLY in the material -- don't invent things it doesn't cover.
- Keep the quiz questions genuinely testing understanding of a KEY idea, not trivial detail-recall (e.g. \
not "what's on slide 3") and not requiring information from outside the material.
- Some material is provided as images (slide renders) instead of or in addition to text -- actually look \
at what's in them too, the same way you would for text."""


TOPIC_EXTRACTION_SYSTEM_PROMPT = """You read study material (slides, notes, or a chapter) and extract \
its main thematic topics -- broad themes/concepts the material covers, not literal slide titles or \
section headers copied verbatim.

Output a JSON array of 3 to 5 items (fewer only if the material is genuinely too short/sparse to \
distinguish that many distinct themes). Each item must have exactly these fields:
  "topic": a short (2-6 word) name for the theme.
  "synopsis": 1-2 sentences summarizing what the material actually says about it.

Output ONLY the JSON array -- no explanation, no markdown formatting, no code fences.

Example output:
[{{"topic": "Relational Model Basics", "synopsis": "Introduces tables, rows, and columns as the core \
structure of relational databases, contrasting them with older network/hierarchical models."}}]"""


VOICE_QA_SYSTEM_PROMPT = """You are a study assistant answering a spoken question during a focused study \
session. The student is looking at one specific slide/page right now (given below) and asked a question \
about it out loud.

Answer conversationally and CONCISELY -- 2-4 sentences, like a quick spoken explanation, not a written essay \
-- since your answer will be read aloud by text-to-speech. Do not use markdown, bullet points, headers, or \
any formatting; plain spoken sentences only. If the slide text doesn't obviously relate to the question, \
still do your best to answer helpfully using your own knowledge, but briefly mention it's not directly from \
the slide. You can swear casually and naturally where it fits, like a blunt, casual friend explaining this \
-- don't force it in, just don't hold back if it comes up.

Output ONLY the answer itself -- no preamble like "Sure," or "Here's the answer,", no quotation marks."""


class LLMClientError(Exception):
    pass


def _explain_request_error(e):
    """Pulls LM Studio's actual rejection reason out of a failed request,
    instead of just requests' generic "400 Client Error: Bad Request for
    url: ..." -- LM Studio (llama.cpp's server) usually puts a specific,
    actionable reason in the response body (e.g. the loaded model doesn't
    support images, or doesn't support tool calling, or a schema mismatch)
    that would otherwise be silently lost, leaving a 400 with no way to
    tell what actually went wrong."""
    response = getattr(e, "response", None)
    if response is None:
        return str(e)
    try:
        payload = response.json()
    except ValueError:
        return f"{e} -- {response.text[:300]}" if response.text else str(e)
    detail = payload.get("error", payload)
    if isinstance(detail, dict):
        detail = detail.get("message") or detail
    return f"{e} -- {detail}" if detail else str(e)


def _strip_reasoning(raw):
    """Strips a <think>...</think> (or <thinking>...</thinking>) block that
    local "reasoning" models (DeepSeek-R1 distills, QwQ, Qwen3 with
    thinking enabled, etc.) emit before their actual answer, despite being
    told to output ONLY a number. Without this, re.search(r"\\d+", ...)
    grabs the FIRST number mentioned anywhere in that reasoning trace --
    e.g. a guideline number the model echoes while "thinking out loud"
    ("a chapter usually needs 60-120 minutes...") -- instead of the
    model's actual final answer. A no-op for models that don't emit one."""
    return re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", raw, flags=re.DOTALL | re.IGNORECASE).strip()


def _is_context_overflow_error(e):
    """True if a failed request failed specifically because the
    conversation exceeded the model's context window (LM Studio/llama.cpp
    report this as "type": "exceed_context_size_error", e.g. "request
    (9706 tokens) exceeds the available context size (8192 tokens)") --
    as opposed to some other 400 (a bad tool schema, an unsupported image),
    which trimming history and retrying wouldn't fix and could mask."""
    response = getattr(e, "response", None)
    if response is None:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    error = payload.get("error", {})
    if isinstance(error, dict):
        if error.get("type") == "exceed_context_size_error":
            return True
        message = str(error.get("message", ""))
    else:
        message = str(error)
    message = message.lower()
    return "context size" in message or "context length" in message


def _split_into_turns(messages):
    """Splits a messages list (with or without a leading system message)
    into (system_message_or_None, [turn1_messages, turn2_messages, ...]) --
    each turn starts at a "user" message and includes every message up to
    (not including) the next one. Used for trimming history by whole turns
    rather than individual messages: a tool-calling conversation has a
    structural constraint (a "tool" role message must immediately follow
    the "assistant" message whose tool_call it's answering) that dropping
    messages piecemeal would break."""
    system = None
    body = messages
    if messages and messages[0].get("role") == "system":
        system = messages[0]
        body = messages[1:]

    turns = []
    current = []
    for message in body:
        if message.get("role") == "user" and current:
            turns.append(current)
            current = []
        current.append(message)
    if current:
        turns.append(current)
    return system, turns


def _drop_oldest_turn(messages):
    """Drops the single oldest complete turn (see _split_into_turns) to
    make room when the model's context window is too small for the whole
    conversation -- always keeps the system message and at least the most
    recent turn, even if that leaves nothing else to drop."""
    system, turns = _split_into_turns(messages)
    if len(turns) <= 1:
        return messages  # nothing safe left to drop
    result = [system] if system else []
    for turn in turns[1:]:
        result.extend(turn)
    return result


MAX_CONTEXT_TRIM_RETRIES = 6  # how many oldest turns we'll drop before giving up on one round
MAX_IMAGE_TRIM_RETRIES = 4    # how many times we'll halve the image count before giving up on a multimodal attempt entirely


class LLMClient:
    def __init__(self, base_url=None):
        if base_url is None:
            settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
            base_url = settings.value(SETTINGS_KEY_LM_STUDIO_URL) or LM_STUDIO_BASE_URL
        self.base_url = base_url

    def get_model_id(self):
        """Fetch the exact model ID string LM Studio wants in requests --
        whichever model it lists first. Deliberately NOT cached: which
        model is actually loaded in LM Studio can change at any moment (the
        user ejects/loads a different one) without this app restarting, and
        a stale cached ID would keep being used indefinitely. The extra
        GET /v1/models per call is cheap (a local request, no inference)
        next to how long an actual chat completion takes."""
        try:
            resp = requests.get(f"{self.base_url}/models", timeout=10)
            resp.raise_for_status()
            models = resp.json().get("data", [])
        except requests.RequestException as e:
            raise LLMClientError(
                "Couldn't reach LM Studio's local server. Make sure LM Studio "
                "is open, a model is loaded, and you've clicked 'Start Server' "
                f"in the Developer tab. ({_explain_request_error(e)})"
            ) from e
        if not models:
            raise LLMClientError("LM Studio's server is running but no model is loaded.")
        return models[0]["id"]

    def check_connection(self):
        """Best-effort health check for Settings' 'Test Connection' button
        -- unlike get_model_id(), this NEVER raises: always returns (ok,
        message) so a bad connection shows a clear status instead of an
        unhandled exception. Meant to be checked BEFORE a lockdown session
        starts (server down, no model loaded, wrong URL), not discovered
        mid-session when it's much more disruptive."""
        try:
            model_id = self.get_model_id()
            return True, f"Connected -- model loaded: {model_id}"
        except LLMClientError as e:
            return False, str(e)

    def _chat(self, system_prompt, user_prompt, temperature=0.1, timeout=None):
        model_id = self.get_model_id()
        payload = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
        }
        try:
            resp = requests.post(f"{self.base_url}/chat/completions", json=payload, timeout=timeout or REQUEST_TIMEOUT_SEC)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise LLMClientError(f"LM Studio request failed: {_explain_request_error(e)}") from e
        return resp.json()["choices"][0]["message"]["content"]

    def _chat_multimodal(self, system_prompt, text_prompt, images_b64, temperature=0.1):
        """Like _chat(), but attaches images (base64-encoded PNG strings) to
        the user turn using the OpenAI vision content format -- only works
        if the model currently loaded in LM Studio is actually multimodal;
        a text-only model will typically either ignore the images or error
        out on the request.

        If the request overflows the model's context window (a real, common
        failure with several slide renders and a modest local context
        length -- e.g. a dozen images can easily need more than an
        8192-token window, observed in practice) OR it simply times out
        (observed in practice too: vision models often need a minimum
        number of image tokens PER image just to encode them at all --
        e.g. Qwen-VL wants >=1024 -- so a dozen full images can take far
        longer to prompt-process than REQUEST_TIMEOUT_SEC even once they'd
        technically fit in context), the image count is progressively
        HALVED and retried rather than giving up on every image at once --
        salvaging as many images as actually fit (and finish in time) is
        far better than the caller's all-or-nothing fallback to zero
        images (see estimate_*_minutes(), which falls back to a pure-text
        _chat() call only once this raises). Still raises LLMClientError
        if even a single image doesn't fit/finish in time, or the model
        rejects images outright for a different reason (not
        vision-capable, etc.)."""
        model_id = self.get_model_id()
        images = list(images_b64)
        last_error = None
        for _ in range(MAX_IMAGE_TRIM_RETRIES + 1):
            content = [{"type": "text", "text": text_prompt}]
            for b64 in images:
                content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
            payload = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": content},
                ],
                "temperature": temperature,
            }
            try:
                resp = requests.post(f"{self.base_url}/chat/completions", json=payload, timeout=REQUEST_TIMEOUT_SEC)
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
            except requests.RequestException as e:
                last_error = e
                is_timeout = isinstance(e, requests.exceptions.Timeout)
                if not (images and (_is_context_overflow_error(e) or is_timeout)):
                    raise LLMClientError(
                        f"LM Studio request failed (does the loaded model support images?): {_explain_request_error(e)}"
                    ) from e
                images = images[:len(images) // 2]

        raise LLMClientError(
            f"LM Studio request failed (does the loaded model support images?): {_explain_request_error(last_error)}"
        ) from last_error

    def estimate_study_minutes(self, material_text, chapter_count, images_b64=None):
        """Returns an int: total minutes of focused study estimated needed,
        given material text (may be empty/None), how many chapters the exam
        covers, and optionally a list of base64-PNG images (slide/page
        renders or screenshots) to actually look at rather than rely on
        OCR/extracted text alone. If images are given but the loaded model
        doesn't support vision, falls back to a text-only estimate rather
        than failing outright (as long as there's some text to go on).
        Raises LLMClientError if the model never returns a usable number."""
        material_text = (material_text or "").strip()
        prompt = (
            f"Chapters covered: {chapter_count}\n\n"
            f"Material:\n{material_text[:6000] if material_text else '(no material text extracted -- see attached images)'}"
        )

        if images_b64:
            try:
                raw = self._chat_multimodal(STUDY_ESTIMATE_SYSTEM_PROMPT, prompt, images_b64, temperature=0.2)
            except LLMClientError:
                if not material_text:
                    raise
                raw = self._chat(STUDY_ESTIMATE_SYSTEM_PROMPT, prompt, temperature=0.2)
        else:
            raw = self._chat(STUDY_ESTIMATE_SYSTEM_PROMPT, prompt, temperature=0.2)

        print(
            f"[LLMClient.estimate_study_minutes] chapter_count={chapter_count} "
            f"material_chars={len(material_text)} images={len(images_b64 or [])}\n"
            f"--- raw model response ---\n{raw}\n--- end raw response ---"
        )
        match = re.search(r"\d+", _strip_reasoning(raw).replace(",", ""))
        if not match:
            raise LLMClientError(f"Model didn't return a number for the study-time estimate: {raw!r}")
        return int(match.group(0))

    def estimate_project_minutes(self, description, material_text, images_b64=None):
        """Like estimate_study_minutes(), but for a project deliverable
        instead of exam review -- no chapter count involved. description:
        a free-text summary of what the project needs (may be empty/None if
        material alone is provided). Raises LLMClientError if the model
        never returns a usable number."""
        description = (description or "").strip()
        material_text = (material_text or "").strip()
        prompt = (
            f"Project description: {description or '(none given)'}\n\n"
            f"Material:\n{material_text[:6000] if material_text else '(no material text extracted -- see attached images, if any)'}"
        )

        if images_b64:
            try:
                raw = self._chat_multimodal(PROJECT_ESTIMATE_SYSTEM_PROMPT, prompt, images_b64, temperature=0.2)
            except LLMClientError:
                if not description and not material_text:
                    raise
                raw = self._chat(PROJECT_ESTIMATE_SYSTEM_PROMPT, prompt, temperature=0.2)
        else:
            raw = self._chat(PROJECT_ESTIMATE_SYSTEM_PROMPT, prompt, temperature=0.2)

        print(
            f"[LLMClient.estimate_project_minutes] description_chars={len(description)} "
            f"material_chars={len(material_text)} images={len(images_b64 or [])}\n"
            f"--- raw model response ---\n{raw}\n--- end raw response ---"
        )
        match = re.search(r"\d+", _strip_reasoning(raw).replace(",", ""))
        if not match:
            raise LLMClientError(f"Model didn't return a number for the project-time estimate: {raw!r}")
        return int(match.group(0))

    def estimate_revision_minutes(self, material_text, slide_count, images_b64=None):
        """Returns an int: total minutes estimated needed to FULLY REVISE
        (not learn for the first time) the given material -- slide_count is
        how many slides were actually covered in class (the material given
        should already be scoped to just that range -- see
        syllabus_parser.extract_text_and_images()'s page_limit param).
        Raises LLMClientError if the model never returns a usable number."""
        material_text = (material_text or "").strip()
        prompt = (
            f"Slides covered: {slide_count}\n\n"
            f"Material:\n{material_text[:6000] if material_text else '(no material text extracted -- see attached images)'}"
        )

        if images_b64:
            try:
                raw = self._chat_multimodal(REVISION_ESTIMATE_SYSTEM_PROMPT, prompt, images_b64, temperature=0.2)
            except LLMClientError:
                if not material_text:
                    raise
                raw = self._chat(REVISION_ESTIMATE_SYSTEM_PROMPT, prompt, temperature=0.2)
        else:
            raw = self._chat(REVISION_ESTIMATE_SYSTEM_PROMPT, prompt, temperature=0.2)

        print(
            f"[LLMClient.estimate_revision_minutes] slide_count={slide_count} "
            f"material_chars={len(material_text)} images={len(images_b64 or [])}\n"
            f"--- raw model response ---\n{raw}\n--- end raw response ---"
        )
        match = re.search(r"\d+", _strip_reasoning(raw).replace(",", ""))
        if not match:
            raise LLMClientError(f"Model didn't return a number for the revision-time estimate: {raw!r}")
        return int(match.group(0))

    def estimate_engagement_credit(self, window_seconds, distraction_events, distracted_seconds, slides_viewed, pace_status):
        """Judges how much of a recent short study window should count as
        real progress, based purely on distraction-monitor data for that
        window (see lockdown_window.py's periodic engagement check-in) --
        NOT the material content, which this never sees. Returns (credit,
        revise_slide): credit is "full"/"reduced"/"none", revise_slide is
        None or an int drawn from slides_viewed.

        Text-only and deliberately fast (ENGAGEMENT_JUDGMENT_TIMEOUT_SEC,
        not the much larger REQUEST_TIMEOUT_SEC meant for slow vision/
        thinking calls) since this runs on a tight repeating cadence
        during an active session. Never raises: a request failure OR a
        malformed response both fall back to ("full", None) -- punishing
        study progress over an infrastructure hiccup or a formatting slip
        would be worse than just skipping this window's judgment."""
        prompt = (
            f"Window length: {window_seconds}s\n"
            f"Distraction events: {distraction_events}\n"
            f"Seconds spent distracted: {distracted_seconds}\n"
            f"Slides viewed this window: {slides_viewed}\n"
            f"Current pace status: {pace_status}"
        )
        try:
            raw = self._chat(
                ENGAGEMENT_JUDGMENT_SYSTEM_PROMPT, prompt, temperature=0.1, timeout=ENGAGEMENT_JUDGMENT_TIMEOUT_SEC
            )
            parsed = _loads_lenient(_extract_json_object_text(_strip_reasoning(raw)))
            credit = parsed.get("credit")
            if credit not in ("full", "reduced", "none"):
                credit = "full"
            revise_slide = parsed.get("revise_slide")
            if revise_slide not in slides_viewed:
                revise_slide = None
            return credit, revise_slide
        except Exception as e:
            print(f"[LLMClient.estimate_engagement_credit] skipped this window (fail open to full credit): {e}")
            return "full", None

    def generate_session_review(self, material_text, images_b64=None):
        """Returns {"recap": str, "quiz": [{"question", "answer"}, ...]}
        for a just-finished study session -- shown before Monitor
        Lockdown actually releases once the material hits 100% completion
        (see LockdownWindow), a quick reinforcement pass over what was
        just covered rather than just letting the session end silently.
        Unlike the softer fallback-tolerant estimate methods, a genuinely
        broken review here RAISES (LLMClientError) rather than returning
        something empty/wrong -- the caller decides how to handle that
        (e.g. skip straight to exit instead of showing a broken dialog)."""
        material_text = (material_text or "").strip()
        prompt = f"Material:\n{material_text[:8000] if material_text else '(no material text extracted -- see attached images)'}"

        if images_b64:
            try:
                raw = self._chat_multimodal(SESSION_REVIEW_SYSTEM_PROMPT, prompt, images_b64, temperature=0.3)
            except LLMClientError:
                if not material_text:
                    raise
                raw = self._chat(SESSION_REVIEW_SYSTEM_PROMPT, prompt, temperature=0.3)
        else:
            raw = self._chat(SESSION_REVIEW_SYSTEM_PROMPT, prompt, temperature=0.3)

        try:
            parsed = _loads_lenient(_extract_json_object_text(_strip_reasoning(raw)))
            if not isinstance(parsed, dict):
                raise LLMClientError(f"Model didn't return a JSON object for the session review: {raw!r}")
            recap = str(parsed.get("recap") or "").strip()
            quiz_raw = parsed.get("quiz") or []
            quiz = [
                {"question": str(item.get("question", "")).strip(), "answer": str(item.get("answer", "")).strip()}
                for item in quiz_raw if isinstance(item, dict) and item.get("question")
            ]
        except json.JSONDecodeError as e:
            raise LLMClientError(f"Model didn't return valid JSON for the session review: {e}") from e

        if not recap and not quiz:
            raise LLMClientError(f"Model didn't return a usable session review: {raw!r}")
        return {"recap": recap, "quiz": quiz}

    def answer_slide_question(self, slide_text, question):
        """Answers a spoken question asked during Monitor Lockdown's
        push-to-talk voice assistant (see ui/voice_assistant_panel.py),
        using whichever slide is CURRENTLY SCROLLED INTO VIEW as context
        -- not a full embedding search across the whole deck, since the
        page the student is actually looking at right now is a simpler
        and more reliable signal than a keyword/embedding guess would be
        (same reasoning behind push-to-talk itself over fully-automatic
        ambient question detection: prefer the more literal, more
        reliable answer over a guess). Text-only, no images -- slide
        text alone is plenty for a quick spoken Q&A. Raises
        LLMClientError on a request failure or a genuinely empty
        response; the caller surfaces that as a status-label message
        rather than trying to speak nothing."""
        slide_text = (slide_text or "").strip()
        context = slide_text[:4000] if slide_text else "(no slide text available -- answer from general knowledge)"
        prompt = f"Current slide text:\n{context}\n\nStudent's question: {question}"
        raw = self._chat(VOICE_QA_SYSTEM_PROMPT, prompt, temperature=0.3, timeout=VOICE_QA_TIMEOUT_SEC)
        answer = _strip_reasoning(raw).strip()
        if not answer:
            raise LLMClientError(f"Model returned an empty answer: {raw!r}")
        return answer

    def extract_topics(self, material_text, images_b64=None):
        """Returns [{"topic", "synopsis"}, ...] -- 3-5 thematic topics
        covered in the given material (text and/or images), each with a
        1-2 sentence synopsis. Used for the Monitor Lockdown "Preview
        State" (see ui/topic_preview.py) so you see what today's material
        actually covers before locking in, not just a raw page dump.
        Raises LLMClientError if the model never returns usable topics --
        does not retry on bad JSON (a one-off preview isn't worth several
        extra round trips on a slow local model)."""
        material_text = (material_text or "").strip()
        prompt = f"Material:\n{material_text[:8000] if material_text else '(no text extracted -- see attached images)'}"

        if images_b64:
            try:
                raw = self._chat_multimodal(TOPIC_EXTRACTION_SYSTEM_PROMPT, prompt, images_b64, temperature=0.3)
            except LLMClientError:
                if not material_text:
                    raise
                raw = self._chat(TOPIC_EXTRACTION_SYSTEM_PROMPT, prompt, temperature=0.3)
        else:
            raw = self._chat(TOPIC_EXTRACTION_SYSTEM_PROMPT, prompt, temperature=0.3)

        return _parse_topics_array(raw)

    def extract_form_fields(self, field_names, images_b64, this_year=None, context=None):
        """Looks at image(s) (e.g. a syllabus grading/schedule table
        screenshot) and extracts a value for each of the given field names,
        returning {field_name: value} with every requested key always
        present (empty string "" for anything not found or not visible) --
        for pre-filling a review-before-submit form, not a free chat reply.
        context: optional extra instructions specific to the caller's
        domain (e.g. how syllabus terminology like "Midterm"/"Major Exam I"
        maps to this app's exact field names) -- appended to the base
        prompt, which stays generic on purpose so this method isn't tied to
        any one form's vocabulary. Raises LLMClientError only for a genuine
        request failure (can't reach LM Studio, model rejected the
        request); a malformed/unparseable response is handled leniently by
        _parse_fields_object() instead of raising, since the point is a
        best-effort head start the user reviews and edits anyway, not
        something that should block the form over a formatting slip."""
        this_year = this_year or date.today().year
        fields_list = ", ".join(field_names)
        system_prompt = (
            "You read image(s) (e.g. a syllabus grading/schedule table) and extract a value for each "
            f"of these specific fields, if visible: {fields_list}.\n\n"
            f"Output ONLY a JSON object with exactly these keys, nothing else: {fields_list}.\n"
            f"For a date field, write YYYY-MM-DD if a specific date is given (assume {this_year} if no "
            "year is given), \"TBA\" if it's explicitly marked as not yet announced or given as a vague "
            "institution-level phrase (e.g. \"at the end of the semester\", \"in accordance with "
            "university policy\"), or \"\" (empty string) if that field isn't mentioned anywhere in the "
            "image at all. If a field gives several possible dates, use the earliest. If a week number "
            "is given (e.g. \"Week 5\") ALONGSIDE an explicit date for the same item, use the explicit "
            "date and ignore the week number. If ONLY a week number is given, with no explicit date "
            "anywhere for that item, output it as exactly \"Week N\" (e.g. \"Week 5\", nothing else in "
            "the string) -- do not guess an actual calendar date yourself, that gets resolved "
            "separately from a known start date. For a name/text field (e.g. a course name), write it "
            "as shown, or \"\" if not visible. Never guess or invent a value that isn't actually shown."
        )
        if context:
            system_prompt += "\n\n" + context
        raw = self._chat_multimodal(system_prompt, "Extract the fields.", images_b64, temperature=0.1)
        return _parse_fields_object(raw, field_names)

    def chat_with_tools(self, messages, tools, dispatch_tool_call, max_rounds=15, on_progress=None, on_tool_result=None):
        """Runs one full tool-calling conversation turn. Sends messages+tools
        to the model; whenever it responds with tool_calls, executes each via
        dispatch_tool_call(name, arguments_dict) -> a JSON-serializable
        result, appends the results as "tool" messages, and asks again -- up
        to max_rounds round trips, so a confused model can't loop forever.
        Each round can contain several tool_calls at once (most models can
        return more than one in a single response) -- max_rounds counts
        round TRIPS, not individual tool calls, so a model that batches well
        gets much more done per round than one that calls tools one at a
        time; CHAT_SYSTEM_PROMPT nudges toward batching for exactly this
        reason. 15 leaves real headroom for a genuinely multi-field task
        (e.g. filling in 7+ exam table cells from one image) even if the
        model doesn't batch at all.
        A message's "content" can be a plain string or a multimodal list (see
        module docstring) -- this forwards messages to the API as-is, so an
        image attached to the user turn just rides along with the rest of
        the conversation. on_progress: optional callback(phase_text) --
        called right before each network request ("Thinking...") and right
        before each tool call is executed ("Calling <name>..."); the
        resolved model ID is always included (e.g. "Thinking...
        [qwen2.5-vl-7b-instruct]") since LM Studio's own prompt-processing
        percentage isn't exposed over the API at all, and seeing which model
        is actually answering is the single most useful diagnostic when a
        model just talks instead of calling tools. on_tool_result: optional
        callback(name, arguments, result) called right after each tool call
        actually returns -- lets the caller surface a failure (result
        contains "error") directly, rather than trusting the model to notice
        and relay it in its eventual reply, which it won't always do
        reliably. If the model's context window can't fit the whole
        conversation (observed real failure: "request (9706 tokens) exceeds
        the available context size (8192 tokens)"), the oldest turn is
        dropped and the request retried, up to MAX_CONTEXT_TRIM_RETRIES
        times, before giving up -- a long-running chat self-heals by losing
        its oldest history instead of hard-failing every message from then
        on. Returns (final_reply_text, updated_messages) -- updated_messages
        is the full (possibly trimmed) messages list, meant to be passed
        back in as `messages` on the next call for real multi-turn memory."""
        model_id = self.get_model_id()
        working_messages = list(messages)

        for _ in range(max_rounds):
            if on_progress:
                on_progress(f"Thinking... [{model_id}]")

            # If the conversation has grown too large for the model's
            # context window (observed real failure: LM Studio rejects the
            # request outright with "exceeds the available context size"),
            # drop the single oldest turn and retry, rather than failing
            # the whole exchange over history that's no longer even
            # relevant -- up to MAX_CONTEXT_TRIM_RETRIES times. Any other
            # kind of request failure still raises immediately.
            for trim_attempt in range(MAX_CONTEXT_TRIM_RETRIES + 1):
                payload = {
                    "model": model_id,
                    "messages": working_messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "temperature": 0.2,
                }
                try:
                    resp = requests.post(f"{self.base_url}/chat/completions", json=payload, timeout=REQUEST_TIMEOUT_SEC)
                    resp.raise_for_status()
                    break
                except requests.RequestException as e:
                    if not _is_context_overflow_error(e):
                        raise LLMClientError(f"LM Studio request failed: {_explain_request_error(e)}") from e
                    trimmed = _drop_oldest_turn(working_messages)
                    if trimmed is working_messages or trim_attempt == MAX_CONTEXT_TRIM_RETRIES:
                        raise LLMClientError(
                            "LM Studio request failed: the conversation is too large for the model's "
                            f"context window, even after trimming older messages ({_explain_request_error(e)}). "
                            "Increase the model's context length in LM Studio (set when loading the "
                            "model, or in its settings) if this keeps happening."
                        ) from e
                    working_messages = trimmed
                    if on_progress:
                        on_progress(
                            f"Conversation too long for the model's context -- trimming older "
                            f"messages and retrying... [{model_id}]"
                        )

            message = resp.json()["choices"][0]["message"]
            working_messages.append(message)

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                return message.get("content") or "", working_messages

            for call in tool_calls:
                function = call.get("function", {})
                name = function.get("name", "")
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                if on_progress:
                    on_progress(f"Calling {name}... [{model_id}]")
                result = dispatch_tool_call(name, arguments)
                if on_tool_result:
                    on_tool_result(name, arguments, result)
                working_messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": json.dumps(result),
                })

        return (
            "That took more steps than I could finish in one go -- try breaking it into smaller messages.",
            working_messages,
        )


def _extract_json_array_text(raw):
    """Small models often wrap JSON in ```json fences or add a sentence
    before/after it despite instructions -- pulls just the array substring
    out robustly rather than requiring an exact match. Returns text meant
    for _loads_lenient(), not the parsed value."""
    raw = raw.strip()
    fence_match = re.search(r"```(?:json)?\s*(\[.*\])\s*```", raw, re.DOTALL)
    if fence_match:
        return fence_match.group(1)
    bracket_match = re.search(r"\[.*\]", raw, re.DOTALL)
    if bracket_match:
        return bracket_match.group(0)
    return raw


def _extract_json_object_text(raw):
    """Same idea as _extract_json_array_text(), for a JSON OBJECT ({...})
    instead of an array."""
    raw = raw.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
    if fence_match:
        return fence_match.group(1)
    brace_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if brace_match:
        return brace_match.group(0)
    return raw


def _loads_lenient(text):
    """Tries strict JSON first; if a model mixed in Python-style single
    quotes and/or a trailing comma (an observed real failure mode -- some
    local models drift toward "Python literal" instead of strict JSON,
    especially mid-array), falls back to ast.literal_eval(), which
    tolerates both. Re-raises the original json error if neither works."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as json_error:
        try:
            return ast.literal_eval(text)
        except (ValueError, SyntaxError):
            raise json_error


def _parse_topics_array(raw):
    """Parses extract_topics()'s expected schema: a JSON array of
    {"topic", "synopsis"} objects. Raises LLMClientError (not retried --
    a one-off preview isn't worth extra round trips on a slow local model)
    if the model never returns anything usable."""
    try:
        parsed = _loads_lenient(_extract_json_array_text(raw))
    except json.JSONDecodeError as e:
        raise LLMClientError(f"Model didn't return valid JSON for the topic preview: {e}") from e
    if not isinstance(parsed, list):
        raise LLMClientError("Model didn't return a JSON array for the topic preview.")
    cleaned = []
    for item in parsed:
        if not isinstance(item, dict) or "topic" not in item or "synopsis" not in item:
            continue
        cleaned.append({"topic": str(item["topic"]).strip(), "synopsis": str(item["synopsis"]).strip()})
    if not cleaned:
        raise LLMClientError("Model's topic list didn't match the expected format.")
    return cleaned


def _parse_fields_object(raw, field_names):
    """Parses extract_form_fields()'s expected schema: a JSON object mapping
    each requested field name to a value. Deliberately lenient -- unlike
    _parse_topics_array(), this never raises on a parse failure or a
    missing/extra key; it just falls back to an empty string for anything
    it can't make sense of, since the result pre-fills a form the user
    reviews and edits anyway, not something a formatting slip should block."""
    try:
        parsed = _loads_lenient(_extract_json_object_text(raw))
    except (json.JSONDecodeError, ValueError, SyntaxError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return {name: str(parsed.get(name) or "").strip() for name in field_names}


