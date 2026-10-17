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
