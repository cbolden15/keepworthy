from pathlib import Path
from src.transcript import clean_vtt

FIX = Path(__file__).parent / "fixtures" / "sample.vtt"

def test_clean_vtt_removes_header_and_timings():
    out = clean_vtt(FIX.read_text())
    assert "WEBVTT" not in out
    assert "-->" not in out
    assert "align:start" not in out
    assert "<c>" not in out and "<00:" not in out

def test_clean_vtt_dedupes_rolling_lines():
    out = clean_vtt(FIX.read_text())
    # "hello and welcome" and "to the channel" each appear once despite VTT repetition
    assert out.count("hello and welcome") == 1
    assert out.count("to the channel") == 1
    assert "today we install yt-dlp" in out

def test_clean_vtt_empty():
    assert clean_vtt("WEBVTT\n\n") == ""
