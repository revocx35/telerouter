"""Text cleanup for TTS, endpointing on real speech, audio helpers."""

import wave

from telerouter.media import pcm
from telerouter.voice.assistant import sentences, speakable
from telerouter.voice.speech import clean_transcript
from telerouter.voice.vad import EndpointConfig, Endpointer

from .conftest import FIXTURES


def test_speakable_strips_markdown_code_and_links():
    t = speakable("## Result\n**Paris** is the capital. See [docs](https://x.y) or https://a.b/c\n"
                  "```python\nprint(1)\n```\n- one\n- two 🎉")
    assert "**" not in t and "#" not in t and "https" not in t and "print" not in t and "🎉" not in t
    assert "Paris is the capital" in t and "docs" in t and "a link" in t and "(code omitted)" in t
    long = speakable("This is a sentence. " * 100, limit=200)
    assert len(long) < 260 and long.endswith("That's the short version.")


def test_sentences_groups_short_ones():
    parts = sentences("Yes. I called Dad. He will call back in ten minutes. Anything else?")
    assert parts[0].startswith("Yes. I called Dad.") and len(parts) >= 2


def test_whisper_hallucinations_are_dropped():
    assert clean_transcript(" Thank you. ") == ""
    assert clean_transcript("[BLANK_AUDIO]") == ""
    assert clean_transcript("Call my mom. [music]") == "Call my mom."


def read(path):
    with wave.open(str(path)) as w:
        assert w.getframerate() == 16000
        return w.readframes(w.getnframes())


def test_endpointer_finds_the_utterance_and_its_end():
    speech = read(FIXTURES / "speech.wav")
    ep = Endpointer(EndpointConfig(start_timeout_ms=3000, end_silence_ms=700, max_ms=10000))
    result = None
    for i in range(0, len(speech) + 64000, 640):
        chunk = speech[i:i + 640] or bytes(640)
        result = ep.feed(chunk)
        if result:
            break
    assert result == "speech" and ep.started
    dur = len(ep.audio) / 32000
    assert 1.0 < dur < 3.5            # the phrase plus pre-roll and the closing pause, not the leading silence


def test_endpointer_times_out_on_silence():
    ep = Endpointer(EndpointConfig(start_timeout_ms=1000))
    res = None
    for _ in range(100):
        res = ep.feed(bytes(640))
        if res:
            break
    assert res == "timeout" and not ep.started


def test_g711_roundtrip_and_resampling():
    tone = pcm.cue("start", 80)
    for codec in ("PCMA", "PCMU"):
        back = pcm.g711_decode(pcm.g711_encode(tone, codec), codec)
        assert abs(pcm.rms_dbfs(back) - pcm.rms_dbfs(tone)) < 0.5
    down = pcm.resample(tone, 16000, 8000)
    assert abs(len(down) * 2 - len(tone)) <= 4
    rs = pcm.StreamResampler(8000, 16000)
    out = b"".join(rs(down[i:i + 320]) for i in range(0, len(down), 320))
    assert len(out) > len(down)
    assert pcm.gain(tone, 50) != tone and pcm.gain(tone, 100) == tone


def test_unfinished_transcripts_are_recognised():
    from telerouter.voice.vad import looks_unfinished
    for t in ("Since it takes so long, I usually close my...", "Call my", "Turn on the lights and",
              "Can you also add a rule to your", "I want to", "Connect me to the,"):
        assert looks_unfinished(t), t
    for t in ("Turn off all of the lights.", "What time is it?", "Call my dad.", "Goodbye!", ""):
        assert not looks_unfinished(t), t


class _LiveSpeech:
    """Fake STT: counts requests; the live model answers with how much audio it got."""

    live_available = True

    def __init__(self):
        self.finals, self.lives = [], []

    async def transcribe(self, audio, language="", names=None):
        self.finals.append(len(audio))
        return "turn on the lights."

    async def transcribe_live(self, audio, language=""):
        self.lives.append(len(audio))
        return f"words {len(audio)}"


async def _feed(ep, chunks):
    import asyncio
    for c in chunks:
        if ep.feed(c):
            return ep.result
        await asyncio.sleep(0)               # let the live tasks run, as between real 20 ms frames
    return ep.result


async def test_the_accurate_model_starts_at_the_first_pause_and_its_text_is_reused():
    import asyncio

    from telerouter.voice.live import LiveTranscriber
    speech = read(FIXTURES / "speech.wav")
    ep = Endpointer(EndpointConfig(start_timeout_ms=3000, end_silence_ms=1000, max_ms=10000))
    stt, shown = _LiveSpeech(), []
    live = LiveTranscriber(stt, ep, on_partial=shown.append)
    chunks = [speech[i:i + 640] for i in range(0, len(speech), 640)] + [bytes(640)] * 100
    assert await _feed(ep, chunks) == "speech"
    assert ep.speech_end < len(ep.audio)                       # the closing pause is not speech
    for _ in range(10):
        await asyncio.sleep(0)
    early = len(stt.finals)
    assert early >= 1                                          # started during the pause, before the end
    assert await live.final() == "turn on the lights."
    assert live.early_used and len(stt.finals) == early        # no second request after the end
    assert stt.lives and shown and shown[-1].startswith("words ")   # the words so far were shown
    live.close()
    assert ep.on_feed is None


async def test_speech_after_a_pause_makes_the_final_text_fresh():
    from telerouter.voice.live import LiveTranscriber
    speech = read(FIXTURES / "speech.wav")
    ep = Endpointer(EndpointConfig(start_timeout_ms=3000, end_silence_ms=4000, max_ms=20000))
    stt = _LiveSpeech()
    stt.live_available = False                                 # no live model: no partials at all
    live = LiveTranscriber(stt, ep, on_partial=lambda t: None)
    sp = [speech[i:i + 640] for i in range(0, len(speech), 640)]
    await _feed(ep, sp + [bytes(640)] * 20)                    # a 0.4 s pause mid-utterance...
    n = len(stt.finals)
    assert n >= 1 and not stt.lives
    before = ep.speech_end
    for c in sp:                                               # ...then it goes on: stop right after new speech
        await _feed(ep, [c])
        if ep.speech_end > before and ep.silence_ms == 0:
            break
    assert len(stt.finals) == n
    end = ep.speech_end
    ep.result = "speech"                                       # ends before the next pause starts a request
    assert await live.final() == "turn on the lights."
    assert not live.early_used and len(stt.finals) == n + 1 and stt.finals[-1] == len(ep.audio) >= end
    live.close()


def test_old_default_pauses_become_one_second_once(tmp_path):
    from telerouter.models import Config
    from telerouter.store import migrate
    cfg = Config()
    cfg.voice.end_silence_ms, cfg.voice.phone_end_silence_ms, cfg.voice.pause_defaults = 1200, 1500, 0
    assert migrate(cfg)
    assert (cfg.voice.end_silence_ms, cfg.voice.phone_end_silence_ms, cfg.voice.pause_defaults) == (1000, 1000, 2)
    cfg.voice.end_silence_ms = 1200                            # chosen by the owner afterwards: kept
    migrate(cfg)
    assert cfg.voice.end_silence_ms == 1200
    custom = Config()
    custom.voice.end_silence_ms, custom.voice.phone_end_silence_ms = 1700, 2000
    migrate(custom)
    assert (custom.voice.end_silence_ms, custom.voice.phone_end_silence_ms) == (1700, 2000)
